"""
PolyEdge v4 — CLOB Client
===========================
Async CLOB client for Polymarket.
Thin wrapper that delegates to CLOBClientV4 for live trading,
or uses built-in paper mode.
Extracted from main_v4.py.
"""

import time
import logging


class CLOBClient:
    """
    Async CLOB client for Polymarket.

    Thin wrapper that delegates to CLOBClientV4 for live trading,
    or uses built-in paper mode.

    For production, import CLOBClientV4 from engine/core/clob_client_v4.py:
      from engine.core.clob_client_v4 import CLOBClientV4, CLOBConfig
    """

    def __init__(self, config):
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
            clob_cfg.signature_type = self.config.SIGNATURE_TYPE
            clob_cfg.funder = self.config.POLYMARKET_PROXY_ADDRESS
            clob_cfg.residential_proxy = getattr(self.config, 'RESIDENTIAL_PROXY', '')
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
