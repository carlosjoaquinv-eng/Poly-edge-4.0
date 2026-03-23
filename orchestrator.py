"""
PolyEdge v4 — Main Orchestrator
=================================
Wires everything together: services, engines, feeds, dashboard.
Extracted from main_v4.py.
"""

import asyncio
import time
import logging
from typing import Optional

from config_v4 import Config
from engines.market_maker import MarketMakerEngine
from engines.resolution_sniper_v2 import ResolutionSniperV2
from engines.meta_strategist import MetaStrategist

from infrastructure.logging_setup import setup_logging
from infrastructure.clob_client import CLOBClient
from infrastructure.datastore import DataStore
from notifiers.telegram import TelegramNotifier
from feeds.bridge import FeedBridge
from api.dashboard import DashboardAPI
from engines.exit_manager import ExitManager, ExitConfig


class PolyEdgeV4:
    """
    Main orchestrator — wires everything together and runs.

    Startup sequence:
      1. Load config
      2. Initialize shared services (CLOB, Telegram, DataStore)
      3. Initialize engines
      4. Start all engines + feeds + dashboard concurrently
      5. Handle shutdown gracefully
    """

    def __init__(self):
        self.config = Config()
        self.logger = logging.getLogger("polyedge.main")

        # Shared services
        self.clob: Optional[CLOBClient] = None
        self.telegram: Optional[TelegramNotifier] = None
        self.store: Optional[DataStore] = None
        self.orderbook_ws = None  # OrderbookWSFeed for real-time orderbook data

        # Engines
        self.mm: Optional[MarketMakerEngine] = None
        self.sniper: Optional[ResolutionSniperV2] = None
        self.meta: Optional[MetaStrategist] = None
        self.exit_manager: Optional[ExitManager] = None

        # Feed bridge
        self.feeds: Optional[FeedBridge] = None

        # Dashboard
        self.dashboard: Optional[DashboardAPI] = None

        # Engine health alert flags (one-shot per component)
        self._engine_stop_alerted = {
            "MM": False, "Sniper": False, "Meta": False,
            "Dashboard": False, "WS_stale": False,
        }

    async def start(self):
        """Initialize and start everything."""
        # Setup logging
        setup_logging(self.config)

        # Validate config
        warnings = self.config.validate()
        for w in warnings:
            self.logger.warning(w)

        self.logger.info(f"\n{self.config.summary()}")

        # Initialize shared services
        self.clob = CLOBClient(self.config)
        await self.clob.connect()

        # Initialize real-time orderbook WebSocket feed
        from feeds.orderbook_ws import OrderbookWSFeed
        self.orderbook_ws = OrderbookWSFeed(
            ws_url=self.config.CLOB_WS_URL,
            rest_url=self.config.CLOB_API_URL,
        )
        self.clob.set_ws_feed(self.orderbook_ws)
        await self.orderbook_ws.start()

        self.telegram = TelegramNotifier(self.config)
        self.store = DataStore(self.config)
        self.telegram.set_store(self.store)

        # Initialize engines
        self.mm = MarketMakerEngine(
            config=self.config.mm,
            clob_client=self.clob,
            data_store=self.store,
            telegram=self.telegram,
        )

        self.sniper = ResolutionSniperV2(
            config=self.config.sniper,
            clob_client=self.clob,
            data_store=self.store,
            telegram=self.telegram,
        )

        self.meta = MetaStrategist(
            config=self.config.meta,
            telegram=self.telegram,
        )

        # Exit Manager (portfolio-wide SL/TP)
        proxy_addr = getattr(self.config, "POLYMARKET_PROXY_ADDRESS", "")
        self.exit_manager = ExitManager(
            config=ExitConfig(enabled=True),
            clob=self.clob,
            proxy_address=proxy_addr,
            telegram=self.telegram,
            paper_mode=self.config.PAPER_MODE,
        )

        # Feed bridge
        self.feeds = FeedBridge(self.config, self.sniper)

        # Dashboard
        self.dashboard = DashboardAPI(self.config, self.mm, self.sniper, self.meta,
                                       orderbook_ws=self.orderbook_ws, clob=self.clob,
                                       store=self.store)

        # Wire telegram commands to engines + WS feed
        self.telegram.set_engines(self.mm, self.sniper, self.meta)
        self.telegram.set_ws_feed(self.orderbook_ws)

        # Wire WS disconnect/reconnect alerts to Telegram
        async def _ws_alert(event_type, stats):
            if event_type == "disconnect":
                await self.telegram.send(
                    f"⚠️ <b>Orderbook WS disconnected</b>\n"
                    f"Subscribed tokens: {stats.get('subscribed', 0)}\n"
                    f"Reconnects so far: {stats.get('reconnects', 0)}\n"
                    f"Falling back to REST polling."
                )
            elif event_type == "reconnect":
                await self.telegram.send(
                    f"✅ <b>Orderbook WS reconnected</b>\n"
                    f"Subscribed tokens: {stats.get('subscribed', 0)}\n"
                    f"Real-time feed restored."
                )
        self.orderbook_ws.set_alert_callback(_ws_alert)

        # Wire live configs to meta-strategist for prompt building
        self.meta.set_configs(self.config.mm, self.config.sniper)

        # Start notification
        mode = "PAPER" if self.config.PAPER_MODE else "🔴 LIVE"
        await self.telegram.send(
            f"🚀 PolyEdge v{self.config.VERSION} Starting ({mode})\n"
            f"Bankroll: ${self.config.BANKROLL:,.0f}\n"
            f"Engines: MM + Sniper + Meta\n"
            f"Dashboard: :{self.config.DASHBOARD_PORT}\n"
            f"Type /help for commands"
        )

        # Launch everything
        self.logger.info("Starting all engines...")

        # Restore state from last run (if any)
        try:
            mm_state = self.store.load("mm_state")
            if mm_state:
                self.mm.load_state(mm_state)
                self.logger.info("MM state restored from disk")

            # Sync MM inventory with on-chain positions
            await self._sync_inventory_with_chain()

            sniper_state = self.store.load("sniper_state")
            if sniper_state:
                self.sniper.load_state(sniper_state)
                self.logger.info("Sniper state restored from disk")
        except Exception as e:
            self.logger.warning(f"Could not restore state: {e} — starting fresh")

        try:
            await asyncio.gather(
                self.mm.start(paper_mode=self.config.PAPER_MODE),
                self.sniper.start(paper_mode=not self.config.SNIPER_LIVE),
                self.meta.start(),
                self.exit_manager.start(),
                self.feeds.start(),
                self.dashboard.start(),
                self.telegram.start_polling(),
                self._stats_loop(),
                self._resolution_monitor_loop(),
            )
        except asyncio.CancelledError:
            self.logger.info("Shutdown signal received")
        finally:
            await self.shutdown()

    async def _sync_inventory_with_chain(self):
        """Sync MM inventory tracker with actual on-chain positions."""
        try:
            import httpx
            proxy_addr = getattr(self.config, "POLYMARKET_PROXY_ADDRESS", "")
            if not proxy_addr or not self.mm:
                return

            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"https://data-api.polymarket.com/positions?user={proxy_addr.lower()}"
                )
                if r.status_code != 200:
                    return
                positions = r.json()

            # Build on-chain position map: token_id -> size
            chain_positions = {}
            for p in positions:
                token_id = p.get("asset", "")
                size = float(p.get("size", 0))
                if token_id and size > 0:
                    chain_positions[token_id] = size

            # Compare with MM inventory and correct
            inv = self.mm.inventory
            corrected = 0
            for tid, pos in list(inv._positions.items()):
                chain_size = chain_positions.get(tid, 0)
                tracker_size = pos.net_position

                if abs(tracker_size - chain_size) > 1:
                    old = tracker_size
                    pos.net_position = chain_size
                    corrected += 1
                    self.logger.warning(
                        f"📊 Inventory sync: {tid[:16]}... "
                        f"{old:.0f} → {chain_size:.0f} shares"
                    )
                    # If chain shows 0, clear the position
                    if chain_size < 1:
                        pos.net_position = 0

            if corrected:
                self.logger.info(f"📊 Inventory sync: corrected {corrected} positions")
                # Save corrected state
                self.store.save("mm_state", self.mm.save_state())
            else:
                self.logger.info("📊 Inventory sync: all positions match on-chain")

        except Exception as e:
            self.logger.warning(f"Inventory sync failed: {e}")

    async def _stats_loop(self):
        """Periodically save stats and feed data to meta-strategist."""
        _last_daily_report_date = None
        while True:
            await asyncio.sleep(300)  # Every 5 min

            try:
                # Feed engine stats to meta-strategist
                mm_stats = self.mm.get_stats() if self.mm else {}
                sniper_stats = self.sniper.get_stats() if self.sniper else {}
                self.meta.analyzer.take_snapshot(mm_stats, sniper_stats, self.config.BANKROLL)

                # Persist stats (for dashboard)
                self.store.save("mm_stats", mm_stats)
                self.store.save("sniper_stats", sniper_stats)
                self.store.save("meta_stats", self.meta.get_stats() if self.meta else {})

                # Append PnL snapshot to history log (JSONL)
                # Get live USDC balance + equity from on-chain positions
                _balance = 0
                _equity = 0
                try:
                    _balance = await self.clob.get_balance() or 0
                except Exception:
                    pass
                try:
                    import httpx
                    _proxy = getattr(self.config, "POLYMARKET_PROXY_ADDRESS", "")
                    if _proxy:
                        async with httpx.AsyncClient(timeout=10) as _hc:
                            _r = await _hc.get(f"https://data-api.polymarket.com/positions?user={_proxy.lower()}")
                            if _r.status_code == 200:
                                _positions = _r.json()
                                _pos_value = sum(
                                    float(p.get("size", 0)) * float(p.get("curPrice", 0))
                                    for p in _positions
                                )
                                _equity = round(_pos_value + _balance, 2)
                except Exception:
                    pass

                _mm_trades = mm_stats.get("trade_log_count", mm_stats.get("total_fills", 0))
                _sn_trades = sniper_stats.get("executor", {}).get("completed_trades", 0)
                _deposit = self.config.BANKROLL or 507
                self.store.append_log("pnl_history", {
                    "mm_realized": mm_stats.get("pnl", {}).get("realized", 0),
                    "mm_unrealized": mm_stats.get("pnl", {}).get("unrealized", 0),
                    "sniper_pnl": sniper_stats.get("executor", {}).get("total_pnl", 0),
                    "mm_exposure": mm_stats.get("inventory", {}).get("total_exposure", 0),
                    "sniper_exposure": sniper_stats.get("executor", {}).get("total_exposure", 0),
                    "balance": round(_balance, 2),
                    "equity": _equity,
                    "real_pnl": round(_equity - _deposit, 2) if _equity > 0 else 0,
                    "trade_count": _mm_trades + _sn_trades,
                })

                # Persist full engine state (survives restarts)
                if self.mm:
                    self.store.save("mm_state", self.mm.save_state())
                if self.sniper:
                    self.store.save("sniper_state", self.sniper.save_state())

                # Engine health check — alert if stopped unexpectedly
                for name, engine in [("MM", self.mm), ("Sniper", self.sniper), ("Meta", self.meta)]:
                    if engine and hasattr(engine, '_running') and not engine._running:
                        if not self._engine_stop_alerted.get(name):
                            self._engine_stop_alerted[name] = True
                            await self.telegram.send(f"⚠️ <b>{name} engine stopped unexpectedly</b>")
                    elif engine and hasattr(engine, '_running') and engine._running:
                        self._engine_stop_alerted[name] = False

                # Dashboard self-check
                try:
                    import aiohttp as _aio
                    async with _aio.ClientSession() as _sess:
                        async with _sess.get(
                            f"http://127.0.0.1:{self.config.DASHBOARD_PORT}/health",
                            timeout=_aio.ClientTimeout(total=5),
                        ) as _resp:
                            if _resp.status != 200:
                                raise Exception(f"HTTP {_resp.status}")
                except Exception as _he:
                    self.logger.error(f"Dashboard health check failed: {_he}")
                    if not self._engine_stop_alerted.get("Dashboard"):
                        self._engine_stop_alerted["Dashboard"] = True
                        await self.telegram.send(
                            f"🔴 <b>Dashboard not responding</b>\n"
                            f"Port {self.config.DASHBOARD_PORT} — {_he}"
                        )
                else:
                    if self._engine_stop_alerted.get("Dashboard"):
                        self._engine_stop_alerted["Dashboard"] = False
                        await self.telegram.send("✅ <b>Dashboard recovered</b>")

                # WebSocket feed check
                if self.orderbook_ws:
                    ws_stats = self.orderbook_ws.get_stats()
                    last_age = ws_stats.get("last_msg_age_s", 999)
                    if last_age > 120:  # No data for 2 minutes
                        if not self._engine_stop_alerted.get("WS_stale"):
                            self._engine_stop_alerted["WS_stale"] = True
                            await self.telegram.send(
                                f"⚠️ <b>Orderbook feed stale</b>\n"
                                f"Last message: {last_age:.0f}s ago"
                            )
                    else:
                        self._engine_stop_alerted["WS_stale"] = False

                # ── Daily Telegram Report (midnight UTC) ──
                try:
                    from datetime import datetime, timezone
                    now_utc = datetime.now(timezone.utc)
                    today_str = now_utc.strftime("%Y-%m-%d")
                    # Send once per day, after midnight UTC
                    if now_utc.hour == 0 and _last_daily_report_date != today_str:
                        _last_daily_report_date = today_str
                        await self._send_daily_report(mm_stats, sniper_stats, _equity, _balance)
                except Exception as _dr_err:
                    self.logger.warning(f"Daily report error: {_dr_err}")

            except Exception as e:
                self.logger.error(f"Stats loop error: {e}")

    async def _resolution_monitor_loop(self):
        """Check for resolved markets every 30 min. Alert via Telegram + auto-claim."""
        import httpx
        await asyncio.sleep(60)  # Wait for system init
        self.logger.info("Resolution monitor started (checks every 30 min)")
        _alerted_conditions = set()

        while True:
            try:
                proxy_addr = getattr(self.config, "POLYMARKET_PROXY_ADDRESS", "")
                if not proxy_addr:
                    await asyncio.sleep(1800)
                    continue

                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(
                        f"https://data-api.polymarket.com/positions?user={proxy_addr.lower()}"
                    )
                    if r.status_code != 200:
                        await asyncio.sleep(1800)
                        continue

                    positions = r.json()

                for p in positions:
                    size = float(p.get("size", 0))
                    if size < 0.5:
                        continue

                    title = p.get("title", "?")[:60]
                    condition_id = p.get("conditionId", "")
                    end_date = p.get("endDate", "")
                    cur_price = float(p.get("curPrice", 0))
                    avg_price = float(p.get("avgPrice", 0))
                    cost = size * avg_price
                    value = size * cur_price
                    resolved = p.get("resolved")

                    # Check if market has resolved (price goes to 0 or 1)
                    is_resolved = resolved is not None
                    is_likely_resolved = cur_price >= 0.99 or cur_price <= 0.01

                    # Check if end_date has passed
                    is_expired = False
                    if end_date:
                        try:
                            import datetime
                            end_dt = datetime.datetime.strptime(end_date[:10], "%Y-%m-%d")
                            is_expired = datetime.datetime.utcnow() > end_dt
                        except Exception:
                            pass

                    alert_key = f"{condition_id}_{title}"

                    # Alert on resolution
                    if (is_resolved or (is_expired and is_likely_resolved)) and alert_key not in _alerted_conditions:
                        _alerted_conditions.add(alert_key)
                        won = cur_price >= 0.5
                        pnl = value - cost
                        emoji = "\U0001f3c6" if won else "\U0001f4c9"

                        msg = (
                            f"{emoji} MARKET RESOLVED\n"
                            f"\n"
                            f"{title}\n"
                            f"Outcome: {'WON' if won else 'LOST'}\n"
                            f"Size: {size:.0f} shares\n"
                            f"Cost: ${cost:.2f} | Value: ${value:.2f}\n"
                            f"PnL: {'+'if pnl>=0 else ''}${pnl:.2f}\n"
                        )

                        self.logger.info(f"Resolution detected: {title} -> {'WON' if won else 'LOST'} (${pnl:.2f})")

                        # Send Telegram alert
                        try:
                            await self.telegram.send(msg)
                        except Exception as e:
                            self.logger.error(f"Telegram resolution alert failed: {e}")

                        # Auto-claim if won
                        if won and self.clob:
                            try:
                                token_id = p.get("asset", "")
                                self.logger.info(f"Auto-claim: attempting to redeem {size:.0f} shares of {title}")
                                # Place a sell at $0.99 to claim winnings
                                await self.clob.place_limit_order(
                                    token_id=token_id,
                                    price=0.99,
                                    size=size,
                                    side="SELL",
                                )
                                self.logger.info(f"Auto-claim: sell order placed for {title}")
                            except Exception as e:
                                self.logger.error(f"Auto-claim failed for {title}: {e}")

                    # Alert on upcoming resolution (3 days before)
                    if end_date and not is_expired and alert_key + "_upcoming" not in _alerted_conditions:
                        try:
                            import datetime
                            end_dt = datetime.datetime.strptime(end_date[:10], "%Y-%m-%d")
                            days_left = (end_dt - datetime.datetime.utcnow()).days
                            if 0 <= days_left <= 3:
                                _alerted_conditions.add(alert_key + "_upcoming")
                                msg = (
                                    f"\u23f0 RESOLUTION SOON\n"
                                    f"\n"
                                    f"{title}\n"
                                    f"Resolves in {days_left} day{'s' if days_left != 1 else ''}\n"
                                    f"Size: {size:.0f} | Cost: ${cost:.2f} | Value: ${value:.2f}\n"
                                    f"Current price: ${cur_price:.3f}\n"
                                )
                                await self.telegram.send(msg)
                                self.logger.info(f"Resolution soon alert: {title} in {days_left} days")
                        except Exception:
                            pass

            except Exception as e:
                self.logger.error(f"Resolution monitor error: {e}")

            await asyncio.sleep(1800)  # Check every 30 min

    async def _send_daily_report(self, mm_stats, sniper_stats, equity, balance):
        """Send a daily summary report to Telegram at midnight UTC."""
        from datetime import datetime, timezone, timedelta
        try:
            deposit = self.config.BANKROLL or 507
            total_pnl = equity - deposit if equity > 0 else 0
            roi = ((equity / deposit) - 1) * 100 if deposit > 0 and equity > 0 else 0

            # Yesterday's date for the report title
            yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%b %d, %Y")

            # MM stats
            mm_pnl = mm_stats.get("pnl", {})
            mm_realized = mm_pnl.get("realized", 0)
            mm_unrealized = mm_pnl.get("unrealized", 0)
            mm_total = mm_realized + mm_unrealized
            mm_fills = mm_stats.get("total_fills", 0)
            mm_active = mm_stats.get("active_markets", 0)

            # Sniper stats
            sn_exec = sniper_stats.get("executor", {})
            sn_pnl = sn_exec.get("total_pnl", 0)
            sn_trades = sn_exec.get("completed_trades", 0)
            sn_win_rate = sn_exec.get("win_rate", 0)

            # Position summary from on-chain
            pos_count = 0
            top_gainer = ("—", 0)
            top_loser = ("—", 0)
            try:
                import httpx
                proxy_addr = getattr(self.config, "POLYMARKET_PROXY_ADDRESS", "")
                if proxy_addr:
                    async with httpx.AsyncClient(timeout=10) as client:
                        r = await client.get(
                            f"https://data-api.polymarket.com/positions?user={proxy_addr.lower()}"
                        )
                        if r.status_code == 200:
                            raw = r.json()
                            for p in raw:
                                size = float(p.get("size", 0))
                                if size < 0.5:
                                    continue
                                pos_count += 1
                                avg_price = float(p.get("avgPrice", 0))
                                cur_price = float(p.get("curPrice", avg_price))
                                pnl = (cur_price - avg_price) * size
                                title = p.get("title", p.get("asset", "")[:20])
                                if pnl > top_gainer[1]:
                                    top_gainer = (title[:30], pnl)
                                if pnl < top_loser[1]:
                                    top_loser = (title[:30], pnl)
            except Exception:
                pass

            pnl_emoji = "📈" if total_pnl >= 0 else "📉"
            roi_emoji = "🟢" if roi >= 0 else "🔴"

            msg = (
                f"📊 <b>Daily Report — {yesterday}</b>\n"
                f"{'─' * 28}\n"
                f"\n"
                f"{pnl_emoji} <b>Portfolio</b>\n"
                f"  Equity: <b>${equity:,.2f}</b>\n"
                f"  Deposit: ${deposit:,.0f}\n"
                f"  Total PnL: <b>{'+' if total_pnl >= 0 else ''}{total_pnl:,.2f}</b>\n"
                f"  {roi_emoji} ROI: {'+' if roi >= 0 else ''}{roi:.1f}%\n"
                f"  Free USDC: ${balance:,.2f}\n"
                f"\n"
                f"🤖 <b>Market Maker</b>\n"
                f"  PnL: ${mm_total:,.2f} (R: ${mm_realized:,.2f} / U: ${mm_unrealized:,.2f})\n"
                f"  Fills: {mm_fills} | Active: {mm_active} markets\n"
                f"\n"
                f"🎯 <b>Sniper</b>\n"
                f"  PnL: ${sn_pnl:,.2f} | Trades: {sn_trades}\n"
                f"  Win Rate: {sn_win_rate:.0f}%\n"
                f"\n"
                f"📦 <b>Positions: {pos_count}</b>\n"
                f"  Top Gainer: {top_gainer[0]} (+${top_gainer[1]:,.2f})\n"
                f"  Top Loser: {top_loser[0]} (${top_loser[1]:,.2f})\n"
            )

            await self.telegram.send(msg)
            self.logger.info(f"Daily report sent for {yesterday}")
        except Exception as e:
            self.logger.error(f"Failed to send daily report: {e}")

    async def shutdown(self):
        """Graceful shutdown."""
        self.logger.info("Shutting down PolyEdge v4...")

        # Save state before stopping engines
        try:
            if self.mm:
                self.store.save("mm_state", self.mm.save_state())
                self.logger.info("MM state saved to disk")
            if self.sniper:
                self.store.save("sniper_state", self.sniper.save_state())
                self.logger.info("Sniper state saved to disk")
        except Exception as e:
            self.logger.error(f"State save on shutdown failed: {e}")

        if self.mm:
            await self.mm.stop()
        if self.sniper:
            await self.sniper.stop()
        if self.meta:
            await self.meta.stop()
        if self.feeds:
            await self.feeds.stop()
        if self.orderbook_ws:
            await self.orderbook_ws.close()
        if self.clob:
            await self.clob.disconnect()

        await self.telegram.send("🛑 PolyEdge v4 shutdown complete")
        await self.telegram.stop_polling()
        self.logger.info("Shutdown complete")
