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

STRATEGY_PROFILES = {
    "conservative": {
        "name": "Conservative",
        "description": "Capital preservation — small trades, high reserve (30%)",
        "icon": "\U0001f6e1\ufe0f",
        "env": {
            "RISK_PROFILE": "conservative",
            "MM_KELLY": "0.15",
            "MM_MAX_QUOTE_SIZE": "5",
            "MM_MAX_MARKETS": "2",
            "MM_MAX_POSITION": "30",
            "MM_MAX_INVENTORY": "100",
            "MM_MAX_LOSS": "15",
            "SNIPER_MAX_TRADE": "25",
            "SNIPER_MAX_EXPOSURE": "150",
            "SNIPER_MIN_GAP": "8",
            "SNIPER_MIN_EDGE": "8",
        },
        "risk": {
            "risk_pct": 1.5,
            "max_exposure_pct": 40.0,
            "reserve_pct": 30.0,
            "max_single_market_pct": 10.0,
        }
    },
    "balanced": {
        "name": "Balanced",
        "description": "Default — moderate risk, steady growth (15% reserve)",
        "icon": "\u2696\ufe0f",
        "env": {
            "RISK_PROFILE": "balanced",
            "MM_KELLY": "0.25",
            "MM_MAX_QUOTE_SIZE": "15",
            "MM_MAX_MARKETS": "4",
            "MM_MAX_POSITION": "75",
            "MM_MAX_INVENTORY": "300",
            "MM_MAX_LOSS": "40",
            "SNIPER_MAX_TRADE": "50",
            "SNIPER_MAX_EXPOSURE": "300",
            "SNIPER_MIN_GAP": "5",
            "SNIPER_MIN_EDGE": "5",
        },
        "risk": {
            "risk_pct": 3.0,
            "max_exposure_pct": 65.0,
            "reserve_pct": 15.0,
            "max_single_market_pct": 20.0,
        }
    },
    "aggressive": {
        "name": "Aggressive",
        "description": "Max growth — bigger trades, low reserve (5%)",
        "icon": "\U0001f680",
        "env": {
            "RISK_PROFILE": "aggressive",
            "MM_KELLY": "0.40",
            "MM_MAX_QUOTE_SIZE": "30",
            "MM_MAX_MARKETS": "6",
            "MM_MAX_POSITION": "150",
            "MM_MAX_INVENTORY": "500",
            "MM_MAX_LOSS": "80",
            "SNIPER_MAX_TRADE": "75",
            "SNIPER_MAX_EXPOSURE": "500",
            "SNIPER_MIN_GAP": "3",
            "SNIPER_MIN_EDGE": "3",
        },
        "risk": {
            "risk_pct": 5.0,
            "max_exposure_pct": 85.0,
            "reserve_pct": 5.0,
            "max_single_market_pct": 30.0,
        }
    }
}


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

    async def _calc_equity(self) -> float:
        """Calculate total portfolio equity = positions value + free USDC."""
        import httpx
        proxy_addr = getattr(self.config, "POLYMARKET_PROXY_ADDRESS", "")
        if not proxy_addr:
            return self.config.BANKROLL or 507

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"https://data-api.polymarket.com/positions?user={proxy_addr.lower()}"
                )
                if r.status_code != 200:
                    return self.config.BANKROLL or 507
                positions = r.json()
                total_value = 0
                for p in positions:
                    size = float(p.get("size", 0))
                    cur = float(p.get("curPrice", p.get("avgPrice", 0)))
                    total_value += size * cur

            # Add free USDC from CLOB
            free_usdc = 0
            try:
                free_usdc = await self.clob.get_balance() or 0
            except Exception:
                pass

            return total_value + free_usdc
        except Exception as e:
            self.logger.debug(f"Equity calc error: {e}")
            return self.config.BANKROLL or 507

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
        app.router.add_get("/api/daily_pnl", self._handle_daily_pnl)
        app.router.add_post("/api/meta/run", self._handle_meta_run)
        app.router.add_post("/api/mode", self._handle_mode_toggle)
        app.router.add_get("/api/riskguard", self._handle_riskguard)
        app.router.add_post("/api/riskguard/resume", self._handle_riskguard_resume)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/api/wallet", self._handle_wallet)
        app.router.add_get("/api/positions", self._handle_positions)
        app.router.add_get("/api/token-names", self._handle_token_names)
        app.router.add_post("/api/sell", self._handle_sell)
        app.router.add_post("/api/bulk-sell-losers", self._handle_bulk_sell_losers)
        app.router.add_get("/api/market_profitability", self._handle_market_profitability)
        app.router.add_get("/api/strategy_profile", self._handle_get_strategy_profile)
        app.router.add_post("/api/strategy_profile", self._handle_strategy_profile)

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
        # Bankroll = original deposit (for ROI calculations)
        bankroll = cfg.BANKROLL or 507
        # Equity = positions market value + free USDC (live portfolio value)
        equity = bankroll
        if not cfg.PAPER_MODE and self.clob:
            try:
                equity = await self._calc_equity()
            except Exception:
                pass
        return web.json_response({
            "text": cfg.summary(),
            "structured": {
                "mode": "PAPER" if cfg.PAPER_MODE else "LIVE",
                "bankroll": bankroll,
                "equity": round(equity, 2),
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

    async def _handle_daily_pnl(self, request):
        """Aggregate PnL history into daily buckets + balance tracking."""
        from aiohttp import web
        from datetime import datetime, timezone, timedelta
        from collections import defaultdict

        days = int(request.query.get("days", "30"))
        entries = self.store.load_log("pnl_history", last_n=999999) if self.store else []

        if not entries:
            return web.json_response({"days": [], "summary": {}})

        # Group entries by date (UTC)
        by_date = defaultdict(list)
        for e in entries:
            ts_str = e.get("_ts", "")
            if not ts_str:
                continue
            try:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                date_key = dt.strftime("%Y-%m-%d")
                by_date[date_key].append(e)
            except Exception:
                continue

        # Build daily summaries
        daily = []
        sorted_dates = sorted(by_date.keys())[-days:]
        prev_total = 0
        for date_key in sorted_dates:
            snapshots = by_date[date_key]
            if not snapshots:
                continue

            # Use first and last snapshot of the day
            first = snapshots[0]
            last = snapshots[-1]

            mm_real = last.get("mm_realized", 0)
            mm_unreal = last.get("mm_unrealized", 0)
            sniper = last.get("sniper_pnl", 0)
            tracker_total = mm_real + mm_unreal + sniper
            mm_exp = last.get("mm_exposure", 0)
            sn_exp = last.get("sniper_exposure", 0)
            balance = last.get("balance", 0)

            # Use real equity-based PnL when available, fall back to tracker
            equity = last.get("equity", 0)
            real_pnl = last.get("real_pnl", 0)
            first_equity = first.get("equity", 0)

            if equity > 0 and first_equity > 0:
                # Real PnL from on-chain equity change
                day_pnl = equity - first_equity
                cumulative = real_pnl
            else:
                # Fallback to tracker PnL
                first_total = (first.get("mm_realized", 0) +
                               first.get("mm_unrealized", 0) +
                               first.get("sniper_pnl", 0))
                day_pnl = tracker_total - first_total
                cumulative = tracker_total

            daily.append({
                "date": date_key,
                "day_pnl": round(day_pnl, 2),
                "cumulative_pnl": round(cumulative, 2),
                "mm_realized": round(mm_real, 2),
                "mm_unrealized": round(mm_unreal, 2),
                "sniper_pnl": round(sniper, 2),
                "total_pnl": round(cumulative, 2),
                "exposure": round(mm_exp + sn_exp, 2),
                "equity": round(equity, 2) if equity else None,
                "balance": round(balance, 2) if balance else None,
                "snapshots": len(snapshots),
                "trades": last.get("trade_count", 0),
            })

        # Summary stats
        total_pnl = daily[-1]["total_pnl"] if daily else 0
        best_day = max(daily, key=lambda d: d["day_pnl"]) if daily else None
        worst_day = min(daily, key=lambda d: d["day_pnl"]) if daily else None
        avg_daily = sum(d["day_pnl"] for d in daily) / max(len(daily), 1)
        win_days = sum(1 for d in daily if d["day_pnl"] > 0)

        return web.json_response({
            "days": daily,
            "summary": {
                "total_pnl": round(total_pnl, 2),
                "total_days": len(daily),
                "avg_daily_pnl": round(avg_daily, 2),
                "best_day": {"date": best_day["date"], "pnl": best_day["day_pnl"]} if best_day else None,
                "worst_day": {"date": worst_day["date"], "pnl": worst_day["day_pnl"]} if worst_day else None,
                "win_days": win_days,
                "loss_days": len(daily) - win_days,
                "win_rate": round(win_days / max(len(daily), 1) * 100, 1),
            }
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

    async def _handle_positions(self, request):
        """Fetch all on-chain positions with live P&L."""
        from aiohttp import web
        import httpx

        proxy_addr = getattr(self.config, "POLYMARKET_PROXY_ADDRESS", "")
        if not proxy_addr:
            return web.json_response({"positions": [], "summary": {}})

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"https://data-api.polymarket.com/positions?user={proxy_addr.lower()}"
                )
                if r.status_code != 200:
                    return web.json_response({"positions": [], "error": f"HTTP {r.status_code}"})
                raw = r.json()

            positions = []
            total_cost = 0
            total_value = 0
            total_pnl = 0

            for p in raw:
                size = float(p.get("size", 0))
                avg_price = float(p.get("avgPrice", 0))
                cur_price = float(p.get("curPrice", avg_price))
                title = p.get("title", p.get("asset", "")[:20])
                condition_id = p.get("conditionId", "")

                if size < 0.5:
                    continue

                # Hide dead positions (expired/worthless tokens)
                value_est = size * cur_price
                if value_est < 0.05 and cur_price < 0.001:
                    continue

                cost = size * avg_price
                value = size * cur_price
                pnl = value - cost
                pnl_pct = ((cur_price / avg_price) - 1) * 100 if avg_price > 0 else 0

                total_cost += cost
                total_value += value
                total_pnl += pnl

                # Age & resolution tracking
                end_date = p.get("endDate", "")
                resolved = p.get("resolved")
                created_at = p.get("createdAt", "")

                # Calculate days held (from first seen or createdAt)
                days_held = 0
                if created_at:
                    try:
                        created_dt = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        days_held = (datetime.datetime.now(datetime.timezone.utc) - created_dt).days
                    except Exception:
                        pass

                # Days until resolution
                days_to_resolve = None
                is_expired = False
                if end_date:
                    try:
                        end_dt = datetime.datetime.strptime(end_date[:10], "%Y-%m-%d")
                        now = datetime.datetime.utcnow()
                        delta = (end_dt - now).days
                        days_to_resolve = max(0, delta)
                        is_expired = delta < 0
                    except Exception:
                        pass

                positions.append({
                    "title": title,
                    "token_id": p.get("asset", ""),
                    "condition_id": condition_id,
                    "size": round(size, 1),
                    "avg_price": round(avg_price, 4),
                    "cur_price": round(cur_price, 4),
                    "cost": round(cost, 2),
                    "value": round(value, 2),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 1),
                    "outcome": p.get("outcome", ""),
                    "end_date": end_date,
                    "days_to_resolve": days_to_resolve,
                    "days_held": days_held,
                    "is_expired": is_expired,
                    "resolved": resolved,
                })

            # Sort by absolute value (biggest positions first)
            positions.sort(key=lambda x: abs(x["value"]), reverse=True)

            # Get free USDC
            free_usdc = 0
            try:
                free_usdc = await self.clob.get_balance() or 0
            except Exception:
                pass

            equity = total_value + free_usdc
            deposit = self.config.BANKROLL or 507

            return web.json_response({
                "positions": positions,
                "summary": {
                    "count": len(positions),
                    "total_cost": round(total_cost, 2),
                    "total_value": round(total_value, 2),
                    "total_pnl": round(total_pnl, 2),
                    "total_pnl_pct": round((total_pnl / total_cost * 100) if total_cost > 0 else 0, 1),
                    "free_usdc": round(free_usdc, 2),
                    "equity": round(equity, 2),
                    "deposit": deposit,
                    "roi": round(((equity / deposit) - 1) * 100, 1) if deposit > 0 else 0,
                }
            })
        except Exception as e:
            return web.json_response({"positions": [], "error": str(e)})




    async def _handle_token_names(self, request):
        """Return a complete mapping of token_id -> market name."""
        from aiohttp import web
        import httpx

        proxy_addr = getattr(self.config, "POLYMARKET_PROXY_ADDRESS", "")
        names = {}

        # Source 1: On-chain positions
        if proxy_addr:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(
                        "https://data-api.polymarket.com/positions?user=" + proxy_addr.lower()
                    )
                    if r.status_code == 200:
                        for p in r.json():
                            tid = p.get("asset", "")
                            title = p.get("title", "")
                            if tid and title:
                                names[tid] = title
            except Exception:
                pass

        # Source 2: Active MM markets
        if self.mm:
            for m in getattr(self.mm, "_active_markets", []):
                if m.token_id_yes and m.question:
                    names[m.token_id_yes] = m.question
                if m.token_id_no and m.question:
                    names[m.token_id_no] = m.question + " (No)"

        return web.json_response({"names": names, "count": len(names)})

    async def _handle_sell(self, request):
        """Sell a single position by token_id."""
        from aiohttp import web
        try:
            body = await request.json()
            token_id = body.get("token_id", "")
            size = float(body.get("size", 0))
            title = body.get("title", "?")

            if not token_id or size < 0.5:
                return web.json_response({"ok": False, "error": "Invalid token_id or size"})

            if not self.clob:
                return web.json_response({"ok": False, "error": "CLOB client not available"})

            # Approve conditional tokens for selling (required by Polymarket)
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                clob_client = getattr(getattr(self.clob, "_v4_client", None), "_client", None)  # py_clob_client
                if clob_client:
                    params = BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=token_id,
                    )
                    await __import__("asyncio").to_thread(
                        clob_client.update_balance_allowance, params
                    )
            except Exception as e:
                self.logger.warning(f"Conditional approval warning: {e}")

            # Get best bid from orderbook for smart pricing
            sell_price = 0.001
            try:
                import httpx
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.get(f"https://clob.polymarket.com/book?token_id={token_id}")
                    if r.status_code == 200:
                        book = r.json()
                        bids = book.get("bids", [])
                        if bids:
                            sell_price = max(float(b.get("price", 0)) for b in bids)
            except Exception:
                pass

            if sell_price < 0.002:
                return web.json_response({"ok": False, "error": "No buyers (empty bid side)"})

            order = await self.clob.place_limit_order(token_id, "SELL", sell_price, size)
            order_id = ""
            if order:
                order_id = order if isinstance(order, str) else order.get("id", str(order)[:40])

            self.logger.info(f"MANUAL SELL: {title[:40]} {size:.1f} @ ${sell_price:.4f} -> {order_id}")

            # Telegram notification
            if hasattr(self, 'telegram') and self.telegram:
                try:
                    await self.telegram.send(
                        f"🔧 MANUAL SELL\n{title}\nSELL {size:.1f} @ ${sell_price:.4f}"
                    )
                except Exception:
                    pass

            return web.json_response({
                "ok": True,
                "order_id": str(order_id)[:40],
                "price": sell_price,
                "size": size,
                "title": title,
            })
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    async def _handle_bulk_sell_losers(self, request):
        """Sell all positions that are in loss."""
        from aiohttp import web
        import httpx

        if not self.clob:
            return web.json_response({"ok": False, "error": "CLOB not available"})

        proxy_addr = getattr(self.config, "POLYMARKET_PROXY_ADDRESS", "")
        if not proxy_addr:
            return web.json_response({"ok": False, "error": "No proxy address"})

        try:
            body = await request.json()
            min_loss_pct = float(body.get("min_loss_pct", 0))  # 0 = any loss

            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"https://data-api.polymarket.com/positions?user={proxy_addr.lower()}"
                )
                raw = r.json()

            sold = []
            failed = []
            for p in raw:
                size = float(p.get("size", 0))
                avg_price = float(p.get("avgPrice", 0))
                cur_price = float(p.get("curPrice", avg_price))
                title = p.get("title", "?")
                token_id = p.get("asset", "")

                if size < 0.5 or avg_price <= 0 or cur_price <= 0.001:
                    continue

                pnl_pct = (cur_price - avg_price) / avg_price
                if pnl_pct >= -abs(min_loss_pct) / 100:
                    continue  # Not losing enough

                # Get best bid
                sell_price = 0.001
                try:
                    async with httpx.AsyncClient(timeout=5) as client2:
                        r2 = await client2.get(f"https://clob.polymarket.com/book?token_id={token_id}")
                        if r2.status_code == 200:
                            bids = r2.json().get("bids", [])
                            if bids:
                                sell_price = max(float(b.get("price", 0)) for b in bids)
                except Exception:
                    pass

                if sell_price < 0.002:
                    failed.append({"title": title[:40], "reason": "no buyers"})
                    continue

                try:
                    order = await self.clob.place_limit_order(token_id, "SELL", sell_price, size)
                    order_id = order if isinstance(order, str) else str(order)[:40]
                    sold.append({
                        "title": title[:40],
                        "size": round(size, 1),
                        "price": sell_price,
                        "pnl_pct": round(pnl_pct * 100, 1),
                        "order_id": str(order_id)[:30],
                    })
                    self.logger.info(f"BULK SELL: {title[:40]} {size:.1f} @ ${sell_price:.4f} (pnl {pnl_pct*100:+.1f}%)")
                except Exception as e:
                    failed.append({"title": title[:40], "reason": str(e)[:40]})

            # Telegram summary
            if sold and hasattr(self, 'telegram') and self.telegram:
                try:
                    msg = f"🧹 BULK SELL: {len(sold)} losing positions\n"
                    for s in sold[:5]:
                        msg += f"  {s['title'][:30]} @ ${s['price']:.3f} ({s['pnl_pct']:+.1f}%)\n"
                    await self.telegram.send(msg)
                except Exception:
                    pass

            return web.json_response({
                "ok": True,
                "sold": sold,
                "failed": failed,
                "count_sold": len(sold),
                "count_failed": len(failed),
            })
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    async def _handle_market_profitability(self, request):
        """Per-market profitability breakdown."""
        from aiohttp import web
        if not self.mm:
            return web.json_response({"markets": []})

        inv = self.mm.inventory
        token_names = {}
        for m in self.mm._active_markets:
            token_names[m.token_id_yes] = m.question
            token_names[m.token_id_no] = m.question + " (NO)"

        markets = []
        for tid, pos in inv._positions.items():
            if pos.n_trades == 0:
                continue
            total_pnl = pos.realized_pnl + pos.unrealized_pnl
            markets.append({
                "token_id": tid[:20] + "...",
                "market": token_names.get(tid, tid[:30]),
                "net_position": round(pos.net_position, 1),
                "avg_entry": round(pos.avg_entry, 4),
                "realized_pnl": round(pos.realized_pnl, 2),
                "unrealized_pnl": round(pos.unrealized_pnl, 2),
                "total_pnl": round(total_pnl, 2),
                "trades": pos.n_trades,
                "profitable": total_pnl > 0,
            })

        markets.sort(key=lambda x: x["total_pnl"], reverse=True)

        winners = [m for m in markets if m["total_pnl"] > 0]
        losers = [m for m in markets if m["total_pnl"] <= 0]

        return web.json_response({
            "markets": markets,
            "summary": {
                "total_markets": len(markets),
                "winners": len(winners),
                "losers": len(losers),
                "total_realized": round(sum(m["realized_pnl"] for m in markets), 2),
                "total_unrealized": round(sum(m["unrealized_pnl"] for m in markets), 2),
                "best_market": markets[0] if markets else None,
                "worst_market": markets[-1] if markets else None,
            }
        })

    async def _handle_get_strategy_profile(self, request):
        """Return current strategy profile based on .env values."""
        from aiohttp import web

        env_path = "/root/polyedge/v4/.env"
        try:
            env_vals = {}
            with open(env_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        key, _, val = line.partition("=")
                        env_vals[key.strip()] = val.strip()

            # Use RISK_PROFILE env var directly (simpler than matching all values)
            active_profile = env_vals.get("RISK_PROFILE", "custom").lower()
            if active_profile not in STRATEGY_PROFILES:
                # Fallback: try matching env values
                active_profile = "custom"
                for pid, profile in STRATEGY_PROFILES.items():
                    match = True
                    for key, expected in profile["env"].items():
                        if env_vals.get(key, "").lower() != expected.lower():
                            match = False
                            break
                    if match:
                        active_profile = pid
                        break

            return web.json_response({
                "active": active_profile,
                "active_risk": STRATEGY_PROFILES.get(active_profile, {}).get("risk", {}),
                "profiles": {
                    pid: {
                        "name": p["name"],
                        "description": p["description"],
                        "icon": p["icon"],
                        "env": p["env"],
                    }
                    for pid, p in STRATEGY_PROFILES.items()
                },
            })
        except Exception as e:
            self.logger.error(f"Failed to read strategy profile: {e}")
            return web.json_response({"active": "custom", "error": str(e)})

    async def _handle_strategy_profile(self, request):
        """Switch strategy profile by updating .env file."""
        from aiohttp import web

        try:
            body = await request.json()
        except Exception:
            body = {}

        profile_id = body.get("profile", "").lower()
        if profile_id not in STRATEGY_PROFILES:
            return web.json_response(
                {"error": f"Invalid profile. Use 'conservative', 'balanced', or 'aggressive'."}, status=400
            )

        profile = STRATEGY_PROFILES[profile_id]
        env_path = "/root/polyedge/v4/.env"

        try:
            with open(env_path, "r") as f:
                lines = f.readlines()

            updated_keys = set()
            for i, line in enumerate(lines):
                stripped = line.strip()
                if "=" in stripped and not stripped.startswith("#"):
                    key = stripped.split("=", 1)[0].strip()
                    if key in profile["env"]:
                        lines[i] = f"{key}={profile['env'][key]}\n"
                        updated_keys.add(key)

            # Add any missing keys
            for key, val in profile["env"].items():
                if key not in updated_keys:
                    lines.append(f"{key}={val}\n")

            with open(env_path, "w") as f:
                f.writelines(lines)

            self.logger.warning(
                f"Risk profile changed to {profile['name']}. "
                f"Updated keys: {list(profile['env'].keys())}. Restart required."
            )

            return web.json_response({
                "status": "ok",
                "profile": profile_id,
                "name": profile["name"],
                "message": f"Switched to {profile['name']} profile. Restart PM2 to apply.",
                "restart_required": True,
            })

        except Exception as e:
            self.logger.error(f"Failed to update strategy profile: {e}")
            return web.json_response(
                {"error": f"Failed to update .env: {str(e)}"}, status=500
            )

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
