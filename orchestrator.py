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

            sniper_state = self.store.load("sniper_state")
            if sniper_state:
                self.sniper.load_state(sniper_state)
                self.logger.info("Sniper state restored from disk")
        except Exception as e:
            self.logger.warning(f"Could not restore state: {e} — starting fresh")

        try:
            await asyncio.gather(
                self.mm.start(paper_mode=self.config.PAPER_MODE),
                self.sniper.start(paper_mode=self.config.PAPER_MODE),
                self.meta.start(),
                self.feeds.start(),
                self.dashboard.start(),
                self.telegram.start_polling(),
                self._stats_loop(),
            )
        except asyncio.CancelledError:
            self.logger.info("Shutdown signal received")
        finally:
            await self.shutdown()

    async def _stats_loop(self):
        """Periodically save stats and feed data to meta-strategist."""
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
                self.store.append_log("pnl_history", {
                    "mm_realized": mm_stats.get("pnl", {}).get("realized", 0),
                    "mm_unrealized": mm_stats.get("pnl", {}).get("unrealized", 0),
                    "sniper_pnl": sniper_stats.get("executor", {}).get("total_pnl", 0),
                    "mm_exposure": mm_stats.get("inventory", {}).get("total_exposure", 0),
                    "sniper_exposure": sniper_stats.get("executor", {}).get("total_exposure", 0),
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

            except Exception as e:
                self.logger.error(f"Stats loop error: {e}")

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
