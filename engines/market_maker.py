"""
PolyEdge v4 — Engine 1: Market Maker
=====================================
Earns spread on prediction markets by posting two-sided quotes.

Strategy:
  1. Select markets with wide spreads (2-5¢), decent liquidity ($20K+), >7d to resolution
  2. Estimate fair value via OU mean reversion (dXt = θ(μ - Xt)dt + σdWt)
  3. Post bid/ask around fair value, skewed by inventory
  4. Capture spread minus fees (1% Polymarket fee each side)
  5. Hard inventory limits prevent directional blow-up

Revenue target: $200-270/month on $5K bankroll
Risk: $50/market max, $200 total across all markets
"""

import asyncio
import math
import time
import random
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import defaultdict
from enum import Enum

logger = logging.getLogger("polyedge.mm")


# ─────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────

class QuoteSide(Enum):
    BID = "BID"
    ASK = "ASK"

@dataclass
class Quote:
    """A single limit order (bid or ask) on the CLOB."""
    token_id: str
    side: QuoteSide
    price: float
    size: float
    order_id: Optional[str] = None
    placed_at: float = 0.0
    
    @property
    def notional(self) -> float:
        return self.price * self.size

@dataclass
class QuotePair:
    """Bid + ask quotes for a single market."""
    token_id: str
    condition_id: str
    bid: Optional[Quote] = None
    ask: Optional[Quote] = None
    fair_value: float = 0.5
    last_update: float = 0.0

@dataclass
class MarketInfo:
    """Candidate market for market-making."""
    condition_id: str
    token_id_yes: str
    token_id_no: str
    question: str
    liquidity: float
    volume_24h: float
    best_bid: float
    best_ask: float
    spread_cents: float
    hours_to_resolution: float
    category: str = ""
    slug: str = ""
    
    @property
    def mid_price(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0 if self.best_bid and self.best_ask else 0.5
    
    @property
    def url(self) -> str:
        if self.slug:
            return f"https://polymarket.com/event/{self.slug}"
        return f"https://polymarket.com/markets/{self.condition_id}"

@dataclass
class OUParams:
    """Ornstein-Uhlenbeck calibrated parameters for a market."""
    mu: float       # long-run mean (fair value)
    theta: float    # mean reversion speed
    sigma: float    # volatility
    fitted_at: float = 0.0
    n_obs: int = 0
    
    @property
    def half_life(self) -> float:
        """Time to revert halfway to mean (in seconds)."""
        return math.log(2) / max(self.theta, 1e-6)

@dataclass
class InventoryState:
    """Tracks net position per market."""
    token_id: str
    net_position: float = 0.0        # positive = long, negative = short
    avg_entry: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_bought: float = 0.0
    total_sold: float = 0.0
    n_trades: int = 0
    last_trade_at: float = 0.0


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

@dataclass
class MMConfig:
    """Market Maker configuration — all tuneable parameters."""
    # Market selection
    min_liquidity: float = 20_000       # $20K minimum market liquidity
    min_spread_cents: float = 0.5       # 0.5¢ minimum spread (Polymarket spreads are tight)
    max_spread_cents: float = 10.0      # Too wide = toxic flow risk
    min_hours_to_resolution: float = 168  # 7 days minimum
    max_markets: int = 5                # Max simultaneous markets
    
    # Quoting
    target_half_spread: float = 0.015   # 1.5¢ each side of fair value
    min_quote_size: float = 10.0        # $10 minimum quote size
    max_quote_size: float = 30.0        # $30 maximum quote size
    quote_refresh_secs: float = 45.0    # Re-quote every 45s
    
    # Inventory limits
    max_position_per_market: float = 50.0   # $50 max per market
    max_total_inventory: float = 200.0      # $200 total across all markets
    inventory_skew_factor: float = 0.5      # How aggressively to skew quotes
    
    # OU calibration
    ou_lookback_secs: float = 3600      # 1 hour of price history for calibration
    ou_min_observations: int = 30       # Minimum observations before trusting OU
    ou_recalibrate_secs: float = 300    # Recalibrate every 5 min
    ou_default_theta: float = 0.1       # Default mean reversion speed
    ou_default_sigma: float = 0.02      # Default volatility
    
    # Risk
    kelly_fraction: float = 0.25        # Quarter Kelly for conservative sizing
    max_loss_per_day: float = 50.0      # Kill switch: stop if daily loss > $50
    fee_rate: float = 0.01              # Polymarket 1% fee per side
    
    # Stealth / randomness
    size_jitter_pct: float = 0.10       # ±10% random size variation
    time_jitter_secs: float = 3.0       # ±3s random timing variation
    price_jitter_cents: float = 0.002   # ±0.2¢ random price variation


# ─────────────────────────────────────────────
# OU Calibrator
# ─────────────────────────────────────────────

class OUCalibrator:
    """
    Calibrates Ornstein-Uhlenbeck parameters per market using MLE.
    
    Model: dXt = θ(μ - Xt)dt + σdWt
    
    For prediction markets:
      - μ = long-run fair value (our best estimate of true probability)
      - θ = how fast price reverts to fair value (higher = faster reversion)
      - σ = random noise / volatility
    
    When price deviates from μ by > 2σ, there's a mean-reversion trade.
    """
    
    def __init__(self, config: MMConfig):
        self.config = config
        self._price_history: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
        self._params: Dict[str, OUParams] = {}
        self._last_calibration: Dict[str, float] = {}
        self._max_history = 2000  # Max observations to keep
    
    def record_price(self, token_id: str, price: float, timestamp: float = None):
        """Record a price observation for a market."""
        ts = timestamp or time.time()
        history = self._price_history[token_id]
        history.append((ts, price))
        
        # Trim old data
        cutoff = ts - self.config.ou_lookback_secs * 2
        while history and history[0][0] < cutoff:
            history.pop(0)
        
        if len(history) > self._max_history:
            self._price_history[token_id] = history[-self._max_history:]
    
    def get_params(self, token_id: str) -> Optional[OUParams]:
        """Get cached OU params, recalibrating if stale."""
        now = time.time()
        last_cal = self._last_calibration.get(token_id, 0)
        
        if now - last_cal > self.config.ou_recalibrate_secs:
            self._calibrate(token_id)
        
        return self._params.get(token_id)
    
    def get_fair_value(self, token_id: str) -> Optional[float]:
        """Get OU-estimated fair value (μ)."""
        params = self.get_params(token_id)
        return params.mu if params else None
    
    def get_deviation(self, token_id: str, current_price: float) -> Optional[float]:
        """Get current deviation from fair value in standard deviations."""
        params = self.get_params(token_id)
        if not params or params.sigma < 1e-6:
            return None
        return (current_price - params.mu) / params.sigma
    
    def _calibrate(self, token_id: str):
        """
        MLE calibration of OU parameters.
        
        For discrete observations X_i at times t_i:
          X_{i+1} = X_i * exp(-θ*dt) + μ*(1 - exp(-θ*dt)) + ε
          where ε ~ N(0, σ²*(1 - exp(-2θ*dt))/(2θ))
        
        We use the AR(1) regression approach:
          X_{i+1} = a + b*X_i + ε
          θ = -ln(b) / dt
          μ = a / (1 - b)
          σ² = 2θ * var(ε) / (1 - b²)
        """
        now = time.time()
        self._last_calibration[token_id] = now
        
        history = self._price_history.get(token_id, [])
        cutoff = now - self.config.ou_lookback_secs
        recent = [(t, p) for t, p in history if t >= cutoff]
        
        if len(recent) < self.config.ou_min_observations:
            # Not enough data — use defaults with simple mean
            if recent:
                prices = [p for _, p in recent]
                mu = sum(prices) / len(prices)
            else:
                mu = 0.5
            self._params[token_id] = OUParams(
                mu=mu,
                theta=self.config.ou_default_theta,
                sigma=self.config.ou_default_sigma,
                fitted_at=now,
                n_obs=len(recent)
            )
            return
        
        # AR(1) regression: X_{i+1} = a + b * X_i
        n = len(recent)
        prices = [p for _, p in recent]
        times = [t for t, _ in recent]
        
        # Average time step
        dt = (times[-1] - times[0]) / max(n - 1, 1)
        if dt < 0.1:
            dt = 2.0  # Default 2s if timestamps are weird
        
        # Compute regression coefficients
        x = prices[:-1]
        y = prices[1:]
        
        n_pairs = len(x)
        sum_x = sum(x)
        sum_y = sum(y)
        sum_xy = sum(xi * yi for xi, yi in zip(x, y))
        sum_x2 = sum(xi ** 2 for xi in x)
        
        denom = n_pairs * sum_x2 - sum_x ** 2
        if abs(denom) < 1e-12:
            # All prices identical — no reversion to estimate
            self._params[token_id] = OUParams(
                mu=prices[-1], theta=self.config.ou_default_theta,
                sigma=0.001, fitted_at=now, n_obs=n
            )
            return
        
        b = (n_pairs * sum_xy - sum_x * sum_y) / denom
        a = (sum_y - b * sum_x) / n_pairs
        
        # Clamp b to (0.001, 0.999) to avoid degenerate cases
        b = max(0.001, min(0.999, b))
        
        # Extract OU parameters
        theta = -math.log(b) / dt
        theta = max(0.001, min(10.0, theta))  # Clamp to reasonable range
        
        mu = a / (1.0 - b)
        mu = max(0.01, min(0.99, mu))  # Must be valid probability
        
        # Residual variance → σ
        residuals = [yi - (a + b * xi) for xi, yi in zip(x, y)]
        var_resid = sum(r ** 2 for r in residuals) / max(n_pairs - 2, 1)
        
        b2 = b ** 2
        if abs(1 - b2) > 1e-6 and var_resid > 0:
            sigma_sq = 2 * theta * var_resid / (1 - b2)
            sigma = math.sqrt(max(sigma_sq, 0))
        else:
            sigma = self.config.ou_default_sigma
        
        sigma = max(0.001, min(0.5, sigma))
        
        self._params[token_id] = OUParams(
            mu=mu, theta=theta, sigma=sigma,
            fitted_at=now, n_obs=n
        )
        
        logger.debug(
            f"OU calibrated {token_id[:12]}...: "
            f"μ={mu:.4f} θ={theta:.4f} σ={sigma:.4f} "
            f"half_life={math.log(2)/theta:.0f}s n={n}"
        )


# ─────────────────────────────────────────────
# Inventory Manager
# ─────────────────────────────────────────────

class InventoryManager:
    """Tracks positions, enforces limits, computes skew."""
    
    def __init__(self, config: MMConfig):
        self.config = config
        self._positions: Dict[str, InventoryState] = {}
        self._daily_pnl: float = 0.0
        self._daily_pnl_reset: float = 0.0
    
    def get_position(self, token_id: str) -> InventoryState:
        if token_id not in self._positions:
            self._positions[token_id] = InventoryState(token_id=token_id)
        return self._positions[token_id]
    
    def record_fill(self, token_id: str, side: str, price: float, size: float):
        """Record a trade fill and update inventory."""
        pos = self.get_position(token_id)
        notional = price * size
        
        if side == "BUY":
            if pos.net_position < 0:
                # Covering a short: realize PnL on the covered portion
                cover_size = min(size, abs(pos.net_position))
                pnl = (pos.avg_entry - price) * cover_size  # short sold high, buying low = profit
                pos.realized_pnl += pnl
                self._daily_pnl += pnl
                remaining = size - cover_size
                pos.net_position += cover_size
                if remaining > 0:
                    # Flipping to long with the remaining
                    pos.avg_entry = price
                    pos.net_position += remaining
            else:
                # Adding to long position
                total_cost = pos.avg_entry * pos.net_position + notional
                pos.net_position += size
                if pos.net_position > 0:
                    pos.avg_entry = total_cost / pos.net_position
            pos.total_bought += notional
        else:  # SELL
            if pos.net_position > 0:
                # Selling from a long: realize PnL on the sold portion
                sell_size = min(size, pos.net_position)
                pnl = (price - pos.avg_entry) * sell_size
                pos.realized_pnl += pnl
                self._daily_pnl += pnl
                remaining = size - sell_size
                pos.net_position -= sell_size
                if remaining > 0:
                    # Flipping to short with the remaining
                    pos.avg_entry = price
                    pos.net_position -= remaining
            else:
                # Adding to short position
                if pos.net_position < 0:
                    total_cost = pos.avg_entry * abs(pos.net_position) + notional
                    pos.net_position -= size
                    pos.avg_entry = total_cost / abs(pos.net_position)
                else:
                    # Opening new short
                    pos.avg_entry = price
                    pos.net_position -= size
            pos.total_sold += notional
        
        pos.n_trades += 1
        pos.last_trade_at = time.time()
        
        logger.info(
            f"FILL {side} {size:.1f} @ {price:.4f} | "
            f"net={pos.net_position:.1f} avg={pos.avg_entry:.4f} "
            f"rpnl=${pos.realized_pnl:.2f}"
        )
    
    def update_unrealized(self, token_id: str, current_price: float):
        """Update unrealized PnL for mark-to-market."""
        pos = self.get_position(token_id)
        if abs(pos.net_position) > 0.01:
            pos.unrealized_pnl = (current_price - pos.avg_entry) * pos.net_position
    
    def can_buy(self, token_id: str, size: float, price: float) -> bool:
        """Check if a buy would violate inventory limits."""
        pos = self.get_position(token_id)
        new_position = pos.net_position + size
        
        # Check absolute notional at avg entry (accumulated cost basis)
        new_notional = abs(new_position) * max(pos.avg_entry, price)
        if new_notional > self.config.max_position_per_market:
            return False
        
        # Also hard-cap on raw units to prevent runaway accumulation
        if abs(new_position) * price > self.config.max_position_per_market * 1.5:
            return False
        
        if self.total_exposure + (size * price) > self.config.max_total_inventory:
            return False
        
        return True
    
    def can_sell(self, token_id: str, size: float, price: float) -> bool:
        """Check if a sell would violate limits."""
        pos = self.get_position(token_id)
        new_position = pos.net_position - size
        
        # Check absolute notional at avg entry
        new_notional = abs(new_position) * max(pos.avg_entry, price)
        if new_notional > self.config.max_position_per_market:
            return False
        
        # Hard-cap on raw units
        if abs(new_position) * price > self.config.max_position_per_market * 1.5:
            return False
        
        return True
    
    def compute_skew(self, token_id: str) -> float:
        """
        Compute quote skew based on inventory.
        
        Returns adjustment in cents:
          - Positive = shift quotes UP (want to sell, make ask more attractive)
          - Negative = shift quotes DOWN (want to buy, make bid more attractive)
          - Zero = balanced inventory, symmetric quotes
        
        Skew formula: skew = -inventory_ratio * skew_factor * half_spread
        """
        pos = self.get_position(token_id)
        max_pos = self.config.max_position_per_market
        
        if max_pos == 0:
            return 0.0
        
        inventory_ratio = pos.net_position / max_pos  # -1 to +1
        skew = -inventory_ratio * self.config.inventory_skew_factor * self.config.target_half_spread
        
        return skew
    
    @property
    def total_exposure(self) -> float:
        """Total absolute notional exposure across all markets."""
        return sum(
            abs(p.net_position) * max(p.avg_entry, 0.01) 
            for p in self._positions.values()
        )
    
    @property
    def total_realized_pnl(self) -> float:
        return sum(p.realized_pnl for p in self._positions.values())
    
    @property
    def total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self._positions.values())
    
    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl
    
    def check_kill_switch(self) -> bool:
        """Returns True if daily loss exceeds limit — STOP TRADING."""
        return self._daily_pnl < -self.config.max_loss_per_day
    
    def reset_daily(self):
        """Called at midnight to reset daily PnL."""
        self._daily_pnl = 0.0
        self._daily_pnl_reset = time.time()
    
    def get_stats(self) -> Dict:
        return {
            "active_markets": len([p for p in self._positions.values() if abs(p.net_position) > 0.01]),
            "total_exposure": round(self.total_exposure, 2),
            "total_realized_pnl": round(self.total_realized_pnl, 2),
            "total_unrealized_pnl": round(self.total_unrealized_pnl, 2),
            "daily_pnl": round(self._daily_pnl, 2),
            "kill_switch": self.check_kill_switch(),
            "positions": {
                tid: {
                    "net": round(p.net_position, 2),
                    "avg_entry": round(p.avg_entry, 4),
                    "rpnl": round(p.realized_pnl, 2),
                    "upnl": round(p.unrealized_pnl, 2),
                    "trades": p.n_trades,
                }
                for tid, p in self._positions.items()
                if abs(p.net_position) > 0.01 or p.n_trades > 0
            }
        }
    
    def save_state(self) -> Dict:
        """Serialize full inventory state for persistence."""
        return {
            "positions": {
                tid: {
                    "token_id": p.token_id,
                    "net_position": p.net_position,
                    "avg_entry": p.avg_entry,
                    "realized_pnl": p.realized_pnl,
                    "unrealized_pnl": p.unrealized_pnl,
                    "total_bought": p.total_bought,
                    "total_sold": p.total_sold,
                    "n_trades": p.n_trades,
                    "last_trade_at": p.last_trade_at,
                }
                for tid, p in self._positions.items()
            },
            "daily_pnl": self._daily_pnl,
            "daily_pnl_reset": self._daily_pnl_reset,
        }
    
    def load_state(self, state: Dict):
        """Restore inventory state from persistence."""
        if not state:
            return
        for tid, pdata in state.get("positions", {}).items():
            pos = InventoryState(
                token_id=pdata["token_id"],
                net_position=pdata.get("net_position", 0.0),
                avg_entry=pdata.get("avg_entry", 0.0),
                realized_pnl=pdata.get("realized_pnl", 0.0),
                unrealized_pnl=pdata.get("unrealized_pnl", 0.0),
                total_bought=pdata.get("total_bought", 0.0),
                total_sold=pdata.get("total_sold", 0.0),
                n_trades=pdata.get("n_trades", 0),
                last_trade_at=pdata.get("last_trade_at", 0.0),
            )
            self._positions[tid] = pos
        self._daily_pnl = state.get("daily_pnl", 0.0)
        self._daily_pnl_reset = state.get("daily_pnl_reset", 0.0)
        logger.info(f"Restored {len(self._positions)} positions from state")


# ─────────────────────────────────────────────
# Spread Analyzer (Market Selection)
# ─────────────────────────────────────────────

class SpreadAnalyzer:
    """Selects optimal markets for market-making."""
    
    def __init__(self, config: MMConfig):
        self.config = config
        self._scores: Dict[str, float] = {}
    
    def score_market(self, market: MarketInfo) -> Tuple[bool, float, str]:
        """
        Score a market for MM suitability.
        
        Returns: (eligible, score, reason)
        Score = spread_edge * liquidity_score * time_score
        """
        reasons = []
        
        # Gate checks
        if market.liquidity < self.config.min_liquidity:
            return False, 0, f"Low liquidity ${market.liquidity:,.0f} < ${self.config.min_liquidity:,.0f}"
        
        if market.spread_cents < self.config.min_spread_cents:
            return False, 0, f"Spread too tight {market.spread_cents:.1f}¢ < {self.config.min_spread_cents}¢"
        
        if market.spread_cents > self.config.max_spread_cents:
            return False, 0, f"Spread too wide {market.spread_cents:.1f}¢ > {self.config.max_spread_cents}¢"
        
        if market.hours_to_resolution < self.config.min_hours_to_resolution:
            return False, 0, f"Too close to resolution {market.hours_to_resolution:.0f}h < {self.config.min_hours_to_resolution}h"
        
        # Price must be in makeable range (not near 0 or 1)
        mid = market.mid_price
        if mid < 0.10 or mid > 0.90:
            return False, 0, f"Price {mid:.2f} too extreme for MM"
        
        # Score components (0-1 each)
        
        # Spread edge: how much profit per round-trip after fees
        gross_spread = market.spread_cents / 100.0
        fees = 2 * self.config.fee_rate * mid  # Both sides
        net_spread = gross_spread - fees
        spread_score = max(0, min(1, net_spread / 0.03))  # Normalize to 3¢ net = perfect
        
        if net_spread <= 0:
            return False, 0, f"Negative net spread after fees: {net_spread*100:.1f}¢"
        
        # Liquidity score: more liquidity = safer
        liq_score = min(1.0, math.log10(max(market.liquidity, 1)) / 6)  # $1M = 1.0
        
        # Time score: more time to resolution = more opportunities
        time_score = min(1.0, market.hours_to_resolution / (30 * 24))  # 30 days = 1.0
        
        # Volume score: higher volume = more fills
        vol_score = min(1.0, math.log10(max(market.volume_24h, 1)) / 5)  # $100K = 1.0
        
        # Combined score
        score = spread_score * 0.4 + liq_score * 0.25 + time_score * 0.2 + vol_score * 0.15
        
        return True, score, f"Net spread {net_spread*100:.1f}¢ | liq ${market.liquidity:,.0f} | {market.hours_to_resolution:.0f}h"
    
    def select_markets(self, candidates: List[MarketInfo]) -> List[MarketInfo]:
        """Select top N markets for market-making."""
        scored = []
        for m in candidates:
            eligible, score, reason = self.score_market(m)
            if eligible:
                scored.append((score, m, reason))
                self._scores[m.condition_id] = score
        
        scored.sort(key=lambda x: x[0], reverse=True)
        selected = [m for _, m, _ in scored[:self.config.max_markets]]
        
        if selected:
            logger.info(
                f"Selected {len(selected)} markets for MM: "
                + " | ".join(f"{m.question[:40]}... ({s:.2f})" for s, m, _ in scored[:self.config.max_markets])
            )
        
        return selected


# ─────────────────────────────────────────────
# Quote Generator
# ─────────────────────────────────────────────

class QuoteGenerator:
    """Generates bid/ask quotes with inventory skew and randomness."""
    
    def __init__(self, config: MMConfig, ou: OUCalibrator, inventory: InventoryManager):
        self.config = config
        self.ou = ou
        self.inventory = inventory
    
    def generate_quotes(self, token_id: str, current_mid: float) -> QuotePair:
        """
        Generate a bid/ask quote pair.
        
        Fair value comes from OU model (if calibrated) or market mid.
        Quotes are skewed by inventory to reduce directional exposure.
        Random jitter added for stealth.
        """
        # 1. Fair value estimation
        # Use OU fair value only if well-calibrated (enough observations)
        ou_fair = self.ou.get_fair_value(token_id)
        ou_params = self.ou._params.get(token_id)
        ou_is_calibrated = ou_params and ou_params.n_obs >= self.ou.config.ou_min_observations
        
        if ou_is_calibrated and ou_fair is not None:
            fair_value = ou_fair
        else:
            # Not enough data — trust the market mid
            fair_value = current_mid
        
        # 2. Base half-spread
        half_spread = self.config.target_half_spread
        
        # 3. Inventory skew
        skew = self.inventory.compute_skew(token_id)
        
        # 4. OU deviation adjustment
        # If price is far from fair value, widen spread on the "wrong" side
        deviation = self.ou.get_deviation(token_id, current_mid)
        ou_adjustment = 0.0
        if deviation is not None and abs(deviation) > 1.5:
            # Price is 1.5σ+ from fair value — widen the side towards mispricing
            ou_adjustment = max(0, (abs(deviation) - 1.5)) * 0.005
        
        # 5. Stealth jitter
        price_jitter = random.uniform(
            -self.config.price_jitter_cents,
            self.config.price_jitter_cents
        )
        size_jitter_mult = 1.0 + random.uniform(
            -self.config.size_jitter_pct,
            self.config.size_jitter_pct
        )
        
        # 6. Compute prices
        bid_price = fair_value - half_spread + skew + price_jitter
        ask_price = fair_value + half_spread + skew + price_jitter
        
        # If OU says price is too high, widen the bid (less aggressive buying)
        if deviation and deviation > 1.5:
            bid_price -= ou_adjustment
        elif deviation and deviation < -1.5:
            ask_price += ou_adjustment
        
        # Clamp to valid range
        bid_price = max(0.01, min(0.98, round(bid_price, 3)))
        ask_price = max(0.02, min(0.99, round(ask_price, 3)))
        
        # Ensure bid < ask (minimum 1¢ spread)
        if bid_price >= ask_price:
            mid = (bid_price + ask_price) / 2
            bid_price = round(mid - 0.005, 3)
            ask_price = round(mid + 0.005, 3)
        
        # 7. Size with Kelly and jitter
        base_size = self._kelly_size(token_id, fair_value, bid_price, ask_price)
        size = round(base_size * size_jitter_mult, 1)
        size = max(self.config.min_quote_size, min(self.config.max_quote_size, size))
        
        # 8. Check inventory limits
        bid_quote = None
        ask_quote = None
        
        if self.inventory.can_buy(token_id, size, bid_price):
            bid_quote = Quote(
                token_id=token_id,
                side=QuoteSide.BID,
                price=bid_price,
                size=size,
            )
        
        if self.inventory.can_sell(token_id, size, ask_price):
            ask_quote = Quote(
                token_id=token_id,
                side=QuoteSide.ASK,
                price=ask_price,
                size=size,
            )
        
        pair = QuotePair(
            token_id=token_id,
            condition_id="",  # Set by caller
            bid=bid_quote,
            ask=ask_quote,
            fair_value=fair_value,
            last_update=time.time(),
        )
        
        return pair
    
    def _kelly_size(self, token_id: str, fair_value: float, 
                     bid: float, ask: float) -> float:
        """
        Kelly criterion for quote sizing.
        
        For a market maker, the "edge" is the expected profit per round-trip:
          edge = (ask - bid) - 2 * fee * mid_price
        
        Kelly fraction = edge / variance_of_return
        We use fractional Kelly (0.25) for conservative sizing.
        """
        mid = (bid + ask) / 2
        spread = ask - bid
        fees = 2 * self.config.fee_rate * mid
        net_edge = spread - fees
        
        if net_edge <= 0:
            return self.config.min_quote_size
        
        # Estimate variance from OU sigma
        params = self.ou.get_params(token_id)
        sigma = params.sigma if params else self.config.ou_default_sigma
        variance = sigma ** 2
        
        if variance < 1e-8:
            return self.config.min_quote_size
        
        kelly_full = net_edge / variance
        kelly_size = kelly_full * self.config.kelly_fraction * self.config.max_position_per_market
        
        return max(self.config.min_quote_size, min(self.config.max_quote_size, kelly_size))


# ─────────────────────────────────────────────
# Market Maker Engine (Main Orchestrator)
# ─────────────────────────────────────────────

class MarketMakerEngine:
    """
    Engine 1: Market Maker
    
    Lifecycle:
      1. scan() — find suitable markets
      2. calibrate() — fit OU params from price history
      3. quote() — generate and place bid/ask quotes
      4. monitor() — check fills, update inventory, refresh quotes
      5. risk_check() — kill switch, inventory limits
    
    Runs as async loop inside main_v4.py orchestrator.
    """
    
    def __init__(self, config: MMConfig, clob_client, data_store, telegram):
        self.config = config
        self.clob = clob_client
        self.store = data_store
        self.telegram = telegram
        
        # Sub-components
        self.ou = OUCalibrator(config)
        self.inventory = InventoryManager(config)
        self.spread_analyzer = SpreadAnalyzer(config)
        self.quote_gen = QuoteGenerator(config, self.ou, self.inventory)
        
        # State
        self._active_markets: List[MarketInfo] = []
        self._active_quotes: Dict[str, QuotePair] = {}  # token_id → QuotePair
        self._running = False
        self._paper_mode = True
        self._paper_fills: List[Dict] = []  # Simulated fills for paper mode
        self._force_kill = False  # Manual kill via /stop command
        
        # Stats
        self._started_at = 0.0
        self._quote_count = 0
        self._fill_count = 0
        self._total_spread_captured = 0.0
    
    # ── Lifecycle ──
    
    async def start(self, paper_mode: bool = True):
        """Start the market maker engine."""
        self._running = True
        self._paper_mode = paper_mode
        self._started_at = time.time()
        
        mode_str = "PAPER" if paper_mode else "LIVE"
        logger.info(f"Market Maker Engine starting in {mode_str} mode")
        
        if self.telegram:
            await self.telegram.send(
                f"🏪 Market Maker Engine started ({mode_str})\n"
                f"Max markets: {self.config.max_markets}\n"
                f"Max exposure: ${self.config.max_total_inventory}"
            )
        
        # Run main loops
        await asyncio.gather(
            self._market_scan_loop(),
            self._quote_loop(),
            self._monitor_loop(),
            self._calibration_loop(),
        )
    
    async def stop(self):
        """Gracefully stop — cancel all quotes."""
        self._running = False
        logger.info("Market Maker Engine stopping...")
        
        # Cancel all active quotes
        if not self._paper_mode:
            for token_id, pair in self._active_quotes.items():
                await self._cancel_quotes(token_id)
        
        stats = self.get_stats()
        if self.telegram:
            await self.telegram.send(
                f"🛑 Market Maker stopped\n"
                f"Fills: {self._fill_count} | "
                f"Spread captured: ${self._total_spread_captured:.2f}\n"
                f"PnL: ${stats['pnl']['realized']:.2f} realized, "
                f"${stats['pnl']['unrealized']:.2f} unrealized"
            )
    
    # ── Loop 1: Market Scan (every 5 min) ──
    
    async def _market_scan_loop(self):
        """Periodically scan for best MM markets."""
        while self._running:
            try:
                await self._scan_markets()
            except Exception as e:
                logger.error(f"Market scan error: {e}", exc_info=True)
            
            await asyncio.sleep(300)  # 5 min
    
    async def _scan_markets(self):
        """Fetch markets and select best candidates."""
        # Get all active markets from CLOB
        raw_markets = await self.clob.get_markets()
        if not raw_markets:
            logger.warning("No markets returned from CLOB")
            return
        
        # Build MarketInfo objects
        candidates = []
        for m in raw_markets:
            try:
                # Parse token IDs from Gamma API format
                # Gamma returns clobTokenIds as JSON string: '["token1", "token2"]'
                clob_token_ids_raw = m.get("clobTokenIds", "[]")
                if isinstance(clob_token_ids_raw, str):
                    import json as _json
                    try:
                        clob_token_ids = _json.loads(clob_token_ids_raw)
                    except:
                        clob_token_ids = []
                elif isinstance(clob_token_ids_raw, list):
                    clob_token_ids = clob_token_ids_raw
                else:
                    clob_token_ids = []
                
                if len(clob_token_ids) < 2:
                    continue
                
                token_yes = clob_token_ids[0]
                token_no = clob_token_ids[1]
                
                # Use bestBid/bestAsk from Gamma if available (avoids extra API call)
                best_bid = float(m.get("bestBid", 0) or 0)
                best_ask = float(m.get("bestAsk", 0) or 0)
                
                # If not available, fetch orderbook
                if best_bid <= 0 or best_ask <= 0:
                    ob = await self.clob.get_orderbook(token_yes)
                    if not ob:
                        continue
                    bids = ob.get("bids", [])
                    asks = ob.get("asks", [])
                    if bids:
                        best_bid = max(float(b.get("price", 0)) for b in bids)
                    if asks:
                        best_ask = min(float(a.get("price", 0)) for a in asks if float(a.get("price", 0)) > 0)
                
                if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
                    continue
                
                spread_cents = (best_ask - best_bid) * 100
                
                # Estimate hours to resolution
                end_date = m.get("endDate", m.get("end_date_iso", ""))
                hours_to_res = 720  # Default 30 days
                if end_date:
                    try:
                        from datetime import datetime, timezone
                        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        hours_to_res = max(0, (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600)
                    except:
                        pass
                
                info = MarketInfo(
                    condition_id=m.get("conditionId", m.get("condition_id", "")),
                    token_id_yes=token_yes,
                    token_id_no=token_no,
                    question=m.get("question", ""),
                    liquidity=float(m.get("liquidityNum", m.get("liquidity", 0)) or 0),
                    volume_24h=float(m.get("volume24hr", m.get("volume", 0)) or 0),
                    best_bid=best_bid,
                    best_ask=best_ask,
                    spread_cents=spread_cents,
                    hours_to_resolution=hours_to_res,
                    category=m.get("category", ""),
                    slug=m.get("slug", m.get("market_slug", "")),
                )
                candidates.append(info)
                
            except Exception as e:
                continue
        
        # Select best markets
        selected = self.spread_analyzer.select_markets(candidates)
        self._active_markets = selected
        
        logger.info(
            f"Market scan: {len(candidates)} candidates, "
            f"{len(selected)} selected for MM"
        )
    
    # ── Loop 2: Quote Placement (every 30s) ──
    
    async def _quote_loop(self):
        """Generate and place quotes on selected markets."""
        await asyncio.sleep(15)  # Wait for initial market scan
        
        while self._running:
            try:
                if self._force_kill or self.inventory.check_kill_switch():
                    logger.warning("KILL SWITCH ACTIVE — trading paused")
                    await asyncio.sleep(60)
                    continue
                
                for market in self._active_markets:
                    await self._update_quotes(market)
                    
                    # Stealth: random delay between markets
                    jitter = random.uniform(0, self.config.time_jitter_secs)
                    await asyncio.sleep(jitter)
                
            except Exception as e:
                logger.error(f"Quote loop error: {e}", exc_info=True)
            
            await asyncio.sleep(self.config.quote_refresh_secs)
    
    async def _update_quotes(self, market: MarketInfo):
        """Generate and place/update quotes for a market."""
        token_id = market.token_id_yes
        
        # Get current orderbook
        ob = await self.clob.get_orderbook(token_id)
        if not ob:
            return
        
        # Polymarket API may not sort — ensure we get true best bid/ask
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        best_bid = 0
        best_ask = 0
        if bids:
            best_bid = max(float(b.get("price", 0)) for b in bids)
        if asks:
            best_ask = min(float(a.get("price", 0)) for a in asks if float(a.get("price", 0)) > 0)
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.5
        
        # Record price for OU calibration
        self.ou.record_price(token_id, mid)
        
        # Generate new quotes
        pair = self.quote_gen.generate_quotes(token_id, mid)
        pair.condition_id = market.condition_id
        
        if self._paper_mode:
            # Paper mode: just track quotes, simulate fills
            self._active_quotes[token_id] = pair
            self._simulate_fills(token_id, pair, best_bid, best_ask)
            self._quote_count += 1
        else:
            # Live mode: cancel old quotes, place new ones
            await self._cancel_quotes(token_id)
            
            if pair.bid:
                order = await self.clob.place_limit_order(
                    token_id=token_id,
                    side="BUY",
                    price=pair.bid.price,
                    size=pair.bid.size,
                )
                if order:
                    pair.bid.order_id = order.get("id")
                    pair.bid.placed_at = time.time()
            
            if pair.ask:
                order = await self.clob.place_limit_order(
                    token_id=token_id,
                    side="SELL",
                    price=pair.ask.price,
                    size=pair.ask.size,
                )
                if order:
                    pair.ask.order_id = order.get("id")
                    pair.ask.placed_at = time.time()
            
            self._active_quotes[token_id] = pair
            self._quote_count += 1
        
        logger.debug(
            f"Quotes {token_id[:12]}...: "
            f"bid={pair.bid.price:.3f}x{pair.bid.size:.0f} " if pair.bid else "bid=None "
            f"ask={pair.ask.price:.3f}x{pair.ask.size:.0f} " if pair.ask else "ask=None "
            f"fair={pair.fair_value:.4f} mid={mid:.4f}"
        )
    
    def _simulate_fills(self, token_id: str, pair: QuotePair,
                        market_bid: float, market_ask: float):
        """
        Paper mode fill simulation — probabilistic model.
        
        Models realistic queue priority and time-in-queue:
        - Market crosses our level: 70% fill (not guaranteed — queue priority)
        - Within 1¢ of our level: 15% fill (aggressive flow, we're deep in queue)
        - Within 2¢: 5% fill (rare, only large sweeps)
        - Beyond 2¢: 0%
        
        Each cycle is ~45s, so with 5 markets this produces
        realistic fill rates of 2-5 fills/hour.
        """
        if not pair.bid or not pair.ask:
            return
        
        # Log periodically for diagnostics (every 100th call per token)
        if not hasattr(self, '_fill_sim_counter'):
            self._fill_sim_counter = {}
        self._fill_sim_counter[token_id] = self._fill_sim_counter.get(token_id, 0) + 1
        if self._fill_sim_counter[token_id] % 100 == 1:
            logger.info(
                f"📊 Fill sim [{token_id[:8]}]: "
                f"our_bid={pair.bid.price:.4f} our_ask={pair.ask.price:.4f} "
                f"mkt_bid={market_bid:.4f} mkt_ask={market_ask:.4f} "
                f"bid_dist={abs(market_ask - pair.bid.price):.4f} "
                f"ask_dist={abs(pair.ask.price - market_bid):.4f}"
            )
        
        # BID side: fills when sellers come down to our level
        bid_distance = abs(market_ask - pair.bid.price)
        if market_ask <= pair.bid.price:
            fill_prob = 0.70  # Market crossed us — queue priority matters
        elif bid_distance <= 0.01:
            fill_prob = 0.15  # Within 1¢
        elif bid_distance <= 0.02:
            fill_prob = 0.05  # Within 2¢
        else:
            fill_prob = 0.0
        
        # Re-check inventory before filling
        if fill_prob > 0 and random.random() < fill_prob:
            if self.inventory.can_buy(token_id, pair.bid.size, pair.bid.price):
                self.inventory.record_fill(
                    token_id, "BUY", pair.bid.price, pair.bid.size
                )
                self._fill_count += 1
                self._total_spread_captured += pair.fair_value - pair.bid.price
                self._paper_fills.append({
                    "time": time.time(),
                    "token_id": token_id,
                    "side": "BUY",
                    "price": pair.bid.price,
                    "size": pair.bid.size,
                })
                logger.info(f"📗 PAPER FILL BUY {pair.bid.size:.0f} @ {pair.bid.price:.3f} (prob={fill_prob:.0%}, dist={bid_distance:.4f})")
            else:
                logger.debug(f"⛔ BUY fill blocked by inventory limit on {token_id[:8]}")
        
        # ASK side: fills when buyers come up to our level
        ask_distance = abs(pair.ask.price - market_bid)
        if market_bid >= pair.ask.price:
            fill_prob = 0.70  # Market crossed us
        elif ask_distance <= 0.01:
            fill_prob = 0.15  # Within 1¢
        elif ask_distance <= 0.02:
            fill_prob = 0.05  # Within 2¢
        else:
            fill_prob = 0.0
        
        # Re-check inventory before filling
        if fill_prob > 0 and random.random() < fill_prob:
            if self.inventory.can_sell(token_id, pair.ask.size, pair.ask.price):
                self.inventory.record_fill(
                    token_id, "SELL", pair.ask.price, pair.ask.size
                )
                self._fill_count += 1
                self._total_spread_captured += pair.ask.price - pair.fair_value
                self._paper_fills.append({
                    "time": time.time(),
                    "token_id": token_id,
                    "side": "SELL",
                    "price": pair.ask.price,
                    "size": pair.ask.size,
                })
                logger.info(f"📕 PAPER FILL SELL {pair.ask.size:.0f} @ {pair.ask.price:.3f} (prob={fill_prob:.0%}, dist={ask_distance:.4f})")
            else:
                logger.debug(f"⛔ SELL fill blocked by inventory limit on {token_id[:8]}")
    
    async def _cancel_quotes(self, token_id: str):
        """Cancel existing quotes for a token."""
        pair = self._active_quotes.get(token_id)
        if not pair:
            return
        
        if pair.bid and pair.bid.order_id:
            await self.clob.cancel_order(pair.bid.order_id)
        if pair.ask and pair.ask.order_id:
            await self.clob.cancel_order(pair.ask.order_id)
    
    # ── Loop 3: Monitor Fills (every 10s) ──
    
    async def _monitor_loop(self):
        """Check for fills and update state."""
        await asyncio.sleep(20)
        
        while self._running:
            try:
                if not self._paper_mode:
                    # Live mode: check open orders for fills
                    open_orders = await self.clob.get_open_orders()
                    # Compare with expected orders to detect fills
                    for token_id, pair in self._active_quotes.items():
                        if pair.bid and pair.bid.order_id:
                            if not any(o.get("id") == pair.bid.order_id for o in open_orders):
                                # Bid order gone — likely filled
                                self.inventory.record_fill(
                                    token_id, "BUY", pair.bid.price, pair.bid.size
                                )
                                self._fill_count += 1
                                pair.bid = None
                        
                        if pair.ask and pair.ask.order_id:
                            if not any(o.get("id") == pair.ask.order_id for o in open_orders):
                                self.inventory.record_fill(
                                    token_id, "SELL", pair.ask.price, pair.ask.size
                                )
                                self._fill_count += 1
                                self._total_spread_captured += pair.ask.price - pair.fair_value
                                pair.ask = None
                
                # Update unrealized PnL for all positions
                for token_id in list(self._active_quotes.keys()):
                    ob = await self.clob.get_orderbook(token_id)
                    if ob:
                        bids = ob.get("bids", [])
                        asks = ob.get("asks", [])
                        if bids and asks:
                            best_bid = max(float(b.get("price", 0)) for b in bids)
                            best_ask = min(float(a.get("price", 0)) for a in asks if float(a.get("price", 0)) > 0)
                            mid = (best_bid + best_ask) / 2
                            self.inventory.update_unrealized(token_id, mid)
                
            except Exception as e:
                logger.error(f"Monitor loop error: {e}", exc_info=True)
            
            await asyncio.sleep(10)
    
    # ── Loop 4: OU Recalibration (every 5 min) ──
    
    async def _calibration_loop(self):
        """Periodically recalibrate OU parameters."""
        await asyncio.sleep(30)
        
        while self._running:
            try:
                for token_id in list(self._active_quotes.keys()):
                    params = self.ou.get_params(token_id)
                    if params:
                        logger.debug(
                            f"OU {token_id[:12]}...: "
                            f"μ={params.mu:.4f} θ={params.theta:.4f} "
                            f"σ={params.sigma:.4f} hl={params.half_life:.0f}s"
                        )
            except Exception as e:
                logger.error(f"Calibration loop error: {e}")
            
            await asyncio.sleep(self.config.ou_recalibrate_secs)
    
    # ── Stats & Dashboard ──
    
    def get_stats(self) -> Dict:
        """Full stats snapshot for dashboard / auditor."""
        uptime = time.time() - self._started_at if self._started_at else 0
        
        return {
            "engine": "market_maker",
            "status": "running" if self._running else "stopped",
            "mode": "PAPER" if self._paper_mode else "LIVE",
            "uptime_hours": round(uptime / 3600, 1),
            "active_markets": len(self._active_markets),
            "markets": [
                {
                    "question": m.question,
                    "url": m.url,
                    "spread": f"{m.spread_cents:.1f}¢",
                    "liquidity": f"${m.liquidity:,.0f}",
                    "hours_to_res": round(m.hours_to_resolution, 0),
                }
                for m in self._active_markets
            ],
            "quotes": {
                tid: {
                    "bid": f"{p.bid.price:.3f}x{p.bid.size:.0f}" if p.bid else None,
                    "ask": f"{p.ask.price:.3f}x{p.ask.size:.0f}" if p.ask else None,
                    "fair_value": round(p.fair_value, 4),
                    "question": next(
                        (m.question for m in self._active_markets if m.token_id_yes == tid),
                        tid[:16] + "..."
                    ),
                    "url": next(
                        (m.url for m in self._active_markets if m.token_id_yes == tid),
                        ""
                    ),
                }
                for tid, p in self._active_quotes.items()
            },
            "quote_count": self._quote_count,
            "fill_count": self._fill_count,
            "spread_captured": round(self._total_spread_captured, 4),
            "kill_switch": self.inventory.check_kill_switch(),
            "inventory": self.inventory.get_stats(),
            "pnl": {
                "realized": round(self.inventory.total_realized_pnl, 2),
                "unrealized": round(self.inventory.total_unrealized_pnl, 2),
                "daily": round(self.inventory.daily_pnl, 2),
                "total": round(
                    self.inventory.total_realized_pnl + self.inventory.total_unrealized_pnl, 2
                ),
            },
            "ou_params": {
                tid: {
                    "mu": round(p.mu, 4),
                    "theta": round(p.theta, 4),
                    "sigma": round(p.sigma, 4),
                    "half_life_s": round(p.half_life, 0),
                    "n_obs": p.n_obs,
                }
                for tid, p in self.ou._params.items()
            },
            "kill_switch": self.inventory.check_kill_switch(),
            "recent_paper_fills": self._paper_fills[-20:] if self._paper_mode else [],
        }
    
    def save_state(self) -> Dict:
        """Save full engine state for persistence across restarts."""
        return {
            "started_at": self._started_at,
            "quote_count": self._quote_count,
            "fill_count": self._fill_count,
            "total_spread_captured": self._total_spread_captured,
            "paper_fills": self._paper_fills[-200:],  # Keep last 200 fills
            "inventory": self.inventory.save_state(),
        }
    
    def load_state(self, state: Dict):
        """Restore engine state from persistence."""
        if not state:
            return
        self._started_at = state.get("started_at", self._started_at)
        self._quote_count = state.get("quote_count", 0)
        self._fill_count = state.get("fill_count", 0)
        self._total_spread_captured = state.get("total_spread_captured", 0.0)
        self._paper_fills = state.get("paper_fills", [])
        if "inventory" in state:
            self.inventory.load_state(state["inventory"])
        logger.info(
            f"Restored MM state: {self._fill_count} fills, "
            f"${self._total_spread_captured:.2f} spread captured, "
            f"rpnl=${self.inventory.total_realized_pnl:.2f}"
        )
