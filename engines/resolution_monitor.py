"""
PolyEdge v4 — Resolution Monitor
==================================
Monitors positions for market resolution and handles:
  1. Position age tracking (days held, days to expiry)
  2. Resolution alerts via Telegram
  3. Auto-claim resolved winning positions (redeem shares → USDC)

Runs as a background loop in the orchestrator, checking every 5 minutes.

Polymarket resolution flow:
  - Market expires → oracle reports outcome → condition resolves
  - Winning shares can be redeemed: 1 share → $1.00 USDC
  - Losing shares → worthless ($0)
  - Redemption is via CTF contract: redeemPositions()
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("polyedge.resolution")


@dataclass
class TrackedPosition:
    """A position with age and resolution tracking."""
    condition_id: str
    token_id: str
    title: str
    outcome: str  # "Yes" or "No"
    size: float  # Number of shares
    avg_price: float
    cur_price: float
    cost: float
    value: float
    pnl: float
    pnl_pct: float
    end_date: Optional[datetime] = None
    first_seen: float = 0.0  # Timestamp when we first tracked this
    resolved: bool = False
    resolution_outcome: str = ""  # "Yes" or "No" — the winning side
    claimed: bool = False

    @property
    def days_held(self) -> int:
        if self.first_seen <= 0:
            return 0
        return int((time.time() - self.first_seen) / 86400)

    @property
    def days_to_expiry(self) -> Optional[int]:
        if not self.end_date:
            return None
        now = datetime.now(timezone.utc)
        delta = self.end_date - now
        return max(0, delta.days)

    @property
    def is_expired(self) -> bool:
        if not self.end_date:
            return False
        return datetime.now(timezone.utc) > self.end_date

    @property
    def is_winner(self) -> bool:
        """Did we bet on the winning side?"""
        if not self.resolved or not self.resolution_outcome:
            return False
        return self.outcome.lower() == self.resolution_outcome.lower()

    @property
    def claimable_value(self) -> float:
        """How much USDC we can claim if we won."""
        if self.is_winner:
            return self.size * 1.0  # Each winning share = $1
        return 0.0


class ResolutionMonitor:
    """
    Monitors positions for resolution and handles claims.

    Usage:
        monitor = ResolutionMonitor(telegram_notify=send_func, clob_client=clob)
        await monitor.start()  # Runs forever, checking every 5 min
    """

    def __init__(
        self,
        telegram_notify=None,
        clob_client=None,
        proxy_wallet: str = "0xC0637B5E68A4C6170b319c507d7dc962088c2af9",
        check_interval: int = 300,  # 5 minutes
        auto_claim: bool = True,
    ):
        self.telegram_notify = telegram_notify
        self.clob = clob_client
        self.proxy_wallet = proxy_wallet
        self.check_interval = check_interval
        self.auto_claim = auto_claim

        self._positions: Dict[str, TrackedPosition] = {}
        self._alerted: set = set()  # condition_ids we already alerted on
        self._claimed: set = set()  # condition_ids we already claimed
        self._running = False

    async def start(self):
        """Main loop — check positions every N minutes."""
        self._running = True
        logger.info(f"Resolution monitor started (interval={self.check_interval}s, auto_claim={self.auto_claim})")

        while self._running:
            try:
                await self._check_cycle()
            except Exception as e:
                logger.error(f"Resolution monitor error: {e}")

            await asyncio.sleep(self.check_interval)

    async def stop(self):
        self._running = False

    async def _check_cycle(self):
        """One check cycle: fetch positions, check resolution, alert, claim."""
        # 1. Fetch current positions from data-api
        positions = await self._fetch_positions()
        if not positions:
            return

        # 2. Update tracked positions
        self._update_tracked(positions)

        # 3. Check for newly resolved markets
        resolved = [p for p in self._positions.values()
                    if p.is_expired and p.condition_id not in self._alerted]

        for pos in resolved:
            # Check if actually resolved on-chain
            is_resolved, winner = await self._check_market_resolved(pos.condition_id)

            if is_resolved:
                pos.resolved = True
                pos.resolution_outcome = winner

                # Alert via Telegram
                await self._send_resolution_alert(pos)
                self._alerted.add(pos.condition_id)

                # Auto-claim if winning
                if self.auto_claim and pos.is_winner and pos.condition_id not in self._claimed:
                    await self._attempt_claim(pos)

            elif pos.is_expired:
                # Expired but not resolved yet — might be pending oracle
                days_past = -pos.days_to_expiry if pos.days_to_expiry == 0 else 0
                if pos.condition_id not in self._alerted:
                    logger.info(f"Position expired but not yet resolved: {pos.title[:40]}")

        # 4. Check upcoming expirations (alert 24h before)
        upcoming = [p for p in self._positions.values()
                    if p.days_to_expiry is not None
                    and 0 < p.days_to_expiry <= 1
                    and f"upcoming_{p.condition_id}" not in self._alerted]

        for pos in upcoming:
            await self._send_expiry_warning(pos)
            self._alerted.add(f"upcoming_{pos.condition_id}")

    async def _fetch_positions(self) -> List[dict]:
        """Fetch positions from Polymarket data-api."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://data-api.polymarket.com/positions",
                    params={
                        "user": self.proxy_wallet,
                        "sizeThreshold": "0.01"
                    }
                )
                if resp.status_code == 200:
                    return resp.json()
                else:
                    logger.warning(f"Data API returned {resp.status_code}")
                    return []
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            return []

    def _update_tracked(self, positions: List[dict]):
        """Update internal tracking with fresh position data."""
        now = time.time()

        for p in positions:
            cid = p.get("conditionId", p.get("condition_id", ""))
            if not cid:
                continue

            size = float(p.get("size", 0))
            if size <= 0:
                continue

            avg = float(p.get("avgPrice", p.get("avg_price", 0)))
            cur = float(p.get("curPrice", p.get("cur_price", 0)))
            cost = size * avg
            value = size * cur
            pnl = value - cost
            pnl_pct = (pnl / cost * 100) if cost > 0 else 0

            end_date = None
            end_str = p.get("endDate", p.get("end_date", ""))
            if end_str:
                try:
                    end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

            if cid in self._positions:
                # Update existing
                tracked = self._positions[cid]
                tracked.size = size
                tracked.cur_price = cur
                tracked.value = value
                tracked.pnl = pnl
                tracked.pnl_pct = pnl_pct
            else:
                # New position
                self._positions[cid] = TrackedPosition(
                    condition_id=cid,
                    token_id=p.get("tokenId", p.get("token_id", "")),
                    title=p.get("title", p.get("market", "Unknown")),
                    outcome=p.get("outcome", "?"),
                    size=size,
                    avg_price=avg,
                    cur_price=cur,
                    cost=cost,
                    value=value,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    end_date=end_date,
                    first_seen=now,
                )
                logger.info(f"Tracking new position: {self._positions[cid].title[:50]}")

    async def _check_market_resolved(self, condition_id: str) -> tuple:
        """Check if a market has been resolved on Polymarket."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Check gamma-api for resolution info
                resp = await client.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"condition_id": condition_id}
                )
                if resp.status_code == 200:
                    markets = resp.json()
                    if markets:
                        m = markets[0]
                        resolved = m.get("resolved", False)
                        winner = ""
                        if resolved:
                            # Determine winning outcome
                            tokens = m.get("tokens", [])
                            for t in tokens:
                                if float(t.get("price", 0)) >= 0.95:
                                    winner = t.get("outcome", "")
                                    break
                            if not winner:
                                winner = m.get("winner", "")
                        return (resolved, winner)
        except Exception as e:
            logger.error(f"Failed to check resolution for {condition_id[:20]}: {e}")

        return (False, "")

    async def _send_resolution_alert(self, pos: TrackedPosition):
        """Send Telegram alert when a market resolves."""
        if not self.telegram_notify:
            return

        if pos.is_winner:
            emoji = "🎉"
            result = f"WON — claimable: ${pos.claimable_value:.2f}"
        else:
            emoji = "💀"
            result = f"LOST — ${pos.cost:.2f} gone"

        msg = (
            f"{emoji} *MARKET RESOLVED*\n\n"
            f"*{pos.title}*\n"
            f"Our bet: {pos.outcome} ({pos.size:.0f} shares @ ${pos.avg_price:.3f})\n"
            f"Winner: {pos.resolution_outcome}\n"
            f"Result: {result}\n"
            f"Held: {pos.days_held} days"
        )

        try:
            await self.telegram_notify(msg)
            logger.info(f"Resolution alert sent: {pos.title[:40]} → {result}")
        except Exception as e:
            logger.error(f"Failed to send resolution alert: {e}")

    async def _send_expiry_warning(self, pos: TrackedPosition):
        """Send Telegram alert 24h before market expires."""
        if not self.telegram_notify:
            return

        msg = (
            f"⏰ *EXPIRING IN 24h*\n\n"
            f"*{pos.title}*\n"
            f"Our bet: {pos.outcome} ({pos.size:.0f} shares)\n"
            f"Current P&L: {pos.pnl_pct:+.1f}% (${pos.pnl:+.2f})\n"
            f"Value: ${pos.value:.2f}\n\n"
            f"Consider selling if uncertain."
        )

        try:
            await self.telegram_notify(msg)
            logger.info(f"Expiry warning sent: {pos.title[:40]}")
        except Exception as e:
            logger.error(f"Failed to send expiry warning: {e}")

    async def _attempt_claim(self, pos: TrackedPosition):
        """Attempt to claim/redeem winning shares."""
        logger.info(f"Attempting to claim {pos.size:.0f} shares of {pos.title[:40]} (${pos.claimable_value:.2f})")

        # Polymarket redemption requires calling the CTF contract
        # For now, we log and alert — full on-chain redemption needs web3
        if self.telegram_notify:
            msg = (
                f"💰 *AUTO-CLAIM READY*\n\n"
                f"*{pos.title}*\n"
                f"Shares: {pos.size:.0f} winning shares\n"
                f"Value: ${pos.claimable_value:.2f} USDC\n\n"
                f"⚠️ Manual claim needed on polymarket.com/portfolio"
            )
            try:
                await self.telegram_notify(msg)
            except Exception:
                pass

        # TODO: Implement on-chain redemption via CTF contract
        # This requires:
        # 1. web3.py connection to Polygon
        # 2. CTF contract ABI (ConditionalTokens)
        # 3. Call: ctf.redeemPositions(collateral, parentCollectionId, conditionId, indexSets)
        # For now, alert user to claim manually

        self._claimed.add(pos.condition_id)
        logger.info(f"Claim alert sent for {pos.title[:40]}")

    # ─── Public API ─────────────────────────────────────────

    def get_positions_summary(self) -> List[dict]:
        """Get all tracked positions with age info for dashboard."""
        result = []
        for pos in sorted(self._positions.values(), key=lambda p: p.value, reverse=True):
            result.append({
                "title": pos.title,
                "outcome": pos.outcome,
                "size": pos.size,
                "avg_price": pos.avg_price,
                "cur_price": pos.cur_price,
                "cost": pos.cost,
                "value": pos.value,
                "pnl": pos.pnl,
                "pnl_pct": pos.pnl_pct,
                "days_held": pos.days_held,
                "days_to_expiry": pos.days_to_expiry,
                "is_expired": pos.is_expired,
                "resolved": pos.resolved,
                "is_winner": pos.is_winner,
                "claimable": pos.claimable_value,
            })
        return result

    def get_expiring_soon(self, days: int = 7) -> List[TrackedPosition]:
        """Get positions expiring within N days."""
        return [
            p for p in self._positions.values()
            if p.days_to_expiry is not None and 0 < p.days_to_expiry <= days
        ]

    def get_expired_unclaimed(self) -> List[TrackedPosition]:
        """Get expired positions that haven't been claimed."""
        return [
            p for p in self._positions.values()
            if p.is_expired and not p.claimed
        ]
