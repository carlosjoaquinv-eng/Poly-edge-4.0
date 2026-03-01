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
  - price_change: Incremental level updates on order placement/cancellation.
    Each change specifies asset_id, price, size, side (BUY/SELL).
    Merged into cached orderbook: upsert if size>0, remove if size==0.
  - last_trade_price: Last trade price per asset (tracked for reference)
  - tick_size_change: Tick size updates (when price > 0.96 or < 0.04)

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

        # Last trade prices: token_id -> (timestamp, price)
        self._last_trade: Dict[str, tuple] = {}

        # Tick sizes: token_id -> tick_size (str)
        self._tick_sizes: Dict[str, str] = {}

        # Stats
        self._msg_count = 0
        self._book_count = 0
        self._price_change_count = 0
        self._level_merges = 0
        self._reconnect_count = 0
        self._last_msg_time = 0.0

        # Alert callback: called with (event_type, stats_dict)
        # event_type: "disconnect" or "reconnect"
        self._alert_callback = None

    def set_alert_callback(self, callback):
        """Set async callback for disconnect/reconnect alerts."""
        self._alert_callback = callback

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

    def get_last_trade(self, token_id: str) -> Optional[float]:
        """Get last trade price for a token, or None."""
        entry = self._last_trade.get(token_id)
        if not entry:
            return None
        ts, price = entry
        if time.time() - ts > self.STALE_THRESHOLD:
            return None
        return price

    def get_stats(self) -> Dict:
        return {
            "connected": self._connected,
            "subscribed": len(self._subscribed),
            "cached": len(self._cache),
            "messages_total": self._msg_count,
            "book_updates": self._book_count,
            "price_changes": self._price_change_count,
            "level_merges": self._level_merges,
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
                    # Fire reconnect alert if this is a reconnection (not first connect)
                    if consecutive_failures > 0 and self._alert_callback:
                        try:
                            await self._alert_callback("reconnect", self.get_stats())
                        except Exception:
                            pass
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

            # Fire disconnect alert (once per disconnect event)
            if self._alert_callback and consecutive_failures <= 1:
                try:
                    await self._alert_callback("disconnect", self.get_stats())
                except Exception:
                    pass

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
        elif event_type == "last_trade_price":
            self._handle_last_trade(data)
        elif event_type == "tick_size_change":
            self._handle_tick_size_change(data)
        # best_bid_ask events are redundant when we have price_change merge

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
        Process price_change event — merge updated levels into cached orderbook.

        price_change events fire on order placement/cancellation. Each contains
        a `price_changes` array where each entry specifies:
          - asset_id, price, size, side (BUY/SELL), best_bid, best_ask

        Merge logic:
          - BUY side → upsert/remove bid level
          - SELL side → upsert/remove ask level
          - size == "0" → remove the level entirely
          - Keeps bids sorted descending, asks ascending by price
        """
        changes = data.get("price_changes", [])
        if not changes:
            return

        self._price_change_count += 1
        now = time.time()

        for change in changes:
            asset_id = change.get("asset_id", "")
            if not asset_id:
                continue

            cached = self._cache.get(asset_id)
            if not cached:
                continue  # No book to update — wait for full snapshot

            _, ob = cached
            price_str = change.get("price", "")
            size_str = change.get("size", "0")
            side = change.get("side", "")

            if not price_str or not side:
                continue

            if side == "BUY":
                ob["bids"] = self._merge_level(ob.get("bids", []), price_str, size_str, descending=True)
            elif side == "SELL":
                ob["asks"] = self._merge_level(ob.get("asks", []), price_str, size_str, descending=False)

            self._cache[asset_id] = (now, ob)
            self._level_merges += 1

    @staticmethod
    def _merge_level(levels: List[Dict], price: str, size: str, descending: bool) -> List[Dict]:
        """
        Merge a single price level into an existing list of levels.

        - size > 0: insert or update the level at this price
        - size == 0: remove the level at this price
        - Keeps sorted: descending for bids, ascending for asks
        """
        price_f = float(price)
        size_f = float(size)

        # Remove existing level at this price (compare as float for "0.5" vs ".50")
        new_levels = [l for l in levels if abs(float(l.get("price", 0)) - price_f) > 1e-10]

        # Add updated level if size > 0
        if size_f > 0:
            new_levels.append({"price": price, "size": size})

        # Sort: bids descending, asks ascending
        new_levels.sort(key=lambda l: float(l.get("price", 0)), reverse=descending)

        return new_levels

    def _handle_last_trade(self, data: Dict):
        """Track last trade price per asset."""
        asset_id = data.get("asset_id", "")
        price_str = data.get("price", "")
        if asset_id and price_str:
            try:
                self._last_trade[asset_id] = (time.time(), float(price_str))
            except (ValueError, TypeError):
                pass

    def _handle_tick_size_change(self, data: Dict):
        """Track tick size changes (occurs when price > 0.96 or < 0.04)."""
        asset_id = data.get("asset_id", "")
        tick_size = data.get("tick_size", "")
        if asset_id and tick_size:
            old = self._tick_sizes.get(asset_id)
            self._tick_sizes[asset_id] = tick_size
            if old and old != tick_size:
                logger.info(f"Tick size change: {asset_id[:12]}... {old} → {tick_size}")
