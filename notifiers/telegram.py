"""
PolyEdge v4 — Telegram Notifier
=================================
Telegram bot: sends alerts + handles commands via polling.
Extracted from main_v4.py.
"""

import asyncio
import time
import logging


class TelegramNotifier:
    """Telegram bot: sends alerts + handles commands via polling."""

    def __init__(self, config):
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
