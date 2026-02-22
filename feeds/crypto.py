"""
PolyEdge v4 — Crypto Feed
Connects to Binance WebSocket for real-time price monitoring.

Emits FeedEvents:
  PRICE_PUMP   — price up >2% in 5 min or >5% in 1 hour
  PRICE_DUMP   — price down >2% in 5 min or >5% in 1 hour
  PRICE_THRESHOLD_CROSS — crosses round numbers (50K, 100K, etc.)

Usage:
  feed = CryptoFeed(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
  events = await feed.poll()  # Returns list of FeedEvent

No API key needed — uses public Binance WebSocket streams.
"""

import asyncio
import aiohttp
import logging
import time
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Deque
from enum import Enum

logger = logging.getLogger("polyedge.feeds.crypto")

# ─── Import FeedEvent ────────────────────────────────────────

try:
    from engines.resolution_sniper_v2 import FeedEvent, EventType
except ImportError:
    class EventType(Enum):
        PRICE_PUMP = "PRICE_PUMP"
        PRICE_DUMP = "PRICE_DUMP"
        PRICE_THRESHOLD_CROSS = "PRICE_THRESHOLD_CROSS"
        CUSTOM = "CUSTOM"

    @dataclass
    class FeedEvent:
        event_type: EventType
        source: str
        timestamp: float
        received_at: float
        data: Dict
        keywords: List[str]
        confidence: float


# ─── Config ──────────────────────────────────────────────────

# Thresholds for triggering events
PUMP_DUMP_5MIN_PCT = 2.0      # 2% in 5 min → event
PUMP_DUMP_1HR_PCT = 5.0       # 5% in 1 hour → event
PUMP_DUMP_15MIN_PCT = 3.0     # 3% in 15 min → event

# Round number thresholds for BTC/ETH/SOL
THRESHOLD_MAP = {
    "BTCUSDT": [50000, 60000, 70000, 75000, 80000, 85000, 90000, 95000,
                100000, 110000, 120000, 150000, 200000],
    "ETHUSDT": [1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000,
                6000, 7000, 8000, 10000],
    "SOLUSDT": [50, 75, 100, 125, 150, 175, 200, 250, 300, 400, 500],
}

# Cooldown: don't emit same event within this window
EVENT_COOLDOWN_SECS = 300  # 5 min

# Keyword mapping for market search
SYMBOL_KEYWORDS = {
    "BTCUSDT": ["bitcoin", "btc", "crypto"],
    "ETHUSDT": ["ethereum", "eth", "ether", "crypto"],
    "SOLUSDT": ["solana", "sol", "crypto"],
}

WS_URL = "wss://stream.binance.com:9443/ws"


# ─── Price History ───────────────────────────────────────────

@dataclass
class PricePoint:
    price: float
    timestamp: float


@dataclass
class SymbolState:
    """Tracks price history and last events for a symbol."""
    symbol: str
    current_price: float = 0.0
    history_5min: Deque[PricePoint] = field(default_factory=lambda: deque(maxlen=60))    # ~5s intervals
    history_15min: Deque[PricePoint] = field(default_factory=lambda: deque(maxlen=180))  # ~5s intervals
    history_1hr: Deque[PricePoint] = field(default_factory=lambda: deque(maxlen=720))    # ~5s intervals
    last_threshold_side: Dict[float, str] = field(default_factory=dict)  # threshold → "above"/"below"
    last_event_time: Dict[str, float] = field(default_factory=dict)  # event_key → timestamp


class CryptoFeed:
    """
    Real-time crypto price monitoring via Binance.
    
    Methods:
        poll()  → List[FeedEvent]  (check for new events)
        start() → background WebSocket connection
        close() → clean shutdown
    """
    
    def __init__(self, symbols: List[str] = None):
        self.symbols = symbols or ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        self._states: Dict[str, SymbolState] = {
            s: SymbolState(symbol=s) for s in self.symbols
        }
        self._ws = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._connected = False
        self._pending_events: List[FeedEvent] = []
        self._ws_task = None
        self._started = False
    
    async def _ensure_started(self):
        """Start WebSocket if not already running."""
        if not self._started:
            self._started = True
            self._ws_task = asyncio.create_task(self._ws_loop())
    
    async def poll(self) -> List[FeedEvent]:
        """
        Return any pending events since last poll.
        Also starts WebSocket connection on first call.
        """
        await self._ensure_started()
        
        # If WebSocket isn't connected yet, do a REST fallback
        if not self._connected:
            await self._rest_update()
        
        # Check all symbols for events
        events = self._check_events()
        
        # Add any pending events from WebSocket
        pending = self._pending_events.copy()
        self._pending_events.clear()
        
        return events + pending
    
    async def _ws_loop(self):
        """WebSocket connection loop with auto-reconnect."""
        streams = "/".join(f"{s.lower()}@trade" for s in self.symbols)
        url = f"{WS_URL}/{streams}"
        
        while self._started:
            try:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession()
                
                async with self._session.ws_connect(url, heartbeat=30) as ws:
                    self._connected = True
                    self._ws = ws
                    logger.info(f"WebSocket connected: {', '.join(self.symbols)}")
                    
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle_trade(json.loads(msg.data))
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break
                
                self._connected = False
                logger.warning("WebSocket disconnected, reconnecting in 5s...")
                await asyncio.sleep(5)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                logger.error(f"WebSocket error: {e}, reconnecting in 10s...")
                await asyncio.sleep(10)
    
    def _handle_trade(self, data: Dict):
        """Process incoming trade from WebSocket."""
        symbol = data.get("s", "")
        if symbol not in self._states:
            return
        
        price = float(data.get("p", 0))
        if price <= 0:
            return
        
        state = self._states[symbol]
        state.current_price = price
        
        now = time.time()
        point = PricePoint(price=price, timestamp=now)
        
        # Only record every ~2 seconds to save memory
        if (not state.history_5min or
                now - state.history_5min[-1].timestamp >= 2.0):
            state.history_5min.append(point)
            state.history_15min.append(point)
            state.history_1hr.append(point)
    
    async def _rest_update(self):
        """REST API fallback when WebSocket isn't connected."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        
        try:
            for symbol in self.symbols:
                async with self._session.get(
                    "https://api.binance.com/api/v3/ticker/price",
                    params={"symbol": symbol},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        price = float(data["price"])
                        state = self._states[symbol]
                        state.current_price = price
                        
                        now = time.time()
                        point = PricePoint(price=price, timestamp=now)
                        state.history_5min.append(point)
                        state.history_15min.append(point)
                        state.history_1hr.append(point)
        except Exception as e:
            logger.error(f"REST update error: {e}")
    
    def _check_events(self) -> List[FeedEvent]:
        """Check all symbols for triggerable events."""
        events = []
        now = time.time()
        
        for symbol, state in self._states.items():
            if state.current_price <= 0:
                continue
            
            # Check pump/dump on multiple timeframes
            events.extend(self._check_pump_dump(state, now))
            
            # Check threshold crosses
            events.extend(self._check_thresholds(state, now))
        
        return events
    
    def _check_pump_dump(self, state: SymbolState, now: float) -> List[FeedEvent]:
        """Check for significant price moves in various windows."""
        events = []
        
        checks = [
            (state.history_5min, 300, PUMP_DUMP_5MIN_PCT, "5min", 0.95),
            (state.history_15min, 900, PUMP_DUMP_15MIN_PCT, "15min", 0.90),
            (state.history_1hr, 3600, PUMP_DUMP_1HR_PCT, "1hr", 0.85),
        ]
        
        for history, window_secs, threshold_pct, label, confidence in checks:
            if len(history) < 3:
                continue
            
            # Find oldest price in window
            cutoff = now - window_secs
            old_points = [p for p in history if p.timestamp >= cutoff]
            if not old_points:
                continue
            
            old_price = old_points[0].price
            if old_price <= 0:
                continue
            
            change_pct = (state.current_price - old_price) / old_price * 100
            
            if abs(change_pct) >= threshold_pct:
                # Cooldown check
                event_key = f"{state.symbol}_{label}_{'pump' if change_pct > 0 else 'dump'}"
                last_time = state.last_event_time.get(event_key, 0)
                if now - last_time < EVENT_COOLDOWN_SECS:
                    continue
                
                state.last_event_time[event_key] = now
                
                is_pump = change_pct > 0
                event_type = EventType.PRICE_PUMP if is_pump else EventType.PRICE_DUMP
                
                keywords = SYMBOL_KEYWORDS.get(state.symbol, [state.symbol.lower()])
                
                event = FeedEvent(
                    event_type=event_type,
                    source="crypto",
                    timestamp=now,
                    received_at=now,
                    data={
                        "symbol": state.symbol,
                        "price": state.current_price,
                        "old_price": old_price,
                        "change_pct": round(change_pct, 2),
                        "window": label,
                        "direction": "up" if is_pump else "down",
                    },
                    keywords=keywords.copy(),
                    confidence=confidence,
                )
                events.append(event)
                
                direction = "🟢 PUMP" if is_pump else "🔴 DUMP"
                logger.info(
                    f"{direction}: {state.symbol} {change_pct:+.1f}% in {label} "
                    f"(${old_price:,.2f} → ${state.current_price:,.2f})"
                )
        
        return events
    
    def _check_thresholds(self, state: SymbolState, now: float) -> List[FeedEvent]:
        """Check if price crossed a significant threshold."""
        events = []
        thresholds = THRESHOLD_MAP.get(state.symbol, [])
        
        if not thresholds or len(state.history_5min) < 2:
            return events
        
        # Previous price (a few seconds ago)
        prev = state.history_5min[-2].price if len(state.history_5min) >= 2 else 0
        curr = state.current_price
        
        if prev <= 0 or curr <= 0:
            return events
        
        for threshold in thresholds:
            # Did we cross this threshold?
            crossed_up = prev < threshold <= curr
            crossed_down = prev > threshold >= curr
            
            if not crossed_up and not crossed_down:
                continue
            
            direction = "above" if crossed_up else "below"
            
            # Check if already on this side
            last_side = state.last_threshold_side.get(threshold)
            if last_side == direction:
                continue
            state.last_threshold_side[threshold] = direction
            
            # Cooldown
            event_key = f"{state.symbol}_threshold_{threshold}_{direction}"
            last_time = state.last_event_time.get(event_key, 0)
            if now - last_time < EVENT_COOLDOWN_SECS:
                continue
            state.last_event_time[event_key] = now
            
            keywords = SYMBOL_KEYWORDS.get(state.symbol, [state.symbol.lower()])
            
            event = FeedEvent(
                event_type=EventType.PRICE_THRESHOLD_CROSS,
                source="crypto",
                timestamp=now,
                received_at=now,
                data={
                    "symbol": state.symbol,
                    "price": curr,
                    "threshold": threshold,
                    "direction": direction,
                    "crossed": "up" if crossed_up else "down",
                },
                keywords=keywords.copy(),
                confidence=0.99,
            )
            events.append(event)
            
            emoji = "⬆️" if crossed_up else "⬇️"
            logger.info(
                f"{emoji} THRESHOLD: {state.symbol} crossed ${threshold:,.0f} "
                f"({direction}) — now ${curr:,.2f}"
            )
        
        return events
    
    async def close(self):
        """Clean shutdown."""
        self._started = False
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
        logger.info("Crypto feed closed")
