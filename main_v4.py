"""
PolyEdge v4 — Main Orchestrator
=================================
Runs all 3 engines + feeds concurrently:
  - Engine 1: Market Maker (spread capture)
  - Engine 2: Resolution Sniper v2 (event-driven)
  - Engine 3: Meta Strategist (LLM optimizer every 12h)
  - Football Feed (live match events)
  - Crypto Feed (price thresholds)
  - Dashboard API (stats + monitoring)

Usage:
  python main_v4.py              # Paper mode (default)
  PAPER_MODE=false python main_v4.py  # Live mode (requires funded wallet)
  
PM2:
  pm2 start main_v4.py --name polyedge-v4 --interpreter python3
"""

import asyncio
import signal
import sys
import os
import time
import json
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config_v4 import Config
from engines.market_maker import MarketMakerEngine
from engines.resolution_sniper_v2 import ResolutionSniperV2, FeedEvent
from engines.meta_strategist import MetaStrategist


# ─────────────────────────────────────────────
# Logging Setup
# ─────────────────────────────────────────────

def setup_logging(config: Config):
    """Configure logging with file rotation and console output."""
    os.makedirs(os.path.dirname(config.LOG_FILE) or "logs", exist_ok=True)
    
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # File handler (rotating, 10MB max, 5 backups)
    fh = RotatingFileHandler(config.LOG_FILE, maxBytes=10_000_000, backupCount=5)
    fh.setFormatter(fmt)
    
    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))
    root.addHandler(fh)
    root.addHandler(ch)
    
    # Quiet noisy libraries
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


# ─────────────────────────────────────────────
# Stub classes for shared infrastructure
# (These wrap existing v3.1 code or provide minimal implementations)
# ─────────────────────────────────────────────

class CLOBClient:
    """
    Async CLOB client for Polymarket.
    
    Thin wrapper that delegates to CLOBClientV4 for live trading,
    or uses built-in paper mode.
    
    For production, import CLOBClientV4 from engine/core/clob_client_v4.py:
      from engine.core.clob_client_v4 import CLOBClientV4, CLOBConfig
    """
    
    def __init__(self, config: Config):
        self.config = config
        self._v4_client = None
        self._session = None
        self.api_url = config.CLOB_API_URL
        self.gamma_url = config.GAMMA_API_URL
        self.logger = logging.getLogger("polyedge.clob")
    
    async def connect(self):
        try:
            from engine.core.clob_client_v4 import CLOBClientV4, CLOBConfig
            clob_cfg = CLOBConfig()
            clob_cfg.host = self.config.CLOB_API_URL
            clob_cfg.gamma_url = self.config.GAMMA_API_URL
            clob_cfg.chain_id = self.config.CHAIN_ID
            clob_cfg.private_key = self.config.PRIVATE_KEY
            clob_cfg.api_key = self.config.POLYMARKET_API_KEY
            clob_cfg.api_secret = self.config.POLYMARKET_API_SECRET
            clob_cfg.api_passphrase = self.config.POLYMARKET_API_PASSPHRASE
            clob_cfg.paper_mode = self.config.PAPER_MODE
            
            self._v4_client = CLOBClientV4(clob_cfg)
            await self._v4_client.connect()
            self.logger.info("CLOBClientV4 connected (EIP-712 signing ready)")
        except ImportError:
            self.logger.warning("CLOBClientV4 not available — using fallback HTTP client")
            import aiohttp
            self._session = aiohttp.ClientSession()
    
    async def disconnect(self):
        if self._v4_client:
            await self._v4_client.disconnect()
        if self._session:
            await self._session.close()
    
    async def get_markets(self, limit: int = 200) -> list:
        if self._v4_client:
            return await self._v4_client.get_markets(limit)
        try:
            async with self._session.get(
                f"{self.gamma_url}/markets",
                params={"limit": limit, "active": True, "closed": False},
            ) as resp:
                return await resp.json() if resp.status == 200 else []
        except Exception as e:
            self.logger.error(f"get_markets error: {e}")
            return []
    
    async def get_orderbook(self, token_id: str):
        if self._v4_client:
            return await self._v4_client.get_orderbook(token_id)
        try:
            async with self._session.get(
                f"{self.api_url}/book", params={"token_id": token_id},
            ) as resp:
                return await resp.json() if resp.status == 200 else None
        except Exception as e:
            self.logger.error(f"get_orderbook error: {e}")
            return None
    
    async def get_price(self, token_id: str):
        if self._v4_client:
            return await self._v4_client.get_price(token_id)
        try:
            async with self._session.get(
                f"{self.api_url}/price",
                params={"token_id": token_id, "side": "buy"},
            ) as resp:
                if resp.status == 200:
                    return float((await resp.json()).get("price", 0))
                return None
        except Exception as e:
            self.logger.error(f"get_price error: {e}")
            return None
    
    async def place_limit_order(self, token_id, side, price, size):
        if self._v4_client:
            return await self._v4_client.place_limit_order(token_id, side, price, size)
        self.logger.info(f"PAPER: limit {side} {size}@{price} on {token_id[:12]}")
        return {"id": f"paper_{int(time.time()*1000)}", "status": "paper"}
    
    async def place_order(self, token_id, side, size):
        if self._v4_client:
            return await self._v4_client.place_market_order(token_id, side, size)
        self.logger.info(f"PAPER: market {side} ${size:.1f} on {token_id[:12]}")
        return {"id": f"paper_{int(time.time()*1000)}", "status": "paper"}
    
    async def cancel_order(self, order_id):
        if self._v4_client:
            return await self._v4_client.cancel_order(order_id)
        return True
    
    async def cancel_all_orders(self):
        if self._v4_client:
            return await self._v4_client.cancel_all_orders()
        return True
    
    async def get_open_orders(self):
        if self._v4_client:
            return await self._v4_client.get_open_orders()
        return []
    
    async def get_balance(self):
        if self._v4_client:
            return await self._v4_client.get_balance()
        return 5000.0


class TelegramNotifier:
    """Sends notifications via Telegram bot."""
    
    def __init__(self, config: Config):
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.enabled = bool(self.token and self.chat_id)
        self.logger = logging.getLogger("polyedge.telegram")
    
    async def send(self, message: str):
        if not self.enabled:
            self.logger.debug(f"TG (disabled): {message[:80]}")
            return
        
        try:
            import aiohttp
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            async with aiohttp.ClientSession() as session:
                await session.post(url, json={
                    "chat_id": self.chat_id,
                    "text": message[:4096],
                    "parse_mode": "HTML",
                })
        except Exception as e:
            self.logger.error(f"Telegram error: {e}")


class DataStore:
    """Simple JSON-based persistence for v4 state."""
    
    def __init__(self, config: Config):
        self.data_dir = config.DATA_DIR
        os.makedirs(self.data_dir, exist_ok=True)
        self.logger = logging.getLogger("polyedge.store")
    
    def save(self, key: str, data: dict):
        path = os.path.join(self.data_dir, f"{key}.json")
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            self.logger.error(f"Save error ({key}): {e}")
    
    def load(self, key: str) -> Optional[dict]:
        path = os.path.join(self.data_dir, f"{key}.json")
        try:
            if os.path.exists(path):
                with open(path) as f:
                    return json.load(f)
        except Exception as e:
            self.logger.error(f"Load error ({key}): {e}")
        return None


# ─────────────────────────────────────────────
# Feed Bridge
# ─────────────────────────────────────────────

class FeedBridge:
    """
    Bridges existing feeds (football.py, crypto.py) to Engine 2.
    
    Polls feeds and converts their events to FeedEvent objects
    that the Resolution Sniper can consume.
    
    In production, this imports from feeds/ directory.
    """
    
    def __init__(self, config: Config, sniper: ResolutionSniperV2):
        self.config = config
        self.sniper = sniper
        self.logger = logging.getLogger("polyedge.feeds")
        self._running = False
        
        # Feed instances (lazy-loaded)
        self._football = None
        self._crypto = None
    
    async def start(self):
        self._running = True
        self.logger.info("Feed bridge starting...")
        
        tasks = []
        
        # Start football feed if API key is available
        if self.config.FOOTBALL_API_KEY:
            tasks.append(self._football_loop())
            self.logger.info("Football feed enabled")
        else:
            self.logger.info("Football feed disabled (no API key)")
        
        # Crypto feed always runs (no API key needed)
        tasks.append(self._crypto_loop())
        self.logger.info(f"Crypto feed enabled: {self.config.CRYPTO_SYMBOLS}")
        
        if tasks:
            await asyncio.gather(*tasks)
    
    async def stop(self):
        self._running = False
    
    async def _football_loop(self):
        """Poll football feed for live match events."""
        try:
            # Try importing existing feed
            from feeds.football import FootballFeed
            self._football = FootballFeed(self.config.FOOTBALL_API_KEY)
            self.logger.info("Football feed loaded from feeds/football.py")
        except ImportError:
            self.logger.warning("feeds/football.py not found — using stub")
            return
        
        while self._running:
            try:
                events = await self._football.poll()
                for event in events:
                    # Convert to v4 FeedEvent format if needed
                    if hasattr(event, 'event_type'):
                        await self.sniper.on_event(event)
                    else:
                        # Adapt from v3.1 format
                        from engines.resolution_sniper_v2 import EventType as ET
                        fe = FeedEvent(
                            event_type=ET(event.get("type", "CUSTOM")),
                            source="football",
                            timestamp=event.get("timestamp", time.time()),
                            received_at=time.time(),
                            data=event.get("data", {}),
                            keywords=event.get("keywords", []),
                            confidence=event.get("confidence", 0.9),
                        )
                        await self.sniper.on_event(fe)
                
                interval = self.config.FOOTBALL_POLL_LIVE if self._football.has_live_matches() \
                    else self.config.FOOTBALL_POLL_IDLE
                await asyncio.sleep(interval)
                
            except Exception as e:
                self.logger.error(f"Football feed error: {e}")
                await asyncio.sleep(30)
    
    async def _crypto_loop(self):
        """Poll crypto feed for price events."""
        try:
            from feeds.crypto import CryptoFeed
            self._crypto = CryptoFeed(self.config.CRYPTO_SYMBOLS)
            self.logger.info("Crypto feed loaded from feeds/crypto.py")
        except ImportError:
            self.logger.warning("feeds/crypto.py not found — using stub")
            # Stub: just poll Binance directly
            await self._crypto_stub_loop()
            return
        
        while self._running:
            try:
                events = await self._crypto.poll()
                for event in events:
                    if hasattr(event, 'event_type'):
                        await self.sniper.on_event(event)
                
                await asyncio.sleep(self.config.CRYPTO_POLL_INTERVAL)
                
            except Exception as e:
                self.logger.error(f"Crypto feed error: {e}")
                await asyncio.sleep(10)
    
    async def _crypto_stub_loop(self):
        """Minimal crypto feed when feeds/crypto.py isn't available."""
        import aiohttp
        from engines.resolution_sniper_v2 import EventType as ET
        
        prices: dict = {}
        
        while self._running:
            try:
                async with aiohttp.ClientSession() as session:
                    for symbol in self.config.CRYPTO_SYMBOLS:
                        async with session.get(
                            f"https://api.binance.us/api/v3/ticker/price",
                            params={"symbol": symbol},
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                price = float(data["price"])
                                
                                old_price = prices.get(symbol)
                                prices[symbol] = price
                                
                                if old_price:
                                    change_pct = (price - old_price) / old_price * 100
                                    if abs(change_pct) > 5:
                                        event_type = ET.PRICE_PUMP if change_pct > 0 else ET.PRICE_DUMP
                                        event = FeedEvent(
                                            event_type=event_type,
                                            source="crypto",
                                            timestamp=time.time(),
                                            received_at=time.time(),
                                            data={
                                                "symbol": symbol,
                                                "price": price,
                                                "change_pct": change_pct,
                                            },
                                            keywords=[symbol.lower(), symbol.replace("USDT","").lower()],
                                            confidence=0.95,
                                        )
                                        await self.sniper.on_event(event)
                
                await asyncio.sleep(self.config.CRYPTO_POLL_INTERVAL)
                
            except Exception as e:
                self.logger.error(f"Crypto stub error: {e}")
                await asyncio.sleep(10)


# ─────────────────────────────────────────────
# Dashboard API
# ─────────────────────────────────────────────

class DashboardAPI:
    """
    Minimal HTTP API for dashboard consumption.
    Serves JSON stats from all engines.
    
    Endpoints:
      GET /api/status    → overall system status
      GET /api/mm        → market maker stats
      GET /api/sniper    → sniper stats
      GET /api/meta      → meta-strategist stats
      GET /api/config    → current config summary
    """
    
    def __init__(self, config: Config, mm: MarketMakerEngine, 
                  sniper: ResolutionSniperV2, meta: MetaStrategist):
        self.config = config
        self.mm = mm
        self.sniper = sniper
        self.meta = meta
        self.logger = logging.getLogger("polyedge.dashboard")
        self._started_at = time.time()
    
    async def start(self):
        """Start the dashboard HTTP server."""
        from aiohttp import web
        
        app = web.Application()
        
        # API endpoints
        app.router.add_get("/api/status", self._handle_status)
        app.router.add_get("/api/mm", self._handle_mm)
        app.router.add_get("/api/sniper", self._handle_sniper)
        app.router.add_get("/api/meta", self._handle_meta)
        app.router.add_get("/api/config", self._handle_config)
        app.router.add_get("/health", self._handle_health)
        
        # Serve dashboard HTML at root
        app.router.add_get("/", self._handle_dashboard)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.config.DASHBOARD_HOST, self.config.DASHBOARD_PORT)
        await site.start()
        
        self.logger.info(f"Dashboard running on :{self.config.DASHBOARD_PORT}")
    
    async def _handle_dashboard(self, request):
        """Serve the dashboard HTML."""
        from aiohttp import web
        dashboard_path = os.path.join(os.path.dirname(__file__), "dashboard_v4.html")
        if os.path.exists(dashboard_path):
            return web.FileResponse(dashboard_path)
        return web.Response(text="Dashboard HTML not found", status=404)
    
    async def _handle_status(self, request):
        from aiohttp import web
        uptime = time.time() - self._started_at
        status = {
            "version": self.config.VERSION,
            "mode": "PAPER" if self.config.PAPER_MODE else "LIVE",
            "uptime_hours": round(uptime / 3600, 1),
            "engines": {
                "market_maker": self.mm.get_stats() if self.mm else {},
                "sniper": self.sniper.get_stats() if self.sniper else {},
                "meta": self.meta.get_stats() if self.meta else {},
            },
        }
        return web.json_response(status)
    
    async def _handle_mm(self, request):
        from aiohttp import web
        return web.json_response(self.mm.get_stats() if self.mm else {})
    
    async def _handle_sniper(self, request):
        from aiohttp import web
        return web.json_response(self.sniper.get_stats() if self.sniper else {})
    
    async def _handle_meta(self, request):
        from aiohttp import web
        return web.json_response(self.meta.get_stats() if self.meta else {})
    
    async def _handle_config(self, request):
        from aiohttp import web
        return web.json_response({"config": self.config.summary()})
    
    async def _handle_health(self, request):
        from aiohttp import web
        return web.json_response({"status": "ok", "version": self.config.VERSION})


# ─────────────────────────────────────────────
# Main Orchestrator
# ─────────────────────────────────────────────

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
        
        # Engines
        self.mm: Optional[MarketMakerEngine] = None
        self.sniper: Optional[ResolutionSniperV2] = None
        self.meta: Optional[MetaStrategist] = None
        
        # Feed bridge
        self.feeds: Optional[FeedBridge] = None
        
        # Dashboard
        self.dashboard: Optional[DashboardAPI] = None
    
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
        
        self.telegram = TelegramNotifier(self.config)
        self.store = DataStore(self.config)
        
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
        self.dashboard = DashboardAPI(self.config, self.mm, self.sniper, self.meta)
        
        # Start notification
        mode = "PAPER" if self.config.PAPER_MODE else "🔴 LIVE"
        await self.telegram.send(
            f"🚀 PolyEdge v{self.config.VERSION} Starting ({mode})\n"
            f"Bankroll: ${self.config.BANKROLL:,.0f}\n"
            f"Engines: MM + Sniper + Meta\n"
            f"Dashboard: :{self.config.DASHBOARD_PORT}"
        )
        
        # Launch everything
        self.logger.info("Starting all engines...")
        
        try:
            await asyncio.gather(
                self.mm.start(paper_mode=self.config.PAPER_MODE),
                self.sniper.start(paper_mode=self.config.PAPER_MODE),
                self.meta.start(),
                self.feeds.start(),
                self.dashboard.start(),
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
                
                # Persist state
                self.store.save("mm_stats", mm_stats)
                self.store.save("sniper_stats", sniper_stats)
                self.store.save("meta_stats", self.meta.get_stats() if self.meta else {})
                
            except Exception as e:
                self.logger.error(f"Stats loop error: {e}")
    
    async def shutdown(self):
        """Graceful shutdown."""
        self.logger.info("Shutting down PolyEdge v4...")
        
        if self.mm:
            await self.mm.stop()
        if self.sniper:
            await self.sniper.stop()
        if self.meta:
            await self.meta.stop()
        if self.feeds:
            await self.feeds.stop()
        if self.clob:
            await self.clob.disconnect()
        
        await self.telegram.send("🛑 PolyEdge v4 shutdown complete")
        self.logger.info("Shutdown complete")


# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────

def main():
    """Entry point for PolyEdge v4."""
    app = PolyEdgeV4()
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # Handle SIGINT/SIGTERM
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(app.shutdown()))
    
    try:
        loop.run_until_complete(app.start())
    except KeyboardInterrupt:
        loop.run_until_complete(app.shutdown())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
