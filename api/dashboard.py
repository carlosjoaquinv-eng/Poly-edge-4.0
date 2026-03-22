"""
PolyEdge v4 — Dashboard API
==============================
Minimal HTTP API for dashboard consumption.
Serves JSON stats from all engines.
Extracted from main_v4.py.
"""

import os
import time
import logging

from engines.market_maker import MarketMakerEngine
from engines.resolution_sniper_v2 import ResolutionSniperV2
from engines.meta_strategist import MetaStrategist
from api.auth import AuthManager

# Project root is one level up from api/
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DashboardAPI:
    """
    Minimal HTTP API for dashboard consumption.
    Serves JSON stats from all engines.

    Endpoints:
      GET /api/status    -> overall system status
      GET /api/mm        -> market maker stats
      GET /api/sniper    -> sniper stats
      GET /api/meta      -> meta-strategist stats
      GET /api/config    -> current config summary
    """

    def __init__(self, config, mm: MarketMakerEngine,
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

    async def start(self):
        """Start the dashboard HTTP server."""
        from aiohttp import web

        middlewares = []

        # ── Auth setup ──
        self.auth_manager = None
        if getattr(self.config, "DASHBOARD_AUTH", True):
            data_dir = os.path.join(PROJECT_ROOT, self.config.DATA_DIR)
            jwt_secret = getattr(self.config, "JWT_SECRET", "") or None
            session_hours = getattr(self.config, "SESSION_HOURS", 72)
            self.auth_manager = AuthManager(
                data_dir=data_dir,
                jwt_secret=jwt_secret,
                session_hours=session_hours,
            )
            middlewares.append(self.auth_manager.make_middleware())
            if self.auth_manager.has_users():
                self.logger.info("Dashboard auth ENABLED (login required)")
            else:
                self.logger.info("Dashboard auth ENABLED (first visit will create account)")
        else:
            self.logger.warning("Dashboard auth DISABLED — set DASHBOARD_AUTH=true to protect it")

        app = web.Application(middlewares=middlewares)

        # Auth routes (login page, login API, logout)
        if self.auth_manager:
            self.auth_manager.register_routes(app)

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
        app.router.add_get("/api/wallet", self._handle_wallet)

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
        dashboard_path = os.path.join(PROJECT_ROOT, "dashboard_v4.html")
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
        # Use live CLOB balance when available, fall back to config
        bankroll = cfg.BANKROLL
        if not cfg.PAPER_MODE and self.clob:
            try:
                live_bal = await self.clob.get_balance()
                if live_bal and live_bal > 0:
                    bankroll = round(live_bal, 2)
            except Exception:
                pass
        return web.json_response({
            "text": cfg.summary(),
            "structured": {
                "mode": "PAPER" if cfg.PAPER_MODE else "LIVE",
                "bankroll": bankroll,
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
        # Collect trades from both MM and Sniper
        mm_trades = []
        if self.mm:
            mm_stats = self.mm.get_stats()
            mm_trades = mm_stats.get("trade_log", [])

        sniper_trades = []
        sniper_summary = {}
        if self.sniper:
            stats = self.sniper.executor.get_stats()
            executor_state = self.sniper.executor.save_state()
            sniper_trades = executor_state.get("completed_trades", [])
            sniper_summary = {
                "total_trades": stats.get("completed_trades", 0),
                "win_rate": stats.get("win_rate", 0),
                "total_pnl": stats.get("total_pnl", 0),
                "avg_pnl": stats.get("avg_pnl", 0),
            }

        # Merge and sort all trades by time (most recent first)
        all_trades = mm_trades + sniper_trades
        all_trades.sort(key=lambda t: t.get("time", t.get("fill_time", 0)), reverse=True)

        return web.json_response({
            "active": [],
            "completed": all_trades[:100],
            "summary": {
                "total_trades": len(all_trades),
                "mm_trades": len(mm_trades),
                "sniper_trades": len(sniper_trades),
                **sniper_summary,
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

            self.logger.warning(f"Mode changed: {current_mode} -> {target_mode} (restart required)")

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

    async def _handle_wallet(self, request):
        """Return bot wallet balances read on-chain (no browser extension needed)."""
        from aiohttp import web
        import asyncio

        proxy = getattr(self.config, "POLYMARKET_PROXY_ADDRESS", "")
        eoa = getattr(self.config, "PRIVATE_KEY", "")

        # Derive EOA address from private key
        eoa_address = ""
        if eoa:
            try:
                from eth_account import Account
                eoa_address = Account.from_key(eoa).address
            except Exception:
                pass

        if not proxy and not eoa_address:
            return web.json_response({
                "error": "No wallet configured",
                "eoa": "", "proxy": "",
            })

        # Read balances on-chain via JSON-RPC
        rpc_url = "https://polygon-bor-rpc.publicnode.com"
        usdc_contract = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e on Polygon

        async def read_balance(address):
            """Read USDC.e balance for an address via eth_call."""
            if not address:
                return 0.0
            try:
                import aiohttp as aio
                # balanceOf(address) selector = 0x70a08231
                padded = address.lower().replace("0x", "").zfill(64)
                call_data = "0x70a08231" + padded
                payload = {
                    "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                    "params": [{"to": usdc_contract, "data": call_data}, "latest"]
                }
                async with aio.ClientSession() as session:
                    async with session.post(rpc_url, json=payload, timeout=aio.ClientTimeout(total=5)) as resp:
                        result = await resp.json()
                        hex_val = result.get("result", "0x0")
                        return int(hex_val, 16) / 1e6  # USDC has 6 decimals
            except Exception as e:
                self.logger.warning(f"Failed to read balance for {address[:10]}: {e}")
                return 0.0

        async def read_matic(address):
            """Read MATIC/POL balance."""
            if not address:
                return 0.0
            try:
                import aiohttp as aio
                payload = {
                    "jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
                    "params": [address, "latest"]
                }
                async with aio.ClientSession() as session:
                    async with session.post(rpc_url, json=payload, timeout=aio.ClientTimeout(total=5)) as resp:
                        result = await resp.json()
                        hex_val = result.get("result", "0x0")
                        return int(hex_val, 16) / 1e18
            except Exception as e:
                self.logger.warning(f"Failed to read MATIC for {address[:10]}: {e}")
                return 0.0

        # Read all balances concurrently
        proxy_usdc, eoa_usdc, proxy_matic, eoa_matic = await asyncio.gather(
            read_balance(proxy),
            read_balance(eoa_address),
            read_matic(proxy),
            read_matic(eoa_address),
        )

        # CLOB balance (what Polymarket API reports)
        clob_balance = 0.0
        if self.clob:
            try:
                clob_balance = await self.clob.get_balance()
            except Exception:
                pass

        return web.json_response({
            "eoa": eoa_address,
            "proxy": proxy,
            "mode": "PAPER" if self.config.PAPER_MODE else "LIVE",
            "balances": {
                "proxy_usdc": round(proxy_usdc, 2),
                "eoa_usdc": round(eoa_usdc, 2),
                "proxy_matic": round(proxy_matic, 4),
                "eoa_matic": round(eoa_matic, 4),
                "clob_reported": round(clob_balance, 2),
                "total_usdc": round(proxy_usdc + eoa_usdc, 2),
            },
            "network": "Polygon",
            "chain_id": 137,
        })

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
            # Build token_id -> market name map from active markets
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
