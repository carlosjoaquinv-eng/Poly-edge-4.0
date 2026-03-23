"""
PolyEdge v4 — Liquidity Checker
================================
Pre-trade liquidity analysis to prevent getting trapped in illiquid markets.

Philosophy: "Don't enter where you can't exit"

Checks:
  1. Exit depth — enough $ on the exit side to absorb our position
  2. Spread health — tight spread = liquid, wide = illiquid
  3. Depth ratio — imbalanced books signal potential traps
  4. Slippage estimate — how much would selling our position cost

Usage:
  checker = LiquidityChecker()
  result = checker.analyze(orderbook, side="buy", size=15, price=0.38)
  if result.safe_to_enter:
      # Place trade
  else:
      logger.warning(f"Skipping: {result.reason}")
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("polyedge.liquidity")


@dataclass
class LiquidityConfig:
    """Liquidity check thresholds."""
    # Minimum exit depth ($ available on the opposite side)
    min_exit_depth_usd: float = 50.0        # At least $50 to sell into

    # Minimum depth as multiple of our trade size
    min_depth_multiple: float = 3.0         # Exit depth >= 3x our trade size

    # Maximum acceptable slippage to exit
    max_exit_slippage_pct: float = 5.0      # Max 5% slippage to exit full position

    # Spread health
    max_spread_pct: float = 10.0            # Max 10% spread (wider = illiquid)

    # Depth imbalance (bid_depth / ask_depth)
    min_depth_ratio: float = 0.3            # At least 30% balance (0.3 to 3.0 is healthy)
    max_depth_ratio: float = 3.0

    # Minimum number of price levels on exit side
    min_exit_levels: int = 3                # At least 3 price levels to absorb

    # Dust filter — ignore orders below this size
    dust_filter_usd: float = 0.50           # Ignore orders < $0.50


@dataclass
class LiquidityResult:
    """Result of liquidity analysis."""
    safe_to_enter: bool = False
    reason: str = ""

    # Raw metrics
    bid_depth_usd: float = 0.0      # Total $ in bids
    ask_depth_usd: float = 0.0      # Total $ in asks
    exit_depth_usd: float = 0.0     # $ available on exit side
    spread_pct: float = 0.0         # Current spread %
    depth_ratio: float = 0.0        # bid/ask depth ratio
    exit_levels: int = 0            # Number of price levels on exit side
    estimated_slippage_pct: float = 0.0  # Estimated slippage to exit

    # Scores (0-100)
    depth_score: int = 0
    spread_score: int = 0
    balance_score: int = 0
    overall_score: int = 0


class LiquidityChecker:
    """
    Analyzes orderbook liquidity before entering a trade.

    Core principle: Before buying, check if you can sell.
    Before selling, check if you can buy back.
    """

    def __init__(self, config: Optional[LiquidityConfig] = None):
        self.config = config or LiquidityConfig()

    def analyze(
        self,
        orderbook: Dict,
        side: str,          # "buy" or "sell" — the side WE want to enter
        size: float,        # Number of shares we want to trade
        price: float,       # Price we'd enter at
    ) -> LiquidityResult:
        """
        Analyze orderbook liquidity for a potential trade.

        If we're BUYING, check if there's enough BID depth to sell into later.
        If we're SELLING, check if there's enough ASK depth to buy back later.
        """
        result = LiquidityResult()

        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])

        if not bids and not asks:
            result.reason = "Empty orderbook — no liquidity"
            return result

        # Parse orderbook
        bid_levels = self._parse_levels(bids, self.config.dust_filter_usd)
        ask_levels = self._parse_levels(asks, self.config.dust_filter_usd)

        # Calculate depths
        result.bid_depth_usd = sum(p * s for p, s in bid_levels)
        result.ask_depth_usd = sum(p * s for p, s in ask_levels)

        # Exit side: if we BUY, we need BIDs to sell into. If we SELL, we need ASKs to buy back.
        if side == "buy":
            exit_levels = bid_levels
            result.exit_depth_usd = result.bid_depth_usd
            result.exit_levels = len(exit_levels)
        else:
            exit_levels = ask_levels
            result.exit_depth_usd = result.ask_depth_usd
            result.exit_levels = len(exit_levels)

        # Spread calculation
        best_bid = max((p for p, s in bid_levels), default=0)
        best_ask = min((p for p, s in ask_levels), default=0)

        if best_bid > 0 and best_ask > 0:
            mid = (best_bid + best_ask) / 2
            result.spread_pct = ((best_ask - best_bid) / mid) * 100 if mid > 0 else 100
        else:
            result.spread_pct = 100  # No valid spread

        # Depth ratio
        if result.ask_depth_usd > 0 and result.bid_depth_usd > 0:
            result.depth_ratio = result.bid_depth_usd / result.ask_depth_usd
        else:
            result.depth_ratio = 0

        # Slippage estimation
        trade_notional = size * price
        result.estimated_slippage_pct = self._estimate_slippage(
            exit_levels, size, price
        )

        # ── Scoring ──

        # Depth score (0-100)
        if result.exit_depth_usd <= 0:
            result.depth_score = 0
        else:
            depth_multiple = result.exit_depth_usd / max(trade_notional, 0.01)
            result.depth_score = min(100, int(depth_multiple / self.config.min_depth_multiple * 100))

        # Spread score (0-100) — tighter = better
        if result.spread_pct <= 2:
            result.spread_score = 100
        elif result.spread_pct >= self.config.max_spread_pct:
            result.spread_score = 0
        else:
            result.spread_score = max(0, int(100 - (result.spread_pct - 2) / (self.config.max_spread_pct - 2) * 100))

        # Balance score (0-100) — balanced book = better
        if result.depth_ratio <= 0:
            result.balance_score = 0
        elif self.config.min_depth_ratio <= result.depth_ratio <= self.config.max_depth_ratio:
            # Closer to 1.0 = better
            balance = 1.0 - abs(1.0 - result.depth_ratio) / max(self.config.max_depth_ratio - 1, 1)
            result.balance_score = max(0, int(balance * 100))
        else:
            result.balance_score = 10  # Very imbalanced

        # Overall score (weighted average)
        result.overall_score = int(
            result.depth_score * 0.50 +      # Depth is most important
            result.spread_score * 0.30 +      # Spread matters
            result.balance_score * 0.20       # Balance is nice-to-have
        )

        # ── Decision ──

        reasons = []

        # Check 1: Minimum exit depth
        if result.exit_depth_usd < self.config.min_exit_depth_usd:
            reasons.append(
                f"Exit depth ${result.exit_depth_usd:.0f} < ${self.config.min_exit_depth_usd:.0f} min"
            )

        # Check 2: Depth multiple
        if trade_notional > 0 and result.exit_depth_usd < trade_notional * self.config.min_depth_multiple:
            reasons.append(
                f"Exit depth ${result.exit_depth_usd:.0f} < {self.config.min_depth_multiple}x trade ${trade_notional:.0f}"
            )

        # Check 3: Slippage
        if result.estimated_slippage_pct > self.config.max_exit_slippage_pct:
            reasons.append(
                f"Exit slippage {result.estimated_slippage_pct:.1f}% > {self.config.max_exit_slippage_pct}% max"
            )

        # Check 4: Spread
        if result.spread_pct > self.config.max_spread_pct:
            reasons.append(
                f"Spread {result.spread_pct:.1f}% > {self.config.max_spread_pct}% max"
            )

        # Check 5: Minimum exit levels
        if result.exit_levels < self.config.min_exit_levels:
            reasons.append(
                f"Only {result.exit_levels} exit levels < {self.config.min_exit_levels} min"
            )

        # Check 6: Depth balance
        if result.depth_ratio > 0 and (
            result.depth_ratio < self.config.min_depth_ratio or
            result.depth_ratio > self.config.max_depth_ratio
        ):
            reasons.append(
                f"Depth imbalance: ratio {result.depth_ratio:.2f} (healthy: {self.config.min_depth_ratio}-{self.config.max_depth_ratio})"
            )

        if reasons:
            result.safe_to_enter = False
            result.reason = " | ".join(reasons)
        else:
            result.safe_to_enter = True
            result.reason = f"OK: depth ${result.exit_depth_usd:.0f}, spread {result.spread_pct:.1f}%, score {result.overall_score}/100"

        return result

    def _parse_levels(self, levels: List, dust_filter: float) -> List[Tuple[float, float]]:
        """Parse orderbook levels into (price, size) tuples, filtering dust."""
        parsed = []
        for level in levels:
            price = float(level.get("price", 0))
            size = float(level.get("size", 0))
            if price > 0 and size > 0 and (price * size) >= dust_filter:
                parsed.append((price, size))
        return parsed

    def _estimate_slippage(
        self,
        exit_levels: List[Tuple[float, float]],
        size: float,
        entry_price: float,
    ) -> float:
        """
        Estimate slippage to exit full position.

        Walk through exit levels consuming liquidity until we've
        filled our entire position. Calculate avg exit price vs entry.
        """
        if not exit_levels or size <= 0 or entry_price <= 0:
            return 100.0  # No exit liquidity = 100% slippage

        # Sort: bids descending (best first), asks ascending (best first)
        # We assume exit_levels are already the right side
        sorted_levels = sorted(exit_levels, key=lambda x: x[0], reverse=True)

        remaining = size
        total_value = 0.0

        for price, available in sorted_levels:
            fill = min(remaining, available)
            total_value += fill * price
            remaining -= fill
            if remaining <= 0:
                break

        if remaining > 0:
            # Couldn't fill entire position — massive slippage
            # Assume remaining fills at 50% of entry (worst case)
            total_value += remaining * entry_price * 0.5

        avg_exit = total_value / size if size > 0 else 0
        slippage_pct = abs(entry_price - avg_exit) / entry_price * 100 if entry_price > 0 else 100

        return round(slippage_pct, 2)

    def quick_check(self, orderbook: Dict, side: str, size: float, price: float) -> bool:
        """Fast boolean check — safe to trade?"""
        result = self.analyze(orderbook, side, size, price)
        if not result.safe_to_enter:
            logger.debug(f"⛔ Liquidity check FAILED: {result.reason}")
        return result.safe_to_enter
