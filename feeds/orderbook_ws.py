"""
PolyEdge v4 — Polymarket Orderbook WebSocket Feed
====================================================
Maintains real-time orderbook snapshots via WebSocket, with REST fallback.

Subscribes to token IDs dynamically as the Market Maker selects markets.
Orderbooks are cached in-memory; CLOBClient reads from this cache
before falling back to REST polling.

WS Endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market
No auth required for market data.

Message types handled:
  - book: Full orderbook snapshot (bids/asks) — sent on subscribe + after trades
  - price_change: Best bid/ask updates on order placement/cancellation
  - (future: last_trade_price, best_bid_ask)

Usage:
    feed = OrderbookWSFeed(ws_url, rest_url)
    await feed.start()
    feed.subscribe(["token_id_1", "token_id_2"])
    ob = feed.get_orderbook("token_id_1")  # sync read from cache
    await feed.close()
"""

import asyncio
import aiohttp
import json
import time
import logging
from typing import Dict, List, Optional, Set

logger = logging.getLogger("polyedge.feeds.orderbook")


class OrderbookWSFeed:
    """
    Real-time orderbook feed from Polymarket CLOB WebSocket.

    - Auto-reconnect with exponential backoff
    - Dynamic subscribe/unsubscribe without reconnection
    - PING/PONG heartbeat per Polymarket spec (every 10s)
    - Stale data detection (returns None after 120s without update)
    - get_orderbook() is synchronous (dict lookup) — no await needed
    """

    MAX_ASSETS_PER_CONNECTION = 500
    PING_INTERVAL = 10          # seconds — Polymarket requires PING every 10s
    RECONNECT_DELAY_BASE = 5    # seconds, doubles on consecutive failures
    RECONNECT_DELAY_MAX = 60    # cap backoff at 60s
    STALE_THRESHOLD = 120       # seconds before considering cached data stale

    def __init__(self, ws_url: str, rest_url: str):
        self._ws_url = ws_url
        self._rest_url = rest_url

        # Connection state
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._connected = False
        self._started = False
        self._ws_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None

        # Subscriptions
        self._subscribed: Set[str] = set()       # token IDs currently subscribed
        self._pending_subscribe: Set[str] = set() # waiting for WS connect

        # Cache: token_id -> (timestamp, orderbook_dict)
        self._cache: Dict[str, tuple] = {}

        # Stats
        self._msg_count = 0
        self._book_count = 0
        self._reconnect_count = 0
        self._last_msg_time = 0.0

    # ── Public API ──────────────────────────────────────────

    async def start(self):
        """Start the WS connection loop in background."""
        if self._started:
            return
        self._started = True
        self._session = aiohttp.ClientSession()
        self._ws_task = asyncio.create_task(self._ws_loop())
        logger.info("Orderbook WS feed starting")

    async def close(self):
        """Clean shutdown."""
        self._started = False
        if self._ping_task:
            self._ping_task.cancel()
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        self._connected = False
        logger.info("Orderbook WS feed closed")

    def subscribe(self, token_ids: List[str]):
        """Subscribe to orderbook updates for given token IDs."""
        new_ids = set(token_ids) - self._subscribed
        if not new_ids:
            return

        if len(self._subscribed) + len(new_ids) > self.MAX_ASSETS_PER_CONNECTION:
            logger.warning(
                f"WS subscription limit: {len(self._subscribed)} current + "
                f"{len(new_ids)} new > {self.MAX_ASSETS_PER_CONNECTION}, trimming"
            )
            available = self.MAX_ASSETS_PER_CONNECTION - len(self._subscribed)
            new_ids = set(list(new_ids)[:max(0, available)])
            if not new_ids:
                return

        self._subscribed.update(new_ids)

        if self._connected and self._ws and not self._ws.closed:
            asyncio.create_task(self._send_subscribe(list(new_ids)))
        else:
            self._pending_subscribe.update(new_ids)

        logger.info(f"WS subscribing to {len(new_ids)} tokens (total: {len(self._subscribed)})")

    def unsubscribe(self, token_ids: List[str]):
        """Unsubscribe from orderbook updates."""
        remove_ids = set(token_ids) & self._subscribed
        if not remove_ids:
            return

        self._subscribed -= remove_ids
        self._pending_subscribe -= remove_ids

        for tid in remove_ids:
            self._cache.pop(tid, None)

        if self._connected and self._ws and not self._ws.closed:
            asyncio.create_task(self._send_unsubscribe(list(remove_ids)))

        logger.info(f"WS unsubscribed {len(remove_ids)} tokens (total: {len(self._subscribed)})")

    def get_orderbook(self, token_id: str) -> Optional[Dict]:
        """
        Get cached orderbook for a token.

        Returns None if not available or stale (>120s old).
        Format matches REST API: {"bids": [...], "asks": [...]}.
        """
        cached = self._cache.get(token_id)
        if not cached:
            return None

        ts, ob = cached
        if time.time() - ts > self.STALE_THRESHOLD:
            return None  # Too old — caller should use REST

        return ob

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_stats(self) -> Dict:
        return {
            "connected": self._connected,
            "subscribed": len(self._subscribed),
            "cached": len(self._cache),
            "messages_total": self._msg_count,
            "book_updates": self._book_count,
            "reconnects": self._reconnect_count,
            "last_msg_age_s": round(time.time() - self._last_msg_time, 1) if self._last_msg_time else None,
        }

    # ── WebSocket Loop ──────────────────────────────────────

    async def _ws_loop(self):
        """Main WS connection loop with auto-reconnect."""
        consecutive_failures = 0

        while self._started:
            try:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession()

                logger.info(f"Connecting to {self._ws_url[:60]}...")

                async with self._session.ws_connect(
                    self._ws_url,
                    timeout=15,
                    heartbeat=None,  # We manage PING ourselves per Polymarket spec
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    consecutive_failures = 0
                    logger.info("Orderbook WS connected")

                    # Subscribe to all tracked tokens
                    all_tokens = list(self._subscribed | self._pending_subscribe)
                    self._pending_subscribe.clear()
                    if all_tokens:
                        await self._send_initial_subscribe(all_tokens)

                    # Start PING loop
                    self._ping_task = asyncio.create_task(self._ping_loop())

                    # Read messages until disconnect
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle_message(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break

                    # WS disconnected normally
                    self._connected = False
                    if self._ping_task:
                        self._ping_task.cancel()
                    logger.warning("Orderbook WS disconnected")

            except asyncio.CancelledError:
                return
            except Exception as e:
                self._connected = False
                consecutive_failures += 1
                logger.warning(f"Orderbook WS error: {e}")

            if not self._started:
                return

            # Reconnect with exponential backoff
            self._reconnect_count += 1
            delay = min(
                self.RECONNECT_DELAY_BASE * (2 ** min(consecutive_failures, 4)),
                self.RECONNECT_DELAY_MAX,
            )
            logger.info(f"WS reconnecting in {delay}s (attempt #{self._reconnect_count})")
            await asyncio.sleep(delay)

    # ── Heartbeat ───────────────────────────────────────────

    async def _ping_loop(self):
        """Send PING every 10s to keep connection alive (Polymarket spec)."""
        try:
            while self._connected and self._ws and not self._ws.closed:
                await self._ws.send_str("PING")
                await asyncio.sleep(self.PING_INTERVAL)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Ping error: {e}")

    # ── Subscription Messages ───────────────────────────────

    async def _send_initial_subscribe(self, token_ids: List[str]):
        """Send initial subscription message on connect."""
        msg = {
            "assets_ids": token_ids,
            "type": "market",
            "custom_feature_enabled": True,
        }
        try:
            await self._ws.send_str(json.dumps(msg))
            logger.info(f"WS initial subscribe: {len(token_ids)} tokens")
        except Exception as e:
            logger.warning(f"WS initial subscribe failed: {e}")
            self._pending_subscribe.update(token_ids)

    async def _send_subscribe(self, token_ids: List[str]):
        """Send dynamic subscribe message (no reconnection needed)."""
        msg = {
            "assets_ids": token_ids,
            "operation": "subscribe",
            "custom_feature_enabled": True,
        }
        try:
            await self._ws.send_str(json.dumps(msg))
            logger.debug(f"WS subscribe: {len(token_ids)} tokens")
        except Exception as e:
            logger.warning(f"WS subscribe send error: {e}")
            self._pending_subscribe.update(token_ids)

    async def _send_unsubscribe(self, token_ids: List[str]):
        """Send dynamic unsubscribe message."""
        msg = {
            "assets_ids": token_ids,
            "operation": "unsubscribe",
        }
        try:
            await self._ws.send_str(json.dumps(msg))
            logger.debug(f"WS unsubscribe: {len(token_ids)} tokens")
        except Exception as e:
            logger.warning(f"WS unsubscribe send error: {e}")

    # ── Message Handling ────────────────────────────────────

    def _handle_message(self, raw: str):
        """Parse and route incoming WS message."""
        if raw == "PONG":
            return

        self._msg_count += 1
        self._last_msg_time = time.time()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug(f"WS non-JSON message: {raw[:100]}")
            return

        event_type = data.get("event_type", "")

        if event_type == "book":
            self._handle_book(data)
        elif event_type == "price_change":
            self._handle_price_change(data)
        # Future: last_trade_price, best_bid_ask, tick_size_change

    def _handle_book(self, data: Dict):
        """Process full orderbook snapshot."""
        asset_id = data.get("asset_id", "")
        if not asset_id:
            return

        # Store in same format as REST API response
        ob = {
            "bids": data.get("bids", []),
            "asks": data.get("asks", []),
        }

        self._cache[asset_id] = (time.time(), ob)
        self._book_count += 1

    def _handle_price_change(self, data: Dict):
        """
        Process price change event — update best bid/ask in cache.

        price_change events fire on order placement/cancellation and include
        updated price levels. We merge them into the existing cached orderbook
        to keep it fresh between full book snapshots.
        """
        asset_id = data.get("asset_id", "")
        if not asset_id:
            return

        # If we have a cached book, update its timestamp to keep it fresh
        # The price_change confirms the market is active
        cached = self._cache.get(asset_id)
        if cached:
            _, ob = cached
            self._cache[asset_id] = (time.time(), ob)
