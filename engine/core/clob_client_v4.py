"""
PolyEdge v4 — CLOB Client (Live Trading)
==========================================
Wraps Polymarket's py_clob_client with EIP-712 signed limit orders.

Features:
  - Limit order placement (GTC/FOK/GTD) with proper signing
  - Order cancellation (single, all, by market)
  - Orderbook fetching with caching
  - Price queries (single + batch)
  - Market discovery via Gamma API
  - Paper mode fallback (no signing needed)
  - Rate limiting and retry logic
  - Async wrapper around sync py_clob_client

Requires:
  - PRIVATE_KEY env var (Polygon wallet)
  - POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE
  - py_clob_client >= 0.34
  - Funded USDC balance on Polygon + CLOB approval

Usage:
  client = CLOBClientV4(config)
  await client.connect()
  order = await client.place_limit_order("token_123", "BUY", 0.45, 20.0)
  await client.cancel_order(order["id"])
"""

import asyncio
import aiohttp
import time
import os
import logging
from typing import Dict, List, Optional, Tuple
from functools import partial
from dataclasses import dataclass
from collections import defaultdict

logger = logging.getLogger("polyedge.clob")


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

@dataclass
class CLOBConfig:
    """CLOB client configuration."""
    host: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137  # Polygon mainnet
    
    # Credentials (loaded from env)
    private_key: str = ""
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    
    # Paper mode
    paper_mode: bool = True
    
    # Rate limiting
    max_requests_per_second: float = 5.0
    orderbook_cache_ttl: float = 2.0      # Cache orderbook for 2s
    market_cache_ttl: float = 60.0        # Cache market list for 60s
    
    # Retry
    max_retries: int = 3
    retry_delay: float = 1.0
    
    # Order defaults
    default_order_type: str = "GTC"       # Good Till Cancel
    default_fee_rate_bps: int = 0         # 0 = use server default
    
    def load_from_env(self):
        """Load credentials from environment."""
        self.private_key = os.environ.get("PRIVATE_KEY", "")
        self.api_key = os.environ.get("POLYMARKET_API_KEY", "")
        self.api_secret = os.environ.get("POLYMARKET_API_SECRET", "")
        self.api_passphrase = os.environ.get("POLYMARKET_API_PASSPHRASE", "")
        self.paper_mode = os.environ.get("PAPER_MODE", "true").lower() in ("true", "1", "yes")


# ─────────────────────────────────────────────
# Rate Limiter
# ─────────────────────────────────────────────

class RateLimiter:
    """Simple token bucket rate limiter."""
    
    def __init__(self, rate: float):
        self.rate = rate
        self.tokens = rate
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()
    
    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.rate, self.tokens + elapsed * self.rate)
            self.last_refill = now
            
            if self.tokens < 1:
                wait = (1 - self.tokens) / self.rate
                await asyncio.sleep(wait)
                self.tokens = 0
            else:
                self.tokens -= 1


# ─────────────────────────────────────────────
# Paper Order Simulator
# ─────────────────────────────────────────────

class PaperOrderBook:
    """Simulates order management in paper mode."""
    
    def __init__(self):
        self._orders: Dict[str, Dict] = {}
        self._order_counter = 0
        self._fills: List[Dict] = []
    
    def place_order(self, token_id: str, side: str, price: float, 
                     size: float, order_type: str = "GTC") -> Dict:
        self._order_counter += 1
        order_id = f"paper_{self._order_counter}_{int(time.time()*1000)}"
        
        order = {
            "id": order_id,
            "token_id": token_id,
            "side": side,
            "price": price,
            "size": size,
            "size_remaining": size,
            "order_type": order_type,
            "status": "LIVE",
            "created_at": time.time(),
        }
        self._orders[order_id] = order
        return order
    
    def cancel_order(self, order_id: str) -> bool:
        if order_id in self._orders:
            self._orders[order_id]["status"] = "CANCELLED"
            del self._orders[order_id]
            return True
        return False
    
    def cancel_all(self) -> int:
        count = len(self._orders)
        self._orders.clear()
        return count
    
    def cancel_market_orders(self, token_id: str) -> int:
        to_cancel = [oid for oid, o in self._orders.items() if o["token_id"] == token_id]
        for oid in to_cancel:
            del self._orders[oid]
        return len(to_cancel)
    
    def get_open_orders(self, token_id: str = None) -> List[Dict]:
        orders = list(self._orders.values())
        if token_id:
            orders = [o for o in orders if o["token_id"] == token_id]
        return orders
    
    def simulate_fill(self, order_id: str, fill_price: float = None) -> Optional[Dict]:
        """Simulate a fill for an order."""
        if order_id not in self._orders:
            return None
        
        order = self._orders[order_id]
        fill = {
            "order_id": order_id,
            "token_id": order["token_id"],
            "side": order["side"],
            "price": fill_price or order["price"],
            "size": order["size"],
            "time": time.time(),
        }
        self._fills.append(fill)
        del self._orders[order_id]
        return fill


# ─────────────────────────────────────────────
# CLOB Client V4
# ─────────────────────────────────────────────

class CLOBClientV4:
    """
    Production CLOB client with EIP-712 signing.
    
    In paper mode: simulates orders locally.
    In live mode: uses py_clob_client with real signing.
    
    All methods are async for compatibility with the v4 engine architecture.
    The underlying py_clob_client is synchronous, so we wrap calls in
    asyncio.to_thread() to avoid blocking the event loop.
    """
    
    def __init__(self, config: CLOBConfig):
        self.config = config
        
        # Live client (py_clob_client)
        self._client = None
        self._creds = None
        
        # Paper mode
        self._paper = PaperOrderBook()
        
        # HTTP session for Gamma API
        self._session: Optional[aiohttp.ClientSession] = None
        
        # Caching
        self._orderbook_cache: Dict[str, Tuple[float, Dict]] = {}
        self._market_cache: Tuple[float, List] = (0, [])
        self._price_cache: Dict[str, Tuple[float, float]] = {}
        
        # Rate limiter
        self._limiter = RateLimiter(config.max_requests_per_second)
        
        # WebSocket feed (optional, set via set_ws_feed)
        self._ws_feed = None

        # Stats
        self._request_count = 0
        self._error_count = 0
        self._orders_placed = 0
        self._orders_cancelled = 0

    def set_ws_feed(self, ws_feed):
        """Attach WS orderbook feed for real-time data."""
        self._ws_feed = ws_feed
    
    # ── Connection ──
    
    async def connect(self):
        """Initialize the CLOB client and establish connections."""
        self._session = aiohttp.ClientSession()
        
        if self.config.paper_mode:
            logger.info("CLOB client initialized in PAPER mode")
            return
        
        # Live mode: initialize py_clob_client with signing
        if not self.config.private_key:
            raise ValueError("PRIVATE_KEY required for live trading")
        
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            
            # Create API credentials
            creds = None
            if self.config.api_key:
                creds = ApiCreds(
                    api_key=self.config.api_key,
                    api_secret=self.config.api_secret,
                    api_passphrase=self.config.api_passphrase,
                )
            
            # Initialize client with private key for EIP-712 signing
            self._client = ClobClient(
                host=self.config.host,
                chain_id=self.config.chain_id,
                key=self.config.private_key,
                creds=creds,
                signature_type=0,  # EOA signature
            )
            
            # If no API creds, derive them
            if not creds:
                logger.info("Deriving API credentials from private key...")
                self._creds = await asyncio.to_thread(
                    self._client.create_or_derive_api_creds
                )
                self._client.set_api_creds(self._creds)
                logger.info(f"API credentials derived: {self._creds.api_key[:8]}...")
            
            # Verify connection
            ok = await asyncio.to_thread(self._client.get_ok)
            if ok:
                logger.info(f"CLOB client connected (LIVE) — wallet: {self._client.get_address()}")
            else:
                raise ConnectionError("CLOB API health check failed")
            
            # Check balance
            balance = await self._get_balance()
            logger.info(f"USDC balance: ${balance:.2f}")
            
        except ImportError:
            raise ImportError(
                "py_clob_client not installed. Run: pip install py-clob-client"
            )
    
    async def disconnect(self):
        """Close connections."""
        if self._session:
            await self._session.close()
        logger.info("CLOB client disconnected")
    
    # ── Market Data ──
    
    async def get_markets(self, limit: int = 200) -> List[Dict]:
        """Fetch active markets from Gamma API."""
        now = time.time()
        cache_ts, cached = self._market_cache
        if now - cache_ts < self.config.market_cache_ttl and cached:
            return cached
        
        await self._limiter.acquire()
        self._request_count += 1
        
        try:
            all_markets = []
            next_cursor = None
            
            while len(all_markets) < limit:
                params = {"limit": min(100, limit - len(all_markets)), "active": "true", "closed": "false"}
                if next_cursor:
                    params["next_cursor"] = next_cursor
                
                async with self._session.get(
                    f"{self.config.gamma_url}/markets", params=params
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"get_markets status {resp.status}")
                        break
                    
                    data = await resp.json()
                    
                    if isinstance(data, list):
                        all_markets.extend(data)
                        break  # No pagination info
                    elif isinstance(data, dict):
                        all_markets.extend(data.get("data", data.get("markets", [])))
                        next_cursor = data.get("next_cursor")
                        if not next_cursor or next_cursor == "LTE=":
                            break
                    else:
                        break
            
            self._market_cache = (now, all_markets)
            return all_markets
            
        except Exception as e:
            self._error_count += 1
            logger.error(f"get_markets error: {e}")
            return cached  # Return stale cache on error
    
    async def get_orderbook(self, token_id: str) -> Optional[Dict]:
        """Fetch orderbook for a token. Checks WS cache first, then REST."""
        # Check WS cache first (real-time, no network call)
        if self._ws_feed:
            ob = self._ws_feed.get_orderbook(token_id)
            if ob:
                return ob

        # Fall back to REST with caching
        now = time.time()
        cached = self._orderbook_cache.get(token_id)
        if cached and now - cached[0] < self.config.orderbook_cache_ttl:
            return cached[1]
        
        await self._limiter.acquire()
        self._request_count += 1
        
        try:
            if self._client and not self.config.paper_mode:
                # Use py_clob_client (sync → async)
                ob = await asyncio.to_thread(self._client.get_order_book, token_id)
                result = {
                    "bids": [{"price": str(b.price), "size": str(b.size)} for b in ob.bids] if ob.bids else [],
                    "asks": [{"price": str(a.price), "size": str(a.size)} for a in ob.asks] if ob.asks else [],
                }
            else:
                # Direct API call
                async with self._session.get(
                    f"{self.config.host}/book",
                    params={"token_id": token_id},
                ) as resp:
                    if resp.status != 200:
                        return None
                    result = await resp.json()
            
            self._orderbook_cache[token_id] = (now, result)
            return result
            
        except Exception as e:
            self._error_count += 1
            logger.error(f"get_orderbook error: {e}")
            return cached[1] if cached else None
    
    async def get_price(self, token_id: str) -> Optional[float]:
        """Get mid price for a token."""
        await self._limiter.acquire()
        self._request_count += 1
        
        try:
            if self._client and not self.config.paper_mode:
                price = await asyncio.to_thread(self._client.get_price, token_id)
                return float(price) if price else None
            
            async with self._session.get(
                f"{self.config.host}/price",
                params={"token_id": token_id, "side": "buy"},
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                price = float(data.get("price", 0))
                self._price_cache[token_id] = (time.time(), price)
                return price
                
        except Exception as e:
            self._error_count += 1
            logger.error(f"get_price error: {e}")
            cached = self._price_cache.get(token_id)
            return cached[1] if cached else None
    
    async def get_prices_batch(self, token_ids: List[str]) -> Dict[str, float]:
        """Get prices for multiple tokens."""
        tasks = [self.get_price(tid) for tid in token_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        prices = {}
        for tid, result in zip(token_ids, results):
            if isinstance(result, (int, float)):
                prices[tid] = float(result)
        return prices
    
    async def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get midpoint price from CLOB."""
        try:
            if self._client and not self.config.paper_mode:
                mid = await asyncio.to_thread(self._client.get_midpoint, token_id)
                return float(mid) if mid else None
            
            # Compute from orderbook
            ob = await self.get_orderbook(token_id)
            if ob:
                bids = ob.get("bids", [])
                asks = ob.get("asks", [])
                if bids and asks:
                    best_bid = float(bids[0]["price"])
                    best_ask = float(asks[0]["price"])
                    return (best_bid + best_ask) / 2
            return None
        except Exception as e:
            logger.error(f"get_midpoint error: {e}")
            return None
    
    async def get_spread(self, token_id: str) -> Optional[Dict]:
        """Get spread info for a token."""
        try:
            if self._client and not self.config.paper_mode:
                spread = await asyncio.to_thread(self._client.get_spread, token_id)
                return spread
            
            ob = await self.get_orderbook(token_id)
            if ob:
                bids = ob.get("bids", [])
                asks = ob.get("asks", [])
                if bids and asks:
                    best_bid = float(bids[0]["price"])
                    best_ask = float(asks[0]["price"])
                    return {
                        "bid": best_bid,
                        "ask": best_ask,
                        "spread": best_ask - best_bid,
                        "spread_bps": (best_ask - best_bid) / ((best_bid + best_ask) / 2) * 10000,
                    }
            return None
        except Exception as e:
            logger.error(f"get_spread error: {e}")
            return None
    
    # ── Order Management (EIP-712 Signed) ──
    
    async def place_limit_order(self, token_id: str, side: str, price: float,
                                 size: float, order_type: str = "GTC") -> Optional[Dict]:
        """
        Place a limit order with EIP-712 signing.
        
        Args:
            token_id: CLOB token ID
            side: "BUY" or "SELL"
            price: Limit price (0.01 - 0.99)
            size: Size in shares (not dollars)
            order_type: "GTC" (default), "FOK", or "GTD"
        
        Returns:
            Order dict with 'id' field, or None on failure.
        """
        # Validate
        if price < 0.01 or price > 0.99:
            logger.warning(f"Invalid price {price} — must be 0.01-0.99")
            return None
        if size < 1:
            logger.warning(f"Invalid size {size} — must be >= 1")
            return None
        
        self._orders_placed += 1
        
        if self.config.paper_mode:
            order = self._paper.place_order(token_id, side.upper(), price, size, order_type)
            logger.info(
                f"📝 PAPER ORDER {side} {size:.1f} @ {price:.3f} "
                f"({order_type}) → {order['id']}"
            )
            return order
        
        # Live order with EIP-712 signing
        if not self._client:
            logger.error("CLOB client not initialized for live trading")
            return None
        
        await self._limiter.acquire()
        self._request_count += 1
        
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            
            # Map order type
            ot_map = {"GTC": OrderType.GTC, "FOK": OrderType.FOK, "GTD": OrderType.GTD}
            ot = ot_map.get(order_type, OrderType.GTC)
            
            # Get tick size for proper price rounding
            tick_size = await asyncio.to_thread(self._client.get_tick_size, token_id)
            
            # Build order args
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side.upper(),
            )
            
            # Create signed order (EIP-712 signature happens here)
            signed_order = await asyncio.to_thread(
                self._client.create_order, order_args
            )
            
            # Post to CLOB
            result = await asyncio.to_thread(
                self._client.post_order, signed_order, ot
            )
            
            if result:
                order_id = result.get("orderID", result.get("id", ""))
                logger.info(
                    f"🔴 LIVE ORDER {side} {size:.1f} @ {price:.3f} "
                    f"({order_type}) → {order_id}"
                )
                return {"id": order_id, "status": "LIVE", **result}
            
            return None
            
        except Exception as e:
            self._error_count += 1
            logger.error(f"place_limit_order error: {e}", exc_info=True)
            return None
    
    async def place_market_order(self, token_id: str, side: str, 
                                  size: float) -> Optional[Dict]:
        """
        Place a market order (FOK at best available price).
        
        For sniper trades where speed > price precision.
        """
        # Get best price
        ob = await self.get_orderbook(token_id)
        if not ob:
            return None
        
        if side.upper() == "BUY":
            asks = ob.get("asks", [])
            if not asks:
                return None
            price = float(asks[0]["price"]) + 0.01  # Ensure fill
        else:
            bids = ob.get("bids", [])
            if not bids:
                return None
            price = float(bids[0]["price"]) - 0.01
        
        price = max(0.01, min(0.99, price))
        return await self.place_limit_order(token_id, side, price, size, "FOK")
    
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order."""
        self._orders_cancelled += 1
        
        if self.config.paper_mode:
            result = self._paper.cancel_order(order_id)
            if result:
                logger.debug(f"PAPER CANCEL {order_id}")
            return result
        
        if not self._client:
            return False
        
        await self._limiter.acquire()
        self._request_count += 1
        
        try:
            result = await asyncio.to_thread(self._client.cancel, order_id)
            logger.info(f"CANCEL {order_id}: {result}")
            return bool(result)
        except Exception as e:
            self._error_count += 1
            logger.error(f"cancel_order error: {e}")
            return False
    
    async def cancel_all_orders(self) -> bool:
        """Cancel ALL open orders across all markets."""
        if self.config.paper_mode:
            count = self._paper.cancel_all()
            logger.info(f"PAPER CANCEL ALL: {count} orders")
            return True
        
        if not self._client:
            return False
        
        await self._limiter.acquire()
        self._request_count += 1
        
        try:
            result = await asyncio.to_thread(self._client.cancel_all)
            logger.info(f"CANCEL ALL: {result}")
            return True
        except Exception as e:
            self._error_count += 1
            logger.error(f"cancel_all error: {e}")
            return False
    
    async def cancel_market_orders(self, token_id: str) -> bool:
        """Cancel all orders for a specific token."""
        if self.config.paper_mode:
            count = self._paper.cancel_market_orders(token_id)
            logger.debug(f"PAPER CANCEL MARKET {token_id[:12]}: {count} orders")
            return True
        
        if not self._client:
            return False
        
        await self._limiter.acquire()
        self._request_count += 1
        
        try:
            result = await asyncio.to_thread(
                self._client.cancel_market_orders, token_id
            )
            logger.info(f"CANCEL MARKET {token_id[:12]}: {result}")
            return True
        except Exception as e:
            self._error_count += 1
            logger.error(f"cancel_market_orders error: {e}")
            return False
    
    async def get_open_orders(self, token_id: str = None) -> List[Dict]:
        """Get all open orders, optionally filtered by token."""
        if self.config.paper_mode:
            return self._paper.get_open_orders(token_id)
        
        if not self._client:
            return []
        
        await self._limiter.acquire()
        self._request_count += 1
        
        try:
            from py_clob_client.clob_types import OpenOrderParams
            
            params = None
            if token_id:
                params = OpenOrderParams(market=token_id)
            
            result = await asyncio.to_thread(self._client.get_orders, params)
            
            if isinstance(result, dict):
                return result.get("data", result.get("orders", []))
            elif isinstance(result, list):
                return result
            return []
            
        except Exception as e:
            self._error_count += 1
            logger.error(f"get_open_orders error: {e}")
            return []
    
    # ── Balance ──
    
    async def _get_balance(self) -> float:
        """Get USDC balance from CLOB."""
        if not self._client:
            return 0.0
        
        try:
            bal = await asyncio.to_thread(self._client.get_balance_allowance)
            if isinstance(bal, dict):
                return float(bal.get("balance", 0)) / 1e6  # USDC has 6 decimals
            return 0.0
        except Exception as e:
            logger.error(f"get_balance error: {e}")
            return 0.0
    
    async def get_balance(self) -> float:
        """Public balance check."""
        if self.config.paper_mode:
            return 5000.0  # Paper bankroll
        return await self._get_balance()
    
    # ── Stats ──
    
    def get_stats(self) -> Dict:
        return {
            "mode": "PAPER" if self.config.paper_mode else "LIVE",
            "host": self.config.host,
            "requests": self._request_count,
            "errors": self._error_count,
            "orders_placed": self._orders_placed,
            "orders_cancelled": self._orders_cancelled,
            "cache_sizes": {
                "orderbook": len(self._orderbook_cache),
                "markets": len(self._market_cache[1]),
                "prices": len(self._price_cache),
            },
            "paper_open_orders": len(self._paper.get_open_orders()) if self.config.paper_mode else None,
        }
