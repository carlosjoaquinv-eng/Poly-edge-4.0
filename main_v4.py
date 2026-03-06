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
from datetime import datetime, timezone
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
        self._ws_feed = None  # OrderbookWSFeed instance for real-time data
        self._ws_hits = 0
        self._rest_fallbacks = 0
        self.api_url = config.CLOB_API_URL
        self.gamma_url = config.GAMMA_API_URL
        self.logger = logging.getLogger("polyedge.clob")

    def set_ws_feed(self, ws_feed):
        """Attach WS orderbook feed for real-time data."""
        self._ws_feed = ws_feed
        # Also attach to v4 client if available
        if self._v4_client and hasattr(self._v4_client, 'set_ws_feed'):
            self._v4_client.set_ws_feed(ws_feed)
    
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
        # Try WS cache first (real-time, no network call)
        if self._ws_feed:
            ob = self._ws_feed.get_orderbook(token_id)
            if ob:
                self._ws_hits += 1
                return ob
            # Auto-subscribe on miss so future calls hit WS
            self._ws_feed.subscribe([token_id])

        # Fall back to REST
        self._rest_fallbacks += 1
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

    def get_feed_stats(self) -> dict:
        """Return WS hit rate and fallback counters."""
        total = self._ws_hits + self._rest_fallbacks
        return {
            "ws_hits": self._ws_hits,
            "rest_fallbacks": self._rest_fallbacks,
            "ws_hit_rate": round(self._ws_hits / total * 100, 1) if total > 0 else 0.0,
        }
    
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
    """Telegram bot: sends alerts + handles commands via polling."""
    
    def __init__(self, config: Config):
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.enabled = bool(self.token and self.chat_id)
        self.logger = logging.getLogger("polyedge.telegram")
        self._last_update_id = 0
        self._polling = False
        self._engines = {}  # Set after init: {"mm": ..., "sniper": ..., "meta": ...}
        self._config = config
        self._start_time = time.time()
        self._ws_feed = None  # Set via set_ws_feed()
        self._store = None    # Set via set_store()

    def set_store(self, store):
        """Called after DataStore is created so /history can access it."""
        self._store = store

    def set_engines(self, mm, sniper, meta):
        """Called after engines are created so commands can access them."""
        self._engines = {"mm": mm, "sniper": sniper, "meta": meta}

    def set_ws_feed(self, ws_feed):
        """Set WS feed reference for /feeds command."""
        self._ws_feed = ws_feed
    
    async def send(self, message: str):
        if not self.enabled:
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
            self.logger.error(f"Telegram send error: {e}")
    
    async def start_polling(self):
        """Start polling for commands in background."""
        if not self.enabled:
            return
        self._polling = True
        await self._register_commands()
        self.logger.info("Telegram command polling started")
        while self._polling:
            try:
                await self._poll_updates()
            except Exception as e:
                self.logger.error(f"Telegram poll error: {e}")
            await asyncio.sleep(2)
    
    async def stop_polling(self):
        self._polling = False
    
    async def _register_commands(self):
        """Register bot commands with Telegram."""
        import aiohttp
        commands = [
            {"command": "status", "description": "📊 System overview"},
            {"command": "mm", "description": "🏪 Market Maker stats"},
            {"command": "sniper", "description": "🎯 Sniper stats"},
            {"command": "meta", "description": "🧠 Meta Strategist"},
            {"command": "quotes", "description": "📝 Active quotes"},
            {"command": "positions", "description": "📋 Sniper positions"},
            {"command": "pnl", "description": "💰 P&L summary"},
            {"command": "feeds", "description": "📡 Feed status"},
            {"command": "run_meta", "description": "🧠 Run Meta analysis now"},
            {"command": "kill", "description": "🛑 Toggle MM kill switch"},
            {"command": "stop", "description": "⏹ STOP all engines"},
            {"command": "resume", "description": "▶️ Resume all engines"},
            {"command": "help", "description": "❓ Show commands"},
        ]
        try:
            url = f"https://api.telegram.org/bot{self.token}/setMyCommands"
            async with aiohttp.ClientSession() as session:
                await session.post(url, json={"commands": commands})
        except Exception:
            pass
    
    async def _poll_updates(self):
        """Poll for new messages."""
        import aiohttp
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        params = {"offset": self._last_update_id + 1, "timeout": 1, "limit": 5}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()
                    for update in data.get("result", []):
                        self._last_update_id = update["update_id"]
                        msg = update.get("message", {})
                        text = msg.get("text", "")
                        chat_id = str(msg.get("chat", {}).get("id", ""))
                        if chat_id == self.chat_id and text.startswith("/"):
                            cmd = text.split()[0].split("@")[0].lower()
                            await self._handle_command(cmd)
        except Exception:
            pass
    
    async def _handle_command(self, cmd: str):
        """Route command to handler."""
        handlers = {
            "/status": self._cmd_status,
            "/mm": self._cmd_mm,
            "/sniper": self._cmd_sniper,
            "/meta": self._cmd_meta,
            "/run_meta": self._cmd_run_meta,
            "/quotes": self._cmd_quotes,
            "/positions": self._cmd_positions,
            "/pnl": self._cmd_pnl,
            "/feeds": self._cmd_feeds,
            "/kill": self._cmd_kill,
            "/stop": self._cmd_stop,
            "/resume": self._cmd_resume,
            "/history": self._cmd_history,
            "/help": self._cmd_help,
            "/start": self._cmd_help,
        }
        handler = handlers.get(cmd, self._cmd_unknown)
        try:
            await handler()
        except Exception as e:
            await self.send(f"⚠️ Error: {e}")
    
    async def _cmd_status(self):
        mm = self._engines.get("mm")
        sn = self._engines.get("sniper")
        mt = self._engines.get("meta")
        
        uptime_h = (time.time() - self._start_time) / 3600
        mode = "📝 PAPER" if self._config.PAPER_MODE else "🔴 LIVE"
        
        mm_stats = mm.get_stats() if mm else {}
        sn_stats = sn.get_stats() if sn else {}
        mt_stats = mt.get_stats() if mt else {}
        
        pnl_mm = mm_stats.get("pnl", {}).get("total", 0)
        pnl_sn = sn_stats.get("executor", {}).get("total_pnl", 0)
        total_pnl = pnl_mm + pnl_sn
        
        msg = (
            f"<b>⚡ PolyEdge v{self._config.VERSION}</b> {mode}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 Bankroll: <b>${self._config.BANKROLL:,.0f}</b>\n"
            f"📈 PnL: <b>${total_pnl:+,.2f}</b>\n"
            f"⏱ Uptime: {uptime_h:.1f}h\n"
            f"\n"
            f"🏪 MM: {mm_stats.get('active_markets', 0)} markets, "
            f"{mm_stats.get('quote_count', 0)} quotes, "
            f"{mm_stats.get('fill_count', 0)} fills\n"
            f"🎯 Sniper: {sn_stats.get('events_processed', 0)} events, "
            f"{sn_stats.get('gaps_detected', 0)} gaps, "
            f"{sn_stats.get('trades_executed', 0)} trades\n"
            f"🧠 Meta: {mt_stats.get('run_count', 0)} runs, "
            f"next {mt_stats.get('next_run_in_hours', '?')}h\n"
            f"\n"
            f"🔗 <a href='http://178.156.253.21:8081'>Dashboard</a>"
        )
        await self.send(msg)
    
    async def _cmd_mm(self):
        mm = self._engines.get("mm")
        if not mm:
            await self.send("❌ MM not available")
            return
        s = mm.get_stats()
        kill = "🔴 ON" if s.get("kill_switch") else "🟢 OFF"
        
        markets_txt = ""
        for m in s.get("markets", [])[:5]:
            q = m.get("question", "?")[:40]
            markets_txt += f"\n  • {q}  [{m.get('spread')}]"
        
        msg = (
            f"<b>🏪 Market Maker</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Markets: {s.get('active_markets', 0)} | Quotes: {s.get('quote_count', 0)}\n"
            f"Fills: {s.get('fill_count', 0)} | Kill: {kill}\n"
            f"PnL: ${s.get('pnl',{}).get('total',0):+.2f}\n"
            f"Spread captured: ${s.get('spread_captured',0):.2f}\n"
            f"\n<b>Markets:</b>{markets_txt}"
        )
        await self.send(msg)
    
    async def _cmd_sniper(self):
        sn = self._engines.get("sniper")
        if not sn:
            await self.send("❌ Sniper not available")
            return
        s = sn.get_stats()
        mp = s.get("mapper", {})
        gd = s.get("gap_detector", {})
        ex = s.get("executor", {})
        
        msg = (
            f"<b>🎯 Resolution Sniper</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Markets: {mp.get('total_markets', 0)} | "
            f"Teams: {mp.get('football_teams', 0)} | "
            f"Crypto: {mp.get('crypto_symbols', 0)}\n"
            f"Events: {s.get('events_processed', 0)} | "
            f"Gaps: {gd.get('total_signals', 0)}\n"
            f"Trades: {ex.get('completed_trades', 0)} | "
            f"WR: {ex.get('win_rate', 0):.0f}%\n"
            f"PnL: ${ex.get('total_pnl', 0):+.2f}\n"
            f"Active positions: {ex.get('active_trades', 0)}"
        )
        await self.send(msg)
    
    async def _cmd_meta(self):
        mt = self._engines.get("meta")
        if not mt:
            await self.send("❌ Meta not available")
            return
        s = mt.get_stats()
        
        msg = (
            f"<b>🧠 Meta Strategist</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Model: {s.get('model', '?')}\n"
            f"Runs: {s.get('run_count', 0)} | "
            f"Next: {s.get('next_run_in_hours', '?')}h\n"
            f"LLM cost: ${s.get('total_llm_cost', 0):.4f}\n"
            f"Auto-apply: {'✅' if s.get('auto_apply') else '❌'}\n"
            f"Adjustments: {len(s.get('adjustment_history', []))}"
        )
        
        # Last report summary
        reports = s.get("recent_reports", [])
        if reports:
            last = reports[-1]
            msg += f"\n\nLast report:\n{str(last.get('summary', ''))[:200]}"
        
        await self.send(msg)
    
    async def _cmd_run_meta(self):
        mt = self._engines.get("meta")
        if not mt:
            await self.send("❌ Meta not available")
            return
        await self.send("🧠 Running Meta analysis... (may take 10-30s)")
        try:
            mm = self._engines.get("mm")
            sniper = self._engines.get("sniper")
            mm_stats = mm.get_stats() if mm else {}
            sniper_stats = sniper.get_stats() if sniper else {}
            report = await mt.run_analysis(mm_stats, sniper_stats)
            if report:
                adj_text = "\n".join(
                    f"  {a.engine}.{a.parameter}: {a.current_value} → {a.new_value} ({a.confidence:.0f}%)"
                    for a in report.adjustments
                ) or "  None"
                recs = "\n".join(f"  • {r}" for r in report.recommendations[:5]) or "  None"
                msg = (
                    f"<b>🧠 Meta Analysis #{mt._run_count}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"{report.summary[:300]}\n\n"
                    f"<b>Risk:</b> {report.risk_assessment[:200]}\n\n"
                    f"<b>Adjustments:</b>\n{adj_text}\n\n"
                    f"<b>Recommendations:</b>\n{recs}\n\n"
                    f"Cost: ${report.cost_usd:.4f}"
                )
                await self.send(msg)
            else:
                await self.send("⚠️ Analysis returned no report. Check logs.")
        except Exception as e:
            await self.send(f"❌ Meta run failed: {e}")

    async def _cmd_quotes(self):
        mm = self._engines.get("mm")
        if not mm:
            await self.send("❌ MM not available")
            return
        s = mm.get_stats()
        quotes = s.get("quotes", {})
        
        if not quotes:
            await self.send("📝 No active quotes")
            return
        
        msg = "<b>📝 Active Quotes</b>\n━━━━━━━━━━━━━━━━━━\n"
        for tid, q in list(quotes.items())[:8]:
            name = q.get("question", tid[:12])[:35]
            msg += f"\n<b>{name}</b>\n  Bid: {q['bid']} | Ask: {q['ask']} | FV: {q['fair_value']}\n"
        
        await self.send(msg)
    
    async def _cmd_positions(self):
        sn = self._engines.get("sniper")
        if not sn:
            await self.send("❌ Sniper not available")
            return
        s = sn.get_stats()
        positions = s.get("executor", {}).get("active_positions", [])
        
        if not positions:
            await self.send("📋 No active sniper positions")
            return
        
        msg = "<b>📋 Sniper Positions</b>\n━━━━━━━━━━━━━━━━━━\n"
        for p in positions[:10]:
            msg += (
                f"\n• {p.get('market','?')[:35]}\n"
                f"  {p.get('side','?')} @ {p.get('entry',0):.3f} "
                f"${p.get('size',0):.0f} | PnL: ${p.get('pnl',0):+.2f}\n"
            )
        
        await self.send(msg)
    
    async def _cmd_pnl(self):
        mm = self._engines.get("mm")
        sn = self._engines.get("sniper")
        
        mm_s = mm.get_stats() if mm else {}
        sn_s = sn.get_stats() if sn else {}
        
        mm_pnl = mm_s.get("pnl", {})
        sn_ex = sn_s.get("executor", {})
        
        mm_total = mm_pnl.get("total", 0)
        sn_total = sn_ex.get("total_pnl", 0)
        grand = mm_total + sn_total
        
        roi = (grand / self._config.BANKROLL * 100) if self._config.BANKROLL else 0
        
        msg = (
            f"<b>💰 P&L Summary</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🏪 MM:\n"
            f"  Realized: ${mm_pnl.get('realized', 0):+.2f}\n"
            f"  Unrealized: ${mm_pnl.get('unrealized', 0):+.2f}\n"
            f"  Fills: {mm_s.get('fill_count', 0)}\n"
            f"\n"
            f"🎯 Sniper:\n"
            f"  Total: ${sn_total:+.2f}\n"
            f"  Trades: {sn_ex.get('completed_trades', 0)}\n"
            f"  WR: {sn_ex.get('win_rate', 0):.0f}%\n"
            f"\n"
            f"<b>Total: ${grand:+.2f} ({roi:+.1f}% ROI)</b>"
        )
        await self.send(msg)
    
    async def _cmd_feeds(self):
        ws_status = "❌"
        if self._ws_feed:
            stats = self._ws_feed.get_stats()
            if stats.get("connected"):
                ws_status = f"✅ {stats.get('subscribed', 0)} tokens, {stats.get('book_updates', 0)} updates"
            else:
                ws_status = f"🔄 reconnecting ({stats.get('reconnects', 0)} attempts)"
        msg = (
            f"<b>📡 Feed Status</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📖 Orderbook WS: {ws_status}\n"
            f"⚽ Football: {'✅' if self._config.FOOTBALL_API_KEY else '❌'}\n"
            f"₿ Crypto: ✅ {', '.join(self._config.CRYPTO_SYMBOLS)}\n"
            f"📊 Dashboard: :{self._config.DASHBOARD_PORT}\n"
            f"🧠 Anthropic: {'✅' if self._config.ANTHROPIC_API_KEY else '❌'}"
        )
        await self.send(msg)
    
    async def _cmd_kill(self):
        """Toggle MM kill switch only."""
        mm = self._engines.get("mm")
        if not mm:
            await self.send("❌ MM not available")
            return
        
        current = mm._force_kill
        mm._force_kill = not current
        new_state = "🔴 ACTIVATED" if not current else "🟢 DEACTIVATED"
        await self.send(f"🛑 MM Kill switch {new_state}")
    
    async def _cmd_stop(self):
        """STOP ALL — freeze MM + Sniper + Meta. No new trades, no new quotes."""
        mm = self._engines.get("mm")
        sn = self._engines.get("sniper")
        mt = self._engines.get("meta")
        
        stopped = []
        if mm:
            mm._force_kill = True
            stopped.append("MM")
        if sn:
            sn._paused = True
            stopped.append("Sniper")
        if mt:
            mt._paused = True
            stopped.append("Meta")
        
        self._all_stopped = True
        engines_txt = ", ".join(stopped) if stopped else "none"
        await self.send(
            f"🛑 <b>ALL ENGINES STOPPED</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Frozen: {engines_txt}\n"
            f"No new trades or quotes will be placed.\n"
            f"Existing positions remain open.\n\n"
            f"Type /resume to reactivate."
        )
    
    async def _cmd_resume(self):
        """Resume all engines."""
        mm = self._engines.get("mm")
        sn = self._engines.get("sniper")
        mt = self._engines.get("meta")
        
        resumed = []
        if mm:
            mm._force_kill = False
            resumed.append("MM")
        if sn:
            sn._paused = False
            resumed.append("Sniper")
        if mt:
            mt._paused = False
            resumed.append("Meta")
        
        self._all_stopped = False
        engines_txt = ", ".join(resumed) if resumed else "none"
        await self.send(
            f"▶️ <b>ALL ENGINES RESUMED</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Reactivated: {engines_txt}\n"
            f"Trading active again."
        )
    
    async def _cmd_history(self):
        """Show 24h PnL history summary."""
        if not self._store:
            await self.send("❌ DataStore not available")
            return

        # Load last 288 entries (24h at 5-min intervals)
        entries = self._store.load_log("pnl_history", last_n=288)
        if not entries:
            await self.send("📊 No PnL history yet (first snapshot after 5 min)")
            return

        def total_pnl(e):
            return e.get("mm_realized", 0) + e.get("mm_unrealized", 0) + e.get("sniper_pnl", 0)

        pnls = [total_pnl(e) for e in entries]
        start_pnl = pnls[0]
        current_pnl = pnls[-1]
        delta = current_pnl - start_pnl
        peak = max(pnls)
        trough = min(pnls)
        delta_sign = "+" if delta >= 0 else ""

        await self.send(
            f"📊 <b>PnL History ({len(entries)} snapshots)</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Start:   ${start_pnl:.2f}\n"
            f"Current: ${current_pnl:.2f}\n"
            f"Delta:   {delta_sign}${delta:.2f}\n"
            f"Peak:    ${peak:.2f}\n"
            f"Trough:  ${trough:.2f}\n"
            f"Entries: {len(entries)}"
        )

    async def _cmd_help(self):
        msg = (
            f"<b>⚡ PolyEdge v{self._config.VERSION}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"/status — 📊 System overview\n"
            f"/mm — 🏪 Market Maker\n"
            f"/sniper — 🎯 Sniper stats\n"
            f"/meta — 🧠 Meta Strategist\n"
            f"/quotes — 📝 Active quotes\n"
            f"/positions — 📋 Sniper positions\n"
            f"/pnl — 💰 P&L summary\n"
            f"/history — 📈 24h PnL history\n"
            f"/feeds — 📡 Feed status\n"
            f"/kill — 🛑 Toggle MM kill switch\n"
            f"/stop — ⏹ STOP all engines\n"
            f"/resume — ▶️ Resume all engines\n"
        )
        await self.send(msg)
    
    async def _cmd_unknown(self):
        await self.send("❓ Unknown command. Try /help")


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

    def append_log(self, key: str, entry: dict):
        """Append a timestamped entry to a JSONL log file."""
        path = os.path.join(self.data_dir, f"{key}.jsonl")
        try:
            entry["_ts"] = datetime.now(timezone.utc).isoformat()
            with open(path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            self.logger.error(f"Append log error ({key}): {e}")

    def load_log(self, key: str, last_n: int = 100) -> list:
        """Load last N entries from a JSONL log file."""
        path = os.path.join(self.data_dir, f"{key}.jsonl")
        if not os.path.exists(path):
            return []
        try:
            with open(path) as f:
                lines = f.readlines()
            return [json.loads(line) for line in lines[-last_n:]]
        except Exception as e:
            self.logger.error(f"Load log error ({key}): {e}")
            return []


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
                  sniper: ResolutionSniperV2, meta: MetaStrategist,
                  orderbook_ws=None, clob=None, store=None):
        self.config = config
        self.mm = mm
        self.sniper = sniper
        self.meta = meta
        self.orderbook_ws = orderbook_ws
        self.clob = clob
        self.store = store
        self.logger = logging.getLogger("polyedge.dashboard")
        self._started_at = time.time()
    
    def _make_auth_middleware(self):
        """Create HTTP Basic Auth middleware. /health is always public."""
        from aiohttp import web
        import base64

        password = self.config.DASHBOARD_PASSWORD

        @web.middleware
        async def auth_middleware(request, handler):
            # /health is always public (for uptime monitors)
            if request.path == "/health":
                return await handler(request)

            # If no password configured, skip auth
            if not password:
                return await handler(request)

            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Basic "):
                try:
                    decoded = base64.b64decode(auth_header[6:]).decode()
                    _, pwd = decoded.split(":", 1)
                    if pwd == password:
                        return await handler(request)
                except Exception:
                    pass

            return web.Response(
                status=401,
                headers={"WWW-Authenticate": 'Basic realm="PolyEdge v4"'},
                text="Unauthorized",
            )

        return auth_middleware

    async def start(self):
        """Start the dashboard HTTP server."""
        from aiohttp import web

        middlewares = []
        if self.config.DASHBOARD_PASSWORD:
            middlewares.append(self._make_auth_middleware())
            self.logger.info("Dashboard auth ENABLED (password set)")
        else:
            self.logger.warning("Dashboard auth DISABLED — set DASHBOARD_PASSWORD to protect it")

        app = web.Application(middlewares=middlewares)

        # API endpoints
        app.router.add_get("/api/status", self._handle_status)
        app.router.add_get("/api/mm", self._handle_mm)
        app.router.add_get("/api/sniper", self._handle_sniper)
        app.router.add_get("/api/meta", self._handle_meta)
        app.router.add_get("/api/config", self._handle_config)
        app.router.add_get("/api/trades", self._handle_trades)
        app.router.add_get("/api/history", self._handle_history)
        app.router.add_get("/api/pnl", self._handle_pnl)
        app.router.add_get("/api/clean_positions", self._handle_clean_positions)
        app.router.add_post("/api/meta/run", self._handle_meta_run)
        app.router.add_post("/api/mode", self._handle_mode_toggle)
        app.router.add_get("/api/riskguard", self._handle_riskguard)
        app.router.add_post("/api/riskguard/resume", self._handle_riskguard_resume)
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
        ws_stats = self.orderbook_ws.get_stats() if self.orderbook_ws else {}
        clob_stats = self.clob.get_feed_stats() if self.clob and hasattr(self.clob, 'get_feed_stats') else {}
        status = {
            "version": self.config.VERSION,
            "mode": "PAPER" if self.config.PAPER_MODE else "LIVE",
            "uptime_hours": round(uptime / 3600, 1),
            "engines": {
                "market_maker": self.mm.get_stats() if self.mm else {},
                "sniper": self.sniper.get_stats() if self.sniper else {},
                "meta": self.meta.get_stats() if self.meta else {},
            },
            "orderbook_ws": ws_stats,
            "feeds": {**ws_stats, **clob_stats},
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
        cfg = self.config
        return web.json_response({
            "text": cfg.summary(),
            "structured": {
                "mode": "PAPER" if cfg.PAPER_MODE else "LIVE",
                "bankroll": cfg.BANKROLL,
                "mm": {
                    "max_markets": cfg.mm.max_markets,
                    "max_position_per_market": cfg.mm.max_position_per_market,
                    "max_total_inventory": cfg.mm.max_total_inventory,
                    "target_half_spread": cfg.mm.target_half_spread,
                    "kelly_fraction": cfg.mm.kelly_fraction,
                    "max_loss_per_day": cfg.mm.max_loss_per_day,
                },
                "sniper": {
                    "min_gap_cents": cfg.sniper.min_gap_cents,
                    "min_edge_pct": cfg.sniper.min_edge_pct,
                    "max_trade_size": cfg.sniper.max_trade_size,
                    "max_total_exposure": cfg.sniper.max_total_exposure,
                    "kelly_fraction": cfg.sniper.kelly_fraction,
                },
                "meta": {
                    "model": cfg.meta.model,
                    "run_interval_hours": cfg.meta.run_interval_hours,
                    "auto_apply": cfg.meta.auto_apply_adjustments,
                },
            }
        })

    async def _handle_riskguard(self, request):
        from aiohttp import web
        if not self.mm:
            return web.json_response({"error": "MM not available"}, status=503)
        return web.json_response(self.mm.riskguard.get_stats())

    async def _handle_riskguard_resume(self, request):
        from aiohttp import web
        if not self.mm:
            return web.json_response({"error": "MM not available"}, status=503)
        try:
            body = await request.json()
            token_id = body.get("token_id")
            if token_id:
                self.mm.riskguard.resume_market(token_id)
                return web.json_response({"status": "ok", "resumed": token_id})
            else:
                self.mm.riskguard.resume_all()
                return web.json_response({"status": "ok", "resumed": "all"})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def _handle_trades(self, request):
        from aiohttp import web
        if not self.sniper:
            return web.json_response({"active": [], "completed": [], "summary": {}})
        stats = self.sniper.executor.get_stats()
        executor_state = self.sniper.executor.save_state()
        return web.json_response({
            "active": stats.get("active_positions", []),
            "completed": executor_state.get("completed_trades", []),
            "summary": {
                "total_trades": stats.get("completed_trades", 0),
                "win_rate": stats.get("win_rate", 0),
                "total_pnl": stats.get("total_pnl", 0),
                "avg_pnl": stats.get("avg_pnl", 0),
            }
        })

    async def _handle_mode_toggle(self, request):
        """Toggle between PAPER and LIVE mode. Requires restart to take effect."""
        from aiohttp import web
        import json

        try:
            body = await request.json()
        except Exception:
            body = {}

        target_mode = body.get("mode", "").upper()
        if target_mode not in ("PAPER", "LIVE"):
            return web.json_response(
                {"error": "Invalid mode. Use 'PAPER' or 'LIVE'."}, status=400
            )

        current_mode = "PAPER" if self.config.PAPER_MODE else "LIVE"
        if target_mode == current_mode:
            return web.json_response({
                "status": "no_change",
                "mode": current_mode,
                "message": f"Already in {current_mode} mode.",
            })

        # Check credentials before allowing switch to LIVE
        if target_mode == "LIVE":
            missing = []
            if not self.config.PRIVATE_KEY:
                missing.append("PRIVATE_KEY")
            if not getattr(self.config, "POLYMARKET_API_KEY", ""):
                missing.append("POLYMARKET_API_KEY")
            if not getattr(self.config, "POLYMARKET_API_SECRET", ""):
                missing.append("POLYMARKET_API_SECRET")
            if missing:
                return web.json_response({
                    "error": f"Cannot switch to LIVE: missing credentials: {', '.join(missing)}",
                    "missing": missing,
                }, status=400)

        # Update .env file on disk
        env_path = "/root/polyedge/v4/.env"
        try:
            with open(env_path, "r") as f:
                lines = f.readlines()

            new_value = "false" if target_mode == "LIVE" else "true"
            updated = False
            for i, line in enumerate(lines):
                if line.strip().startswith("PAPER_MODE="):
                    lines[i] = f"PAPER_MODE={new_value}\n"
                    updated = True
                    break
            if not updated:
                lines.append(f"PAPER_MODE={new_value}\n")

            with open(env_path, "w") as f:
                f.writelines(lines)

            self.logger.warning(f"🔄 Mode changed: {current_mode} → {target_mode} (restart required)")

            # Send Telegram alert
            if self.telegram:
                emoji = "🟢" if target_mode == "LIVE" else "🟡"
                await self.telegram.send(
                    f"{emoji} <b>Mode changed → {target_mode}</b>\n"
                    f"Previous: {current_mode}\n"
                    f"⚠️ Restart required to apply."
                )

            return web.json_response({
                "status": "ok",
                "previous_mode": current_mode,
                "new_mode": target_mode,
                "message": f"Switched to {target_mode}. Restart PM2 to apply.",
                "restart_required": True,
            })

        except Exception as e:
            self.logger.error(f"Failed to update mode: {e}")
            return web.json_response(
                {"error": f"Failed to update .env: {str(e)}"}, status=500
            )

    async def _handle_health(self, request):
        from aiohttp import web
        return web.json_response({"status": "ok", "version": self.config.VERSION})

    async def _handle_history(self, request):
        from aiohttp import web
        n = int(request.query.get("n", "288"))  # Default: 24h of 5-min snapshots
        entries = self.store.load_log("pnl_history", last_n=n) if self.store else []
        return web.json_response(entries)

    async def _handle_pnl(self, request):
        """Consolidated PnL from Market Maker + Sniper."""
        from aiohttp import web

        # --- Market Maker PnL ---
        mm_pnl = {"realized": 0, "unrealized": 0, "total": 0, "exposure": 0, "positions": {}}
        if self.mm:
            inv = self.mm.inventory
            # Build token_id → market name map from active markets
            token_names = {}
            for m in self.mm._active_markets:
                token_names[m.token_id_yes] = m.question
                token_names[m.token_id_no] = m.question + " (NO)"

            positions = {}
            for tid, p in inv._positions.items():
                if abs(p.net_position) > 0.001 or p.n_trades > 0:
                    positions[tid] = {
                        "market": token_names.get(tid, tid[:16] + "..."),
                        "net": round(p.net_position, 4),
                        "avg_entry": round(p.avg_entry, 4),
                        "rpnl": round(p.realized_pnl, 4),
                        "upnl": round(p.unrealized_pnl, 4),
                        "trades": p.n_trades,
                        "last_trade": p.last_trade_at,
                    }
            mm_pnl = {
                "realized": round(inv.total_realized_pnl, 4),
                "unrealized": round(inv.total_unrealized_pnl, 4),
                "total": round(inv.total_realized_pnl + inv.total_unrealized_pnl, 4),
                "daily": round(inv.daily_pnl, 4),
                "exposure": round(inv.total_exposure, 4),
                "kill_switch": inv.check_kill_switch(),
                "positions": positions,
            }

        # --- Sniper PnL ---
        sniper_pnl = {"total_pnl": 0, "completed_trades": 0, "win_rate": 0, "active_exposure": 0}
        if self.sniper:
            ex = self.sniper.executor
            completed = ex._completed_trades
            wins = [t for t in completed if t.pnl > 0]
            sniper_pnl = {
                "total_pnl": round(sum(t.pnl for t in completed), 4),
                "completed_trades": len(completed),
                "win_rate": round(len(wins) / max(len(completed), 1) * 100, 1),
                "avg_pnl": round(sum(t.pnl for t in completed) / max(len(completed), 1), 4),
                "active_trades": len(ex._active_trades),
                "active_exposure": round(ex._total_exposure, 4),
            }

        # --- Combined ---
        combined_total = mm_pnl.get("total", 0) + sniper_pnl.get("total_pnl", 0)

        return web.json_response({
            "market_maker": mm_pnl,
            "sniper": sniper_pnl,
            "combined": {
                "total_pnl": round(combined_total, 4),
                "mm_contribution": mm_pnl.get("total", 0),
                "sniper_contribution": sniper_pnl.get("total_pnl", 0),
            },
            "timestamp": time.time(),
        })

    async def _handle_clean_positions(self, request):
        """List stale positions; ?force=true removes them from the tracker."""
        from aiohttp import web

        if not self.mm:
            return web.json_response({"error": "MM engine not available"}, status=503)

        inv = self.mm.inventory
        force = request.query.get("force", "").lower() == "true"

        # Build set of token_ids belonging to currently active markets
        active_tids = set()
        for m in self.mm._active_markets:
            active_tids.add(m.token_id_yes)
            active_tids.add(m.token_id_no)

        # NOTE: Do NOT include _active_quotes here — MM quotes tokens
        # from inventory too, which creates a circular dependency that
        # prevents orphan detection. Only _active_markets is authoritative.

        # Build token name map
        token_names = {}
        for m in self.mm._active_markets:
            token_names[m.token_id_yes] = m.question
            token_names[m.token_id_no] = m.question + " (NO)"

        # Find stale positions: in inventory but not in any active market
        stale = {}
        for tid, p in list(inv._positions.items()):
            if tid not in active_tids and (abs(p.net_position) > 0.001 or p.n_trades > 0):
                stale[tid] = {
                    "market": token_names.get(tid, tid[:16] + "..."),
                    "net": round(p.net_position, 4),
                    "rpnl": round(p.realized_pnl, 4),
                    "upnl": round(p.unrealized_pnl, 4),
                    "trades": p.n_trades,
                    "last_trade": p.last_trade_at,
                }

        removed = []
        if force and stale:
            for tid in stale:
                del inv._positions[tid]
                removed.append(tid)
            self.logger.info(f"Cleaned {len(removed)} stale positions: {[stale[t]['market'] for t in removed]}")

        return web.json_response({
            "stale_positions": stale,
            "stale_count": len(stale),
            "active_token_count": len(active_tids),
            "force": force,
            "removed": removed,
            "removed_count": len(removed),
        })

    async def _handle_meta_run(self, request):
        """Trigger a manual Meta Strategist analysis run."""
        from aiohttp import web
        import asyncio

        if not self.meta:
            return web.json_response({"error": "Meta engine not available"}, status=503)

        try:
            # Get fresh stats for the analysis
            mm_stats = self.mm.get_stats() if self.mm else {}
            sniper_stats = self.sniper.get_stats() if self.sniper else {}

            report = await self.meta.run_analysis(mm_stats, sniper_stats)

            if report:
                return web.json_response({
                    "status": "ok",
                    "run_count": self.meta._run_count,
                    "summary": report.summary,
                    "risk_assessment": report.risk_assessment,
                    "adjustments": [
                        {
                            "engine": a.engine,
                            "parameter": a.parameter,
                            "current": a.current_value,
                            "recommended": a.new_value,
                            "reason": a.reason,
                            "confidence": a.confidence,
                        }
                        for a in report.adjustments
                    ],
                    "recommendations": report.recommendations,
                    "cost_usd": round(report.cost_usd, 4),
                })
            else:
                return web.json_response({
                    "status": "error",
                    "message": "Analysis returned no report (check logs)",
                }, status=500)

        except Exception as e:
            self.logger.error(f"Manual meta run failed: {e}")
            return web.json_response({
                "status": "error",
                "message": str(e),
            }, status=500)


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
                self.logger.info("✅ MM state restored from disk")
            
            sniper_state = self.store.load("sniper_state")
            if sniper_state:
                self.sniper.load_state(sniper_state)
                self.logger.info("✅ Sniper state restored from disk")
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
                self.logger.info("💾 MM state saved to disk")
            if self.sniper:
                self.store.save("sniper_state", self.sniper.save_state())
                self.logger.info("💾 Sniper state saved to disk")
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
