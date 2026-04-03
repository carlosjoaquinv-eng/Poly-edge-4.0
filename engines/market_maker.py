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
import os
import math
import time
import random
from engine.core.hmm_regime import RegimeDetector, RegimeConfig
import logging

try:
    from engine.core.risk_calculator import RiskCalculator, RISK_PROFILE_CONFIGS
    _HAS_RISK_CALC = True
except ImportError:
    _HAS_RISK_CALC = False

try:
    from engine.core.liquidity_checker import LiquidityChecker, LiquidityConfig
    _HAS_LIQUIDITY_CHECKER = True
except ImportError:
    _HAS_LIQUIDITY_CHECKER = False
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
    """Bid + ask quotes for a single market (multi-level)."""
    token_id: str
    condition_id: str
    bid: Optional[Quote] = None
    ask: Optional[Quote] = None
    extra_bids: List[Quote] = field(default_factory=list)  # Deeper bid levels
    extra_asks: List[Quote] = field(default_factory=list)  # Deeper ask levels
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
    # SL/TP tracking
    peak_price: float = 0.0         # Highest price seen (for trailing stop)
    tp1_hit: bool = False            # First take-profit level hit
    tp2_hit: bool = False            # Second take-profit level hit
    last_exit_time: float = 0.0      # Cooldown after exit


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

@dataclass
class MMConfig:
    """Market Maker configuration — all tuneable parameters."""
    # Market selection
    min_liquidity: float = 1_000        # $5K minimum market liquidity (sports markets have less)
    min_spread_cents: float = 0.1       # 1.5¢ minimum spread — filter out unprofitable tight markets
    min_fair_value: float = 0.03         # Skip markets with fair value < 3¢ (dust/dead)
    max_spread_cents: float = 8.0       # >8¢ = dead market, toxic flow risk
    min_hours_to_resolution: float = 24   # 24 hours minimum (allow closer to resolution)
    max_markets: int = 8                # Max simultaneous markets

    # Quoting
    target_half_spread: float = 0.015   # 1.5¢ each side (competitive in 3-5¢ spread markets)
    breakeven_floor: bool = True         # Never sell below avg_entry + fees
    breakeven_min_margin: float = 0.005  # 0.5¢ minimum profit above entry (covers fees)

    # ── Multi-level quoting ──
    quote_levels: int = 3                    # 3 BID + 3 ASK levels per market
    level_spacing_pct: float = 0.005         # 0.5% between levels
    level_size_decay: float = 0.6            # Each deeper level = 60% of previous size

    # ── Volume decay ──
    volume_decay_enabled: bool = True
    volume_decay_threshold: float = 0.50     # If volume < 50% of 24h avg, reduce
    volume_decay_min_factor: float = 0.3     # Reduce size to min 30%

    # ── Time proximity sizing ──
    time_proximity_enabled: bool = True
    time_proximity_hours: float = 48.0       # Start reducing at 48h before resolution
    time_proximity_min_factor: float = 0.25  # Reduce to 25% at resolution
    min_quote_size: float = 5.0        # $10 minimum quote size
    max_quote_size: float = 5.0        # $50 maximum quote size
    quote_refresh_secs: float = 30.0    # Re-quote every 30s

    # Inventory limits
    max_position_per_market: float = 100.0  # $100 max per market
    max_total_inventory: float = 500.0      # $500 total across all markets
    inventory_skew_factor: float = 1.0      # Base skew aggressiveness (2x original)
    inventory_skew_nonlinear: bool = True   # Quadratic skew: gentle at low inv, aggressive at high
    
    # OU calibration
    ou_lookback_secs: float = 3600      # 1 hour of price history for calibration
    ou_min_observations: int = 30       # Minimum observations before trusting OU
    ou_recalibrate_secs: float = 300    # Recalibrate every 5 min
    ou_default_theta: float = 0.1       # Default mean reversion speed
    ou_default_sigma: float = 0.02      # Default volatility
    
    # Risk
    kelly_fraction: float = 0.25        # Quarter Kelly for conservative sizing
    max_loss_per_day: float = 80.0      # Kill switch: stop if daily loss > $80
    fee_rate: float = 0.01              # Polymarket 1% fee per side
    
    # Stealth / randomness
    size_jitter_pct: float = 0.10       # ±10% random size variation
    time_jitter_secs: float = 3.0       # ±3s random timing variation
    price_jitter_cents: float = 0.002   # ±0.2¢ random price variation

    # ── Position Exit Rules (Capital Preservation) ──
    sl_enabled: bool = True             # Enable stop-loss
    sl_pct: float = 0.30               # 30% loss from entry → sell entire position
    sl_min_position_value: float = 5.0  # Only SL positions worth > $5
    tp_enabled: bool = True             # Enable take-profit
    tp_pct: float = 0.50               # 50% gain from entry → sell half
    tp_full_pct: float = 1.0           # 100% gain (2x) → sell 75%
    tp_trailing_pct: float = 0.15      # After TP1 hit, trail at -15% from peak
    exit_check_interval: float = 30.0  # Check every 30s
    exit_cooldown: float = 300.0       # 5min cooldown after an exit to avoid re-entry

    # ── RiskGuard: SAFETY NET (only catches catastrophic failures) ──
    # Native skew handles 95% of inventory management; RiskGuard is the emergency brake
    max_units_per_market: float = 400.0         # Hard cap on units — strong skew prevents reaching this
    inventory_warn_pct: float = 0.70            # 70% → log warning (no action)
    inventory_fadeout_pct: float = 0.85         # 85% → start reducing quote size
    inventory_oneside_pct: float = 0.92         # 92% → only quote reducing side
    inventory_halt_pct: float = 0.98            # 98% → stop quoting (near-max only)
    adaptive_skew_threshold: float = 0.65       # 65% → mild RiskGuard skew boost
    adaptive_skew_multiplier: float = 2.0       # Max 2x skew boost (native skew is strong)
    fill_asymmetry_window: int = 20             # Last N fills to check asymmetry
    fill_asymmetry_threshold: float = 0.85      # >85% one-sided fills → trigger
    max_drawdown_per_market: float = 40.0       # $40 drawdown → auto-pause market
    max_drawdown_total: float = 80.0            # $80 total drawdown → reduce all
    pnl_velocity_window_secs: float = 1800.0    # 30min window for PnL velocity
    pnl_velocity_alert: float = -15.0           # -$15/30min → alert
    riskguard_check_interval: float = 60.0      # Check KPIs every 60s (less overhead)
    riskguard_telegram_cooldown: float = 600.0  # 10min between repeated alerts


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

        # ── HARD UNIT CAP (RiskGuard fix #1) ──
        # Prevents accumulating 400+ contracts on cheap tokens
        if abs(new_position) > self.config.max_units_per_market:
            logger.debug(f"⛔ BUY blocked: {abs(new_position):.0f} units > {self.config.max_units_per_market:.0f} cap")
            return False

        # Check notional at CURRENT price (not avg_entry — prevents the OKC bug)
        new_notional = abs(new_position) * price
        if new_notional > self.config.max_position_per_market:
            return False

        # Also check at avg_entry for cost-basis limit
        if pos.avg_entry > 0:
            cost_notional = abs(new_position) * pos.avg_entry
            if cost_notional > self.config.max_position_per_market * 1.5:
                return False

        if self.total_exposure + (size * price) > self.config.max_total_inventory:
            return False

        return True

    def can_sell(self, token_id: str, size: float, price: float) -> bool:
        """Check if a sell would violate limits.

        On Polymarket, SELL YES requires holding YES tokens.
        We can only sell up to our net_position (no naked shorts).
        """
        pos = self.get_position(token_id)

        # ── Polymarket constraint: can't sell tokens we don't own ──
        if pos.net_position < size:
            if pos.net_position <= 0:
                logger.debug(f"⛔ SELL blocked: no inventory to sell (net={pos.net_position:.0f})")
                return False
            # Allow partial sell up to what we have (caller should adjust size)
            logger.debug(f"⛔ SELL blocked: want {size:.0f} but only have {pos.net_position:.0f}")
            return False

        # ── Loss protection: never sell at catastrophic loss ──
        # Prevents cross-market inventory confusion from causing huge losses
        if pos.avg_entry > 0 and price > 0:
            loss_pct = (price - pos.avg_entry) / pos.avg_entry
            if loss_pct < -0.50:  # Never sell at >50% loss via MM
                logger.warning(
                    f"⛔ SELL blocked: price ${price:.4f} is {loss_pct:.0%} below avg ${pos.avg_entry:.4f} — catastrophic loss protection"
                )
                return False

        new_position = pos.net_position - size

        # ── HARD UNIT CAP (RiskGuard fix #1) ──
        if abs(new_position) > self.config.max_units_per_market:
            logger.debug(f"⛔ SELL blocked: {abs(new_position):.0f} units > {self.config.max_units_per_market:.0f} cap")
            return False

        return True
    
    def compute_skew(self, token_id: str, skew_boost: float = 1.0) -> float:
        """
        Compute NON-LINEAR quote skew for natural inventory mean-reversion.

        The quadratic skew means:
          - Low inventory (10-30%): gentle nudge, barely affects quotes
          - Medium inventory (40-60%): moderate pressure, quotes clearly favor reducing side
          - High inventory (70%+): strong pressure, aggressively attracts reducing fills

        This replaces the need for RiskGuard intervention in most cases.
        RiskGuard only kicks in at extreme levels (85%+) as a safety net.

        Skew examples with inventory_skew_factor=1.0, half_spread=1.5¢:
          10% inv → 0.17¢ skew  (invisible)
          30% inv → 0.59¢ skew  (mild)
          50% inv → 1.13¢ skew  (moderate — bid shifts 1¢ toward reducing)
          70% inv → 1.87¢ skew  (strong — most of spread on reducing side)
          90% inv → 2.74¢ skew  (very strong — crosses fair value to attract fills)
        """
        pos = self.get_position(token_id)

        max_units = self.config.max_units_per_market
        max_notional = self.config.max_position_per_market

        if max_units == 0 and max_notional == 0:
            return 0.0

        # Use higher of unit ratio or notional ratio
        unit_ratio = pos.net_position / max(max_units, 1) if max_units > 0 else 0
        notional_ratio = (pos.net_position * max(pos.avg_entry, 0.01)) / max(max_notional, 1)
        if abs(unit_ratio) > abs(notional_ratio):
            inventory_ratio = unit_ratio
        else:
            inventory_ratio = notional_ratio

        # ── NON-LINEAR SKEW ──
        # Quadratic acceleration: skew grows with ratio² making it
        # gentle at low inventory but aggressive at high inventory
        abs_ratio = abs(inventory_ratio)
        if self.config.inventory_skew_nonlinear:
            # nonlinear_factor: 1.0 at 0%, 1.25 at 50%, 1.64 at 80%, 2.0 at 100%
            nonlinear_factor = 1.0 + abs_ratio ** 2
        else:
            nonlinear_factor = 1.0

        effective_skew_factor = self.config.inventory_skew_factor * skew_boost * nonlinear_factor
        skew = -inventory_ratio * effective_skew_factor * self.config.target_half_spread

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
    def remove_position(self, token_id):
        if token_id in self._positions: del self._positions[token_id]; return True
        return False

    
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
                    "peak_price": p.peak_price,
                    "tp1_hit": p.tp1_hit,
                    "tp2_hit": p.tp2_hit,
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
                peak_price=pdata.get("peak_price", 0.0),
                tp1_hit=pdata.get("tp1_hit", False),
                tp2_hit=pdata.get("tp2_hit", False),
            )
            self._positions[tid] = pos
        self._daily_pnl = state.get("daily_pnl", 0.0)
        self._daily_pnl_reset = state.get("daily_pnl_reset", 0.0)
        logger.info(f"Restored {len(self._positions)} positions from state")


# ─────────────────────────────────────────────
# RiskGuard — KPI Monitor + Auto-Fix System
# ─────────────────────────────────────────────

class RiskAction(Enum):
    """Actions the RiskGuard can take."""
    NONE = "none"
    WARN = "warn"                      # Log warning, Telegram alert
    INCREASE_SKEW = "increase_skew"    # Boost skew to reduce inventory faster
    FADEOUT = "fadeout"                 # Reduce quote sizes proportionally
    ONESIDE = "oneside"                # Only quote the reducing side
    HALT_MARKET = "halt_market"        # Stop quoting a specific market
    HALT_ALL = "halt_all"              # Emergency stop all quoting
    REDUCE_SIZE = "reduce_size"        # Reduce max quote size across the board

@dataclass
class MarketKPI:
    """KPIs tracked per market."""
    token_id: str
    inventory_ratio: float = 0.0        # -1.0 to 1.0 (position / max)
    units_ratio: float = 0.0            # abs(units) / max_units
    fill_asymmetry: float = 0.0         # 0.0 = balanced, 1.0 = all one-sided
    fill_bias: str = ""                 # "BUY" or "SELL" — dominant fill side
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    drawdown: float = 0.0              # $ drawdown from peak
    peak_pnl: float = 0.0              # Peak PnL (for drawdown calc)
    spread_capture_rate: float = 0.0   # Realized spread / theoretical spread
    active_action: RiskAction = RiskAction.NONE
    fadeout_factor: float = 1.0        # 1.0 = full size, 0.0 = no quoting
    skew_boost: float = 1.0            # Multiplier on skew_factor
    last_alert_time: float = 0.0       # Throttle alerts

@dataclass
class SystemKPI:
    """System-wide KPIs."""
    total_exposure: float = 0.0
    total_realized_pnl: float = 0.0
    total_unrealized_pnl: float = 0.0
    total_drawdown: float = 0.0
    peak_total_pnl: float = 0.0
    pnl_velocity: float = 0.0          # $/min rate of PnL change
    active_markets: int = 0
    halted_markets: int = 0
    markets_at_warn: int = 0
    markets_at_fadeout: int = 0
    markets_at_oneside: int = 0


class RiskGuard:
    """
    KPI monitoring + automatic risk correction for the Market Maker.

    Monitors:
      1. Inventory ratio per market (units + notional)
      2. Fill asymmetry (detects one-sided accumulation)
      3. Drawdown per market and total
      4. PnL velocity (rate of change)
      5. Spread capture efficiency

    Auto-fixes (escalating severity):
      Level 1 (WARN):       inventory > 50%  → Telegram alert, log warning
      Level 2 (SKEW BOOST): inventory > 50%  → increase skew factor 1x→3x
      Level 3 (FADEOUT):    inventory > 60%  → reduce quote size proportionally
      Level 4 (ONESIDE):    inventory > 80%  → only quote the reducing side
      Level 5 (HALT):       inventory > 95% or drawdown > $20 → stop quoting market
      Level 6 (HALT ALL):   total drawdown > $40 → emergency stop
    """

    def __init__(self, config: MMConfig, inventory: InventoryManager, telegram=None):
        self.config = config
        self.inventory = inventory
        self.telegram = telegram

        self._market_kpis: Dict[str, MarketKPI] = {}
        self._system_kpi = SystemKPI()
        self._fill_history: Dict[str, List[Dict]] = defaultdict(list)  # token → recent fills
        self._pnl_history: List[Tuple[float, float]] = []  # (timestamp, total_pnl)
        self._halted_markets: set = set()
        self._alert_cooldowns: Dict[str, float] = {}  # "alert_key" → last_sent_time

        logger.info("🛡️ RiskGuard initialized")

    def record_fill(self, token_id: str, side: str, price: float, size: float):
        """Record a fill for asymmetry tracking."""
        self._fill_history[token_id].append({
            "time": time.time(),
            "side": side,
            "price": price,
            "size": size,
        })
        # Keep only recent fills
        window = self.config.fill_asymmetry_window * 2
        if len(self._fill_history[token_id]) > window:
            self._fill_history[token_id] = self._fill_history[token_id][-window:]

    def get_market_kpi(self, token_id: str) -> MarketKPI:
        """Get or create KPI tracker for a market."""
        if token_id not in self._market_kpis:
            self._market_kpis[token_id] = MarketKPI(token_id=token_id)
        return self._market_kpis[token_id]

    def is_halted(self, token_id: str) -> bool:
        """Check if a market is halted by RiskGuard."""
        return token_id in self._halted_markets

    def resume_market(self, token_id: str):
        """Manually resume a halted market."""
        self._halted_markets.discard(token_id)
        kpi = self.get_market_kpi(token_id)
        kpi.active_action = RiskAction.NONE
        kpi.fadeout_factor = 1.0
        kpi.skew_boost = 1.0
        logger.info(f"🛡️ Market resumed: {token_id[:12]}...")

    def resume_all(self):
        """Resume all halted markets."""
        self._halted_markets.clear()
        for kpi in self._market_kpis.values():
            kpi.active_action = RiskAction.NONE
            kpi.fadeout_factor = 1.0
            kpi.skew_boost = 1.0
        logger.info("🛡️ All markets resumed")

    async def evaluate(self) -> Dict[str, RiskAction]:
        """
        Run full KPI evaluation and return actions per market.

        Called every riskguard_check_interval seconds from the engine.
        Returns: {token_id: RiskAction}
        """
        actions = {}
        now = time.time()

        # ── Per-market KPIs ──
        for token_id, pos in self.inventory._positions.items():
            if abs(pos.net_position) < 0.01 and pos.n_trades == 0:
                continue

            kpi = self.get_market_kpi(token_id)

            # 1. Inventory ratio (by units)
            max_units = self.config.max_units_per_market
            kpi.units_ratio = abs(pos.net_position) / max(max_units, 1)

            # 2. Inventory ratio (by notional)
            max_notional = self.config.max_position_per_market
            notional = abs(pos.net_position) * max(pos.avg_entry, 0.01)
            kpi.inventory_ratio = notional / max(max_notional, 1)

            # Use the HIGHER of unit ratio or notional ratio for risk decisions
            effective_ratio = max(kpi.units_ratio, kpi.inventory_ratio)

            # 3. PnL tracking
            kpi.realized_pnl = pos.realized_pnl
            kpi.unrealized_pnl = pos.unrealized_pnl
            total_pnl = pos.realized_pnl + pos.unrealized_pnl

            # 4. Drawdown tracking
            if total_pnl > kpi.peak_pnl:
                kpi.peak_pnl = total_pnl
            kpi.drawdown = kpi.peak_pnl - total_pnl

            # 5. Fill asymmetry
            recent_fills = self._fill_history.get(token_id, [])[-self.config.fill_asymmetry_window:]
            if len(recent_fills) >= 4:
                buys = sum(1 for f in recent_fills if f["side"] == "BUY")
                sells = len(recent_fills) - buys
                total = len(recent_fills)
                majority = max(buys, sells)
                kpi.fill_asymmetry = majority / total
                kpi.fill_bias = "BUY" if buys > sells else "SELL"

            # ── Determine action (escalating severity) ──
            action = RiskAction.NONE

            # Level 5: HALT — drawdown or extreme inventory
            if kpi.drawdown > self.config.max_drawdown_per_market:
                action = RiskAction.HALT_MARKET
                self._halted_markets.add(token_id)
                await self._alert(
                    f"halt_{token_id}",
                    f"🛑 <b>MARKET HALTED</b>: {token_id[:16]}...\n"
                    f"Drawdown: ${kpi.drawdown:.2f} > ${self.config.max_drawdown_per_market}\n"
                    f"Position: {pos.net_position:.0f} units @ {pos.avg_entry:.4f}\n"
                    f"uPnL: ${kpi.unrealized_pnl:.2f}"
                )
            elif effective_ratio >= self.config.inventory_halt_pct:
                action = RiskAction.HALT_MARKET
                self._halted_markets.add(token_id)
                await self._alert(
                    f"halt_{token_id}",
                    f"🛑 <b>MARKET HALTED</b>: {token_id[:16]}...\n"
                    f"Inventory: {effective_ratio:.0%} of limit\n"
                    f"Units: {pos.net_position:.0f} / {max_units:.0f}"
                )
            # Level 4: ONE-SIDED quoting
            elif effective_ratio >= self.config.inventory_oneside_pct:
                action = RiskAction.ONESIDE
                kpi.fadeout_factor = 0.3  # Very small size on reducing side
                kpi.skew_boost = self.config.adaptive_skew_multiplier
                await self._alert(
                    f"oneside_{token_id}",
                    f"⚠️ <b>ONE-SIDED MODE</b>: {token_id[:16]}...\n"
                    f"Inventory: {effective_ratio:.0%} — only quoting reducing side\n"
                    f"Units: {pos.net_position:.0f}"
                )
            # Level 3: FADEOUT
            elif effective_ratio >= self.config.inventory_fadeout_pct:
                action = RiskAction.FADEOUT
                # Linear fadeout: at 60% → factor=1.0, at 80% → factor=0.3
                fadeout_range = self.config.inventory_oneside_pct - self.config.inventory_fadeout_pct
                if fadeout_range > 0:
                    progress = (effective_ratio - self.config.inventory_fadeout_pct) / fadeout_range
                    kpi.fadeout_factor = max(0.3, 1.0 - progress * 0.7)
                else:
                    kpi.fadeout_factor = 0.5
                kpi.skew_boost = 1.0 + (self.config.adaptive_skew_multiplier - 1.0) * progress
            # Level 2: SKEW BOOST
            elif effective_ratio >= self.config.adaptive_skew_threshold:
                action = RiskAction.INCREASE_SKEW
                # Linear skew boost: at 50% → 1x, at 60% → adaptive_skew_multiplier
                skew_range = self.config.inventory_fadeout_pct - self.config.adaptive_skew_threshold
                if skew_range > 0:
                    progress = (effective_ratio - self.config.adaptive_skew_threshold) / skew_range
                    kpi.skew_boost = 1.0 + (self.config.adaptive_skew_multiplier - 1.0) * progress
                else:
                    kpi.skew_boost = self.config.adaptive_skew_multiplier
                kpi.fadeout_factor = 1.0  # No size reduction yet
                await self._alert(
                    f"skew_{token_id}",
                    f"📊 <b>SKEW BOOST</b>: {token_id[:16]}...\n"
                    f"Inventory: {effective_ratio:.0%} → skew ×{kpi.skew_boost:.1f}\n"
                    f"Units: {pos.net_position:.0f}"
                )
            # Level 1: WARN on fill asymmetry
            elif kpi.fill_asymmetry > self.config.fill_asymmetry_threshold and len(recent_fills) >= 6:
                action = RiskAction.WARN
                kpi.skew_boost = 1.5  # Mild skew boost on asymmetry
                await self._alert(
                    f"asymmetry_{token_id}",
                    f"⚡ <b>FILL ASYMMETRY</b>: {token_id[:16]}...\n"
                    f"{kpi.fill_bias} bias: {kpi.fill_asymmetry:.0%} of last {len(recent_fills)} fills\n"
                    f"Applying 1.5x skew boost"
                )
            else:
                # All clear — reset corrections
                kpi.fadeout_factor = 1.0
                kpi.skew_boost = 1.0

            kpi.active_action = action
            actions[token_id] = action

        # ── System-wide KPIs ──
        total_rpnl = self.inventory.total_realized_pnl
        total_upnl = self.inventory.total_unrealized_pnl
        total_pnl = total_rpnl + total_upnl

        self._system_kpi.total_exposure = self.inventory.total_exposure
        self._system_kpi.total_realized_pnl = total_rpnl
        self._system_kpi.total_unrealized_pnl = total_upnl
        self._system_kpi.active_markets = len([
            p for p in self.inventory._positions.values() if abs(p.net_position) > 0.01
        ])
        self._system_kpi.halted_markets = len(self._halted_markets)
        self._system_kpi.markets_at_warn = len([
            a for a in actions.values() if a == RiskAction.WARN
        ])
        self._system_kpi.markets_at_fadeout = len([
            a for a in actions.values() if a in (RiskAction.FADEOUT, RiskAction.ONESIDE)
        ])

        # Total drawdown
        if total_pnl > self._system_kpi.peak_total_pnl:
            self._system_kpi.peak_total_pnl = total_pnl
        self._system_kpi.total_drawdown = self._system_kpi.peak_total_pnl - total_pnl

        # PnL velocity ($ per minute over last N minutes)
        self._pnl_history.append((now, total_pnl))
        cutoff = now - self.config.pnl_velocity_window_secs
        self._pnl_history = [(t, p) for t, p in self._pnl_history if t >= cutoff]
        if len(self._pnl_history) >= 2:
            dt_mins = (self._pnl_history[-1][0] - self._pnl_history[0][0]) / 60
            dpnl = self._pnl_history[-1][1] - self._pnl_history[0][1]
            self._system_kpi.pnl_velocity = dpnl / max(dt_mins, 0.1)

        # System-level alerts
        if self._system_kpi.total_drawdown > self.config.max_drawdown_total:
            await self._alert(
                "system_drawdown",
                f"🚨 <b>SYSTEM DRAWDOWN ALERT</b>\n"
                f"Total drawdown: ${self._system_kpi.total_drawdown:.2f} > ${self.config.max_drawdown_total}\n"
                f"Halting ALL markets\n"
                f"rPnL: ${total_rpnl:.2f} | uPnL: ${total_upnl:.2f}"
            )
            # Halt everything
            for token_id in self.inventory._positions:
                self._halted_markets.add(token_id)
                actions[token_id] = RiskAction.HALT_ALL

        if self._system_kpi.pnl_velocity < self.config.pnl_velocity_alert:
            await self._alert(
                "pnl_velocity",
                f"📉 <b>PnL VELOCITY ALERT</b>\n"
                f"Losing ${abs(self._system_kpi.pnl_velocity):.2f}/min over last "
                f"{self.config.pnl_velocity_window_secs/60:.0f} min\n"
                f"Total PnL: ${total_pnl:.2f}"
            )

        return actions

    async def _alert(self, key: str, message: str):
        """Send throttled Telegram alert."""
        now = time.time()
        last = self._alert_cooldowns.get(key, 0)
        if now - last < self.config.riskguard_telegram_cooldown:
            return

        self._alert_cooldowns[key] = now
        logger.warning(f"RiskGuard: {message}")

        if self.telegram:
            try:
                await self.telegram.send(f"🛡️ RiskGuard\n{message}")
            except Exception as e:
                logger.error(f"RiskGuard Telegram alert failed: {e}")

    def get_stats(self) -> Dict:
        """Full RiskGuard status for dashboard."""
        return {
            "system": {
                "total_exposure": round(self._system_kpi.total_exposure, 2),
                "total_rpnl": round(self._system_kpi.total_realized_pnl, 2),
                "total_upnl": round(self._system_kpi.total_unrealized_pnl, 2),
                "total_drawdown": round(self._system_kpi.total_drawdown, 2),
                "peak_pnl": round(self._system_kpi.peak_total_pnl, 2),
                "pnl_velocity": round(self._system_kpi.pnl_velocity, 4),
                "pnl_velocity_per_hour": round(self._system_kpi.pnl_velocity * 60, 2),
                "active_markets": self._system_kpi.active_markets,
                "halted_markets": self._system_kpi.halted_markets,
                "markets_at_warn": self._system_kpi.markets_at_warn,
                "markets_at_fadeout": self._system_kpi.markets_at_fadeout,
            },
            "markets": {
                tid: {
                    "inventory_ratio": round(kpi.inventory_ratio, 3),
                    "units_ratio": round(kpi.units_ratio, 3),
                    "effective_ratio": round(max(kpi.inventory_ratio, kpi.units_ratio), 3),
                    "fill_asymmetry": round(kpi.fill_asymmetry, 2),
                    "fill_bias": kpi.fill_bias,
                    "drawdown": round(kpi.drawdown, 2),
                    "peak_pnl": round(kpi.peak_pnl, 2),
                    "rpnl": round(kpi.realized_pnl, 2),
                    "upnl": round(kpi.unrealized_pnl, 2),
                    "action": kpi.active_action.value,
                    "fadeout_factor": round(kpi.fadeout_factor, 2),
                    "skew_boost": round(kpi.skew_boost, 1),
                    "halted": tid in self._halted_markets,
                }
                for tid, kpi in self._market_kpis.items()
            },
            "halted_market_ids": list(self._halted_markets),
            "config": {
                "max_units_per_market": self.config.max_units_per_market,
                "inventory_warn_pct": self.config.inventory_warn_pct,
                "inventory_fadeout_pct": self.config.inventory_fadeout_pct,
                "inventory_oneside_pct": self.config.inventory_oneside_pct,
                "inventory_halt_pct": self.config.inventory_halt_pct,
                "max_drawdown_per_market": self.config.max_drawdown_per_market,
                "max_drawdown_total": self.config.max_drawdown_total,
            }
        }


# ─────────────────────────────────────────────
# Spread Analyzer (Market Selection)
# ─────────────────────────────────────────────

class SpreadAnalyzer:
    """Selects optimal markets for market-making."""
    
    def __init__(self, config: MMConfig):
        self.config = config
        self._scores: Dict[str, float] = {}
    _BLACKLIST_RE = __import__("re").compile(
        r"\bjesus\b|\bsatan\b|\bflat.*earth\b|\bsimulation\b|\btime.*travel\b|"
        r"\bzodiac\b|\bhoroscope\b|\bastrology\b|\bbigfoot\b|\bloch.*ness\b|"
        r"\billuminati\b|\breptilian\b",
        __import__("re").IGNORECASE)

    def score_market(self, market: MarketInfo) -> Tuple[bool, float, str]:
        """
        Score a market for MM suitability.
        
        Returns: (eligible, score, reason)
        Score = spread_edge * liquidity_score * time_score
        """
        # Blacklist filter
        if self._BLACKLIST_RE.search(market.question):
            return False, 0, f"Blacklisted: {market.question[:50]}"
        
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
        if mid < 0.02 or mid > 0.98:
            return False, 0, f"Price {mid:.2f} too extreme for MM"
        
        # Score components (0-1 each)
        
        # Spread edge: how much profit per round-trip after fees
        gross_spread = market.spread_cents / 100.0
        fees = 2 * self.config.fee_rate * mid  # Both sides
        net_spread = gross_spread - fees
        # Cap reward: 3-5¢ net spread is ideal, >10¢ is suspicious (dead market)
        # Zero volume = dead market, hard skip
        if market.volume_24h < 50.0:
            return -999.0  # Hard reject: no volume = no fills

        if net_spread > 0.10:  # >10¢ net spread = dead market, penalize
            spread_score = max(0, 0.5 - (net_spread - 0.10) * 2)  # Drops to 0 at 35¢
        else:
            spread_score = max(0, min(1, net_spread / 0.05))  # 5¢ net = perfect
        
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
        from collections import Counter
        reject_reasons = Counter()
        for m in candidates:
            eligible, score, reason = self.score_market(m)
            if eligible:
                scored.append((score, m, reason))
                self._scores[m.condition_id] = score
            else:
                # Categorize rejection
                if "Spread too tight" in reason:
                    reject_reasons["tight_spread"] += 1
                elif "Low liquidity" in reason:
                    reject_reasons["low_liq"] += 1
                elif "Spread too wide" in reason:
                    reject_reasons["wide_spread"] += 1
                elif "too extreme" in reason:
                    reject_reasons["extreme_price"] += 1
                elif "Negative net" in reason:
                    reject_reasons["neg_net"] += 1
                elif "resolution" in reason:
                    reject_reasons["close_resolution"] += 1
                elif "Blacklisted" in reason:
                    reject_reasons["blacklisted"] += 1
                else:
                    reject_reasons[reason[:30]] += 1
        
        scored.sort(key=lambda x: x[0], reverse=True)
        selected = [m for _, m, _ in scored[:self.config.max_markets]]
        
        if reject_reasons:
            logger.info(f"Market rejections: {dict(reject_reasons)}")
        
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
    
    def __init__(self, config: MMConfig, ou: OUCalibrator, inventory: InventoryManager, hmm: RegimeDetector = None):
        self.config = config
        self.ou = ou
        self.inventory = inventory
        self.hmm = hmm
    
    def generate_quotes(self, token_id: str, current_mid: float,
                         riskguard: 'RiskGuard' = None) -> QuotePair:
        """
        Generate a bid/ask quote pair.

        Fair value comes from OU model (if calibrated) or market mid.
        Quotes are skewed by inventory to reduce directional exposure.
        Random jitter added for stealth.

        RiskGuard integration:
          - Applies skew_boost (increased inventory pressure)
          - Applies fadeout_factor (reduced quote size)
          - Enforces one-sided quoting when inventory critical
          - Returns empty pair if market is halted
        """
        # ── RiskGuard: Check if market is halted ──
        if riskguard and riskguard.is_halted(token_id):
            return QuotePair(token_id=token_id, condition_id="",
                             fair_value=current_mid, last_update=time.time())

        # Get RiskGuard adjustments
        rg_kpi = riskguard.get_market_kpi(token_id) if riskguard else None
        skew_boost = rg_kpi.skew_boost if rg_kpi else 1.0
        fadeout_factor = rg_kpi.fadeout_factor if rg_kpi else 1.0
        rg_action = rg_kpi.active_action if rg_kpi else RiskAction.NONE

        # 0. Skip dust markets — don't quote if mid is too low
        if current_mid < self.config.min_fair_value:
            logger.debug(f"Skip {token_id[:12]}: mid ${current_mid:.3f} < min ${self.config.min_fair_value:.3f}")
            return None

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

        # 2. Dynamic half-spread from OU calibration
        #    Stationary std of OU process: sigma / sqrt(2*theta)
        #    Tighter spreads for fast mean-reverting markets, wider for volatile ones
        ou_params_spread = self.ou.get_params(token_id)
        if ou_params_spread and ou_params_spread.n_obs >= self.ou.config.ou_min_observations:
            import math as _m
            stationary_std = ou_params_spread.sigma / _m.sqrt(2 * max(ou_params_spread.theta, 0.001))
            half_spread = max(
                self.config.min_spread_cents / 100.0 / 2,
                min(
                    self.config.max_spread_cents / 100.0 / 2,
                    1.5 * stationary_std
                )
            )
        else:
            half_spread = self.config.target_half_spread

        # HMM regime: scale spread
        if self.hmm:
            half_spread *= self.hmm.get_spread_multiplier(token_id)

        # 3. Inventory skew (with RiskGuard boost)
        skew = self.inventory.compute_skew(token_id, skew_boost=skew_boost)

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

        # ── BREAKEVEN FLOOR ──
        # Never place ASK below avg_entry + margin (prevent selling at a loss)
        if self.config.breakeven_floor:
            pos = self.inventory.get_position(token_id)
            if pos.net_position > 0 and pos.avg_entry > 0:
                min_ask = pos.avg_entry + self.config.breakeven_min_margin
                if ask_price < min_ask:
                    logger.info(
                        f"🛡️ BREAKEVEN FLOOR: ask {ask_price:.3f} < entry {pos.avg_entry:.3f} + "
                        f"{self.config.breakeven_min_margin:.3f} margin → raised to {min_ask:.3f}"
                    )
                    ask_price = min_ask

        # Clamp to valid range
        bid_price = max(self.config.min_fair_value, min(0.98, round(bid_price, 3)))

        # Block penny bids — never bid below 2 cents (wastes bandwidth + buys trash)
        if bid_price < 0.02:
            logger.debug(f"Blocked penny bid: {token_id[:12]} bid=${bid_price:.3f} < $0.02")
            return QuotePair(token_id=token_id, bid=None, ask=None)
        ask_price = max(0.02, min(0.99, round(ask_price, 3)))

        # Ensure bid < ask (minimum 1¢ spread)
        if bid_price >= ask_price:
            mid = (bid_price + ask_price) / 2
            bid_price = round(mid - 0.005, 3)
            ask_price = round(mid + 0.005, 3)

        # 7. Size with Kelly and jitter, THEN apply fadeout
        base_size = self._kelly_size(token_id, fair_value, bid_price, ask_price)

        # Risk Calculator advisory (caps size but doesn't block MM operation)
        if _HAS_RISK_CALC and base_size > 0:
            try:
                bankroll = getattr(self.config, 'bankroll', 463)
                exposure = self.inventory.total_exposure
                free = max(0, bankroll - exposure)
                calc = RiskCalculator.from_profile(
                    "conservative", bankroll=bankroll,
                    free_usdc=free, current_exposure=exposure
                )
                decision = calc.size_trade(
                    entry_price=bid_price,
                    confidence=fair_value,
                    side="YES",
                    market_question=token_id[:20],
                )
                if decision.approved and decision.cost > 0:
                    risk_size = decision.cost / bid_price if bid_price > 0 else base_size
                    base_size = min(base_size, risk_size)
                # If rejected, log but DON'T block — MM uses its own min/max sizing
                elif not decision.approved:
                    logger.debug(f"Risk calc advisory: {decision.reason}")
            except Exception:
                pass  # Fall through to normal sizing
        size = round(base_size * size_jitter_mult * fadeout_factor, 1)
        size = max(self.config.min_quote_size, min(self.config.max_quote_size, size))

        # If fadeout makes size too small, don't quote
        if fadeout_factor < 0.3 and size <= self.config.min_quote_size:
            return QuotePair(token_id=token_id, condition_id="",
                             fair_value=fair_value, last_update=time.time())

        # 8. Check inventory limits + RiskGuard one-sided enforcement
        bid_quote = None
        ask_quote = None

        pos = self.inventory.get_position(token_id)

        # ── RiskGuard: ONE-SIDED mode ──
        # Only quote the side that REDUCES inventory
        if rg_action == RiskAction.ONESIDE:
            if pos.net_position > 0:
                # Long → only quote ASK (selling reduces inventory)
                if self.inventory.can_sell(token_id, size, ask_price):
                    ask_quote = Quote(token_id=token_id, side=QuoteSide.ASK,
                                     price=ask_price, size=size)
                logger.debug(f"🛡️ ONE-SIDED: long {pos.net_position:.0f} → ASK only")
            elif pos.net_position < 0:
                # Short → only quote BID (buying reduces inventory)
                if self.inventory.can_buy(token_id, size, bid_price):
                    bid_quote = Quote(token_id=token_id, side=QuoteSide.BID,
                                     price=bid_price, size=size)
                logger.debug(f"🛡️ ONE-SIDED: short {pos.net_position:.0f} → BID only")
        else:
            # Normal two-sided quoting
            if self.inventory.can_buy(token_id, size, bid_price):
                bid_quote = Quote(
                    token_id=token_id, side=QuoteSide.BID,
                    price=bid_price, size=size,
                )

            if self.inventory.can_sell(token_id, size, ask_price):
                ask_quote = Quote(
                    token_id=token_id, side=QuoteSide.ASK,
                    price=ask_price, size=size,
                )

        pair = QuotePair(
            token_id=token_id,
            condition_id="",  # Set by caller
            bid=bid_quote,
            ask=ask_quote,
            fair_value=fair_value,
            last_update=time.time(),
        )

        # ── Multi-level quoting: add deeper BID/ASK levels ──
        if self.config.quote_levels > 1:
            for lvl in range(1, self.config.quote_levels):
                spacing = self.config.level_spacing_pct * lvl
                size_factor = self.config.level_size_decay ** lvl
                lvl_size = max(5.0, round(size * size_factor, 1))

                # Deeper bid (lower price)
                lvl_bid_price = round(bid_price - bid_price * spacing, 3)
                lvl_bid_price = max(self.config.min_fair_value, lvl_bid_price)
                if rg_action != RiskAction.ONESIDE or (pos.net_position <= 0):
                    if self.inventory.can_buy(token_id, lvl_size, lvl_bid_price):
                        pair.extra_bids.append(Quote(
                            token_id=token_id, side=QuoteSide.BID,
                            price=lvl_bid_price, size=lvl_size
                        ))

                # Deeper ask (higher price)
                lvl_ask_price = round(ask_price + ask_price * spacing, 3)
                lvl_ask_price = min(0.99, lvl_ask_price)
                # Respect breakeven floor on deeper levels too
                if self.config.breakeven_floor and pos.net_position > 0 and pos.avg_entry > 0:
                    min_lvl_ask = pos.avg_entry + self.config.breakeven_min_margin
                    lvl_ask_price = max(lvl_ask_price, round(min_lvl_ask, 3))
                if rg_action != RiskAction.ONESIDE or (pos.net_position >= 0):
                    if self.inventory.can_sell(token_id, lvl_size, lvl_ask_price):
                        pair.extra_asks.append(Quote(
                            token_id=token_id, side=QuoteSide.ASK,
                            price=lvl_ask_price, size=lvl_size
                        ))

        # ── Inventory-aware enforcement ──
        # If we have ANY inventory >= 5 shares, ALWAYS place SELL to close
        if pos.net_position >= 5 and ask_quote is None and rg_action != RiskAction.HALT_MARKET:
            inv_ratio = pos.net_position / max(self.config.max_units_per_market, 1)
            aggressive_ask = round(fair_value + half_spread * 0.3, 3)
            aggressive_ask = max(aggressive_ask, 0.01)
            # Respect breakeven floor even in Force ASK mode
            if self.config.breakeven_floor and pos.avg_entry > 0:
                min_force_ask = pos.avg_entry + self.config.breakeven_min_margin
                if aggressive_ask < min_force_ask:
                    aggressive_ask = round(min_force_ask, 3)
                    logger.info(f"🛡️ Force ASK raised to breakeven: {aggressive_ask:.3f}")
            ask_size = max(5.0, min(size, pos.net_position))  # Floor at 5 shares
            ask_quote = Quote(token_id=token_id, side=QuoteSide.ASK,
                             price=aggressive_ask, size=round(ask_size, 1))
            logger.info(f"📉 Force ASK: inv={pos.net_position:.0f} ({inv_ratio:.0%}) → ask {aggressive_ask:.3f}x{ask_size:.0f}")
            pair.ask = ask_quote

        # If inventory > 80% of max, block new buys entirely
        if pos.net_position > 0:
            inv_ratio = pos.net_position / max(self.config.max_units_per_market, 1)
            if inv_ratio > 0.8 and pair.bid:
                logger.info(f"⛔ BID blocked: inventory {inv_ratio:.0%} > 80% cap")
                pair.bid = None

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
        
        if self.hmm:
            kelly_size *= self.hmm.get_position_multiplier(token_id)
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
        self.hmm = RegimeDetector()
        self.inventory = InventoryManager(config)
        self.spread_analyzer = SpreadAnalyzer(config)
        self.quote_gen = QuoteGenerator(config, self.ou, self.inventory, self.hmm)
        self.riskguard = RiskGuard(config, self.inventory, telegram)

        # State
        self._active_markets: List[MarketInfo] = []
        self._active_quotes: Dict[str, QuotePair] = {}  # token_id → QuotePair
        self._running = False
        self._paper_mode = True
        self._paper_fills: List[Dict] = []  # Simulated fills for paper mode
        self._trade_log: List[Dict] = []  # All fills (paper + live), last 200
        self._last_clob_balance: float = 0.0  # Actual CLOB balance, refreshed periodically
        self._force_kill = os.environ.get("MM_KILL_SWITCH", "false").lower() in ("true", "1")  # Kill via env or /stop
        self._kill_alerted = False  # One-shot flag for kill-switch Telegram alert

        # Stats
        self._started_at = 0.0
        self._quote_count = 0

        # Liquidity checker — prevents entering illiquid markets
        if _HAS_LIQUIDITY_CHECKER:
            self._liquidity_checker = LiquidityChecker(LiquidityConfig(
                min_exit_depth_usd=30.0,     # At least $30 exit depth (was $10)
                min_depth_multiple=2.0,       # Exit depth >= 2x our trade (was 1.5x)
                max_exit_slippage_pct=8.0,    # Max 8% slippage (was 15%)
                max_spread_pct=12.0,          # Max 12% spread (was 20%)
                min_exit_levels=2,            # At least 2 exit levels (was 1)
                min_depth_ratio=0.0,          # Disabled — Polymarket binary markets are asymmetric
                max_depth_ratio=999.0,        # Disabled
                dust_filter_usd=0.10,
            ))
            logger.info("Liquidity checker enabled")
        else:
            self._liquidity_checker = None
        self._fill_count = 0
        self._recently_cancelled = set()  # Order IDs we cancelled (not filled)
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
        
        # Run main loops (including RiskGuard + SL/TP)
        # Run main loops (including RiskGuard)
        # Note: SL/TP exits are handled by ExitManager at portfolio level
        await asyncio.gather(
            self._market_scan_loop(),
            self._quote_loop(),
            self._monitor_loop(),
            self._calibration_loop(),
            self._riskguard_loop(),
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
                    if not self._kill_alerted:
                        self._kill_alerted = True
                        reason = "manual /kill" if self._force_kill else f"daily loss ${self.inventory.daily_pnl:.2f}"
                        logger.warning(f"KILL SWITCH ACTIVE — {reason}")
                        await self.telegram.send(
                            f"🚨 <b>MM KILL SWITCH TRIGGERED</b>\n"
                            f"Reason: {reason}\n"
                            f"Daily PnL: ${self.inventory.daily_pnl:.2f}\n"
                            f"Limit: -${self.config.max_loss_per_day}\n"
                            f"All quoting halted. /resume to restart."
                        )
                    await asyncio.sleep(60)
                    continue
                elif self._kill_alerted:
                    # Kill condition cleared (e.g. daily reset) — reset flag
                    self._kill_alerted = False
                
                for market in self._active_markets:
                    if market is None:
                        continue
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
        
        # ── Smart mid: Use Gamma reference + filtered CLOB orderbook ──
        # Gamma API bestBid/bestAsk reflect real market consensus
        # CLOB orderbook has dust orders at $0.01/$0.99 that distort mid
        gamma_mid = market.mid_price  # From Gamma API (reliable)
        
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        
        # Filter dust: ignore bids < 5% of gamma_mid and asks > 1-(1-gamma_mid)*0.05
        dust_bid_floor = max(0.02, gamma_mid * 0.3)  # At least 30% of gamma mid
        dust_ask_ceil = min(0.98, 1.0 - (1.0 - gamma_mid) * 0.3)
        
        real_bids = [float(b.get("price", 0)) for b in bids if float(b.get("price", 0)) >= dust_bid_floor]
        real_asks = [float(a.get("price", 0)) for a in asks if 0 < float(a.get("price", 0)) <= dust_ask_ceil]
        
        best_bid = max(real_bids) if real_bids else 0
        best_ask = min(real_asks) if real_asks else 0
        
        # Compute mid: prefer CLOB filtered mid, fallback to Gamma mid
        if best_bid > 0 and best_ask > 0 and best_ask > best_bid:
            mid = (best_bid + best_ask) / 2
        else:
            mid = gamma_mid  # Trust Gamma when CLOB is empty/extreme
            # Set best_bid/best_ask from Gamma for downstream use
            if best_bid <= 0:
                best_bid = market.best_bid
            if best_ask <= 0:
                best_ask = market.best_ask
        
        # Record price for OU calibration
        self.ou.record_price(token_id, mid)
        self.hmm.record_price(token_id, mid)
        
        # ── Volume decay: reduce size if market volume dropped ──
        volume_factor = 1.0
        if self.config.volume_decay_enabled and hasattr(market, 'volume_24h') and market.volume_24h > 0:
            # Compare current 1h volume pace vs 24h average
            # If volume_24h is low relative to market cap, reduce exposure
            if market.volume_24h < market.liquidity * 0.01:  # Less than 1% turnover
                volume_factor = max(self.config.volume_decay_min_factor, market.volume_24h / max(market.liquidity * 0.01, 1))
                if volume_factor < 0.9:
                    logger.debug(f"📉 Volume decay: {market.question[:30]} vol_factor={volume_factor:.2f}")

        # ── PRE-TRADE LIQUIDITY CHECK ──
        # Don't enter markets where we can't exit
        liq_result = self._liquidity_checker.analyze(ob, "buy", 15.0, mid)
        if not liq_result.safe_to_enter:
            logger.info(f"LIQUIDITY BLOCKED: {market.question[:35]} | {liq_result.reason}")
            # Cancel any existing quotes for this market
            if token_id in self._active_quotes:
                await self._cancel_quotes(token_id)
                del self._active_quotes[token_id]
            return


        # ── Time proximity: reduce size near resolution ──
        time_factor = 1.0
        if self.config.time_proximity_enabled and hasattr(market, 'hours_to_resolution'):
            hrs = market.hours_to_resolution
            if 0 < hrs < self.config.time_proximity_hours:
                # Linear decay from 1.0 at 48h to min_factor at 0h
                time_factor = max(
                    self.config.time_proximity_min_factor,
                    hrs / self.config.time_proximity_hours
                )
                if time_factor < 0.9:
                    logger.debug(f"⏰ Time proximity: {market.question[:30]} {hrs:.0f}h left, factor={time_factor:.2f}")

        # Generate new quotes (with RiskGuard integration)
        pair = self.quote_gen.generate_quotes(token_id, mid, riskguard=self.riskguard)
        if market is None or not hasattr(market, 'condition_id'):
            logger.warning(f"Skipping quote: market object invalid for {token_id[:20]}")
            return
        pair.condition_id = market.condition_id
        
        # Apply volume + time factors to quote sizes
        size_factor = volume_factor * time_factor
        if size_factor < 0.95:
            if pair.bid:
                pair.bid.size = max(5.0, round(pair.bid.size * size_factor, 1))
            if pair.ask:
                pair.ask.size = max(5.0, round(pair.ask.size * size_factor, 1))
            for eb in pair.extra_bids:
                eb.size = max(5.0, round(eb.size * size_factor, 1))
            for ea in pair.extra_asks:
                ea.size = max(5.0, round(ea.size * size_factor, 1))

        if self._paper_mode:
            # Paper mode: just track quotes, simulate fills
            # ── Place extra levels (multi-level quoting) ──
            for extra_bid in pair.extra_bids:
                extra_bid.size = max(5.0, extra_bid.size)
                cost = extra_bid.price * extra_bid.size
                if cost > available or cost < 1.0:
                    continue
                order = await self.clob.place_limit_order(
                    token_id=token_id, side="BUY",
                    price=extra_bid.price, size=extra_bid.size,
                )
                if order:
                    extra_bid.order_id = order.get("id")
                    extra_bid.placed_at = time.time()
                    available -= cost

            for extra_ask in pair.extra_asks:
                extra_ask.size = max(5.0, extra_ask.size)
                cost = extra_ask.price * extra_ask.size
                if cost > available or cost < 1.0:
                    continue
                order = await self.clob.place_limit_order(
                    token_id=token_id, side="SELL",
                    price=extra_ask.price, size=extra_ask.size,
                )
                if order:
                    extra_ask.order_id = order.get("id")
                    extra_ask.placed_at = time.time()

            self._active_quotes[token_id] = pair
            self._simulate_fills(token_id, pair, best_bid, best_ask)
            self._quote_count += 1
        else:
            # Live mode: cancel old quotes, place new ones
            await self._cancel_quotes(token_id)

            # ── LIQUIDITY CHECK before placing orders ──
            if self._liquidity_checker and ob:
                liq = self._liquidity_checker.analyze(ob, "buy", pair.bid.size if pair.bid else 0, mid)
                if not liq.safe_to_enter and pair.bid:
                    logger.info(
                        f"⛔ LIQUIDITY BLOCK: {token_id[:8]}... — {liq.reason} "
                        f"(score {liq.overall_score}/100, exit_depth ${liq.exit_depth_usd:.0f})"
                    )
                    pair.bid = None  # Don't place BUY if we can't exit

            # Check available collateral before placing
            locked = self._collateral_locked()
            # Use actual CLOB balance if available, else fall back to config cap
            balance_cap = self.config.max_total_inventory
            if hasattr(self, '_last_clob_balance') and self._last_clob_balance > 0:
                balance_cap = min(self._last_clob_balance, self.config.max_total_inventory)
            available = balance_cap - locked

            # ── CAPITAL PRESERVATION GATE ──
            # Enforces risk limits BEFORE any BUY order is placed
            if pair.bid and hasattr(self, '_last_clob_balance') and self._last_clob_balance > 0:
                equity = self._last_clob_balance + locked  # Total equity = free + locked
                free_pct = self._last_clob_balance / equity * 100 if equity > 0 else 0
                order_cost_est = pair.bid.price * pair.bid.size

                # RULE 1: Reserve minimum 25% of equity as free USDC
                min_reserve = equity * 0.25
                if self._last_clob_balance - order_cost_est < min_reserve:
                    logger.info(
                        f"\U0001f6e1 CAPITAL GATE: reserve violation — "
                        f"free ${self._last_clob_balance:.0f} - order ${order_cost_est:.0f} "
                        f"< reserve ${min_reserve:.0f} (25% of ${equity:.0f})"
                    )
                    pair.bid = None

                # RULE 2: Max 3% of equity per single trade
                if pair.bid:
                    max_per_trade = equity * 0.03
                    if order_cost_est > max_per_trade:
                        logger.info(
                            f"\U0001f6e1 CAPITAL GATE: trade too large — "
                            f"${order_cost_est:.1f} > 3% of equity (${max_per_trade:.1f})"
                        )
                        # Reduce size to fit within limit
                        max_size = max_per_trade / pair.bid.price if pair.bid.price > 0 else 0
                        if max_size >= 5.0:
                            pair.bid.size = round(max_size, 1)
                            order_cost_est = pair.bid.price * pair.bid.size
                        else:
                            pair.bid = None

                # RULE 3: Max exposure per market (25% of equity)
                if pair.bid:
                    pos = self.inventory.get_position(token_id)
                    current_market_value = pos.net_position * pos.avg_entry if pos.net_position > 0 else 0
                    max_market = equity * 0.25
                    if current_market_value + order_cost_est > max_market:
                        logger.info(
                            f"\U0001f6e1 CAPITAL GATE: market concentration — "
                            f"${current_market_value:.0f} + ${order_cost_est:.0f} > 25% of equity (${max_market:.0f})"
                        )
                        pair.bid = None

                # RULE 4: Max total exposure 70% (keep 30% liquid)
                if pair.bid:
                    total_exposure_pct = locked / equity * 100 if equity > 0 else 100
                    if total_exposure_pct > 70:
                        logger.info(
                            f"\U0001f6e1 CAPITAL GATE: max exposure — "
                            f"{total_exposure_pct:.0f}% locked > 70% limit"
                        )
                        pair.bid = None

            if pair.bid:
                # Enforce Polymarket minimum 5 shares
                pair.bid.size = max(5.0, pair.bid.size)  # Polymarket min 5 shares
                order_cost = pair.bid.price * pair.bid.size
                # Polymarket rejects marketable orders < $1
                if order_cost < 1.0:
                    pair.bid.size = max(pair.bid.size, 1.0 / pair.bid.price + 1)
                    order_cost = pair.bid.price * pair.bid.size
                if order_cost > available:
                    logger.debug(f"⛔ BID skipped: ${order_cost:.1f} > ${available:.1f} available (locked=${locked:.1f})")
                    pair.bid = None
                else:
                    order = await self.clob.place_limit_order(
                        token_id=token_id,
                        side="BUY",
                        price=pair.bid.price,
                        size=pair.bid.size,
                    )
                    if order:
                        pair.bid.order_id = order.get("id")
                        pair.bid.placed_at = time.time()
                        available -= order_cost
                    else:
                        pair.bid = None  # Order rejected (balance/precision)

            if pair.ask:
                pair.ask.size = max(5.0, pair.ask.size)  # Polymarket min 5 shares

                # ── CRITICAL: verify we actually have tokens to sell ──
                pos = self.inventory.get_position(token_id)
                if pos.net_position < pair.ask.size:
                    if pos.net_position < 1:
                        logger.debug(f"⛔ ASK blocked pre-CLOB: no tokens (net={pos.net_position:.0f})")
                        pair.ask = None
                    else:
                        # Reduce to what we actually have
                        pair.ask.size = max(5.0, pos.net_position)
                        logger.debug(f"⛔ ASK reduced to inventory: {pair.ask.size:.0f} (had {pos.net_position:.0f})")

                if pair.ask:
                    order_cost = pair.ask.price * pair.ask.size
                    # Polymarket rejects marketable orders < $1
                    if order_cost < 1.0:
                        pair.ask.size = max(pair.ask.size, 1.0 / pair.ask.price + 1)
                        order_cost = pair.ask.price * pair.ask.size
                    if order_cost > available:
                        logger.debug(f"⛔ ASK skipped: ${order_cost:.1f} > ${available:.1f} available (locked=${locked:.1f})")
                        pair.ask = None
                    if pair.ask is not None:
                        order = await self.clob.place_limit_order(
                            token_id=token_id,
                            side="SELL",
                            price=pair.ask.price,
                            size=pair.ask.size,
                        )
                        if order:
                            pair.ask.order_id = order.get("id")
                            pair.ask.placed_at = time.time()
                        else:
                            pair.ask = None  # Order rejected
            
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
            fill_prob = 0.50  # Market crossed us — queue priority matters
        elif bid_distance <= 0.01:
            fill_prob = 0.08  # Within 1¢
        elif bid_distance <= 0.02:
            fill_prob = 0.02  # Within 2¢
        else:
            fill_prob = 0.0
        
        # Re-check inventory before filling
        if fill_prob > 0 and random.random() < fill_prob:
            if self.inventory.can_buy(token_id, pair.bid.size, pair.bid.price):
                self.inventory.record_fill(
                    token_id, "BUY", pair.bid.price, pair.bid.size
                )
                self.riskguard.record_fill(token_id, "BUY", pair.bid.price, pair.bid.size)
                self._fill_count += 1
                self._total_spread_captured += pair.fair_value - pair.bid.price
                self._paper_fills.append({
                    "time": time.time(),
                    "token_id": token_id,
                    "side": "BUY",
                    "price": pair.bid.price,
                    "size": pair.bid.size,
                })
                self._log_trade(token_id, "BUY", pair.bid.price, pair.bid.size)
                logger.info(f"📗 PAPER FILL BUY {pair.bid.size:.0f} @ {pair.bid.price:.3f} (prob={fill_prob:.0%}, dist={bid_distance:.4f})")
            else:
                logger.debug(f"⛔ BUY fill blocked by inventory limit on {token_id[:8]}")

        # ASK side: fills when buyers come up to our level
        ask_distance = abs(pair.ask.price - market_bid)
        if market_bid >= pair.ask.price:
            fill_prob = 0.50  # Market crossed us
        elif ask_distance <= 0.01:
            fill_prob = 0.08  # Within 1¢
        elif ask_distance <= 0.02:
            fill_prob = 0.02  # Within 2¢
        else:
            fill_prob = 0.0

        # Re-check inventory before filling
        if fill_prob > 0 and random.random() < fill_prob:
            if self.inventory.can_sell(token_id, pair.ask.size, pair.ask.price):
                self.inventory.record_fill(
                    token_id, "SELL", pair.ask.price, pair.ask.size
                )
                self.riskguard.record_fill(token_id, "SELL", pair.ask.price, pair.ask.size)
                self._fill_count += 1
                self._total_spread_captured += pair.ask.price - pair.fair_value
                self._paper_fills.append({
                    "time": time.time(),
                    "token_id": token_id,
                    "side": "SELL",
                    "price": pair.ask.price,
                    "size": pair.ask.size,
                })
                self._log_trade(token_id, "SELL", pair.ask.price, pair.ask.size)
                logger.info(f"📕 PAPER FILL SELL {pair.ask.size:.0f} @ {pair.ask.price:.3f} (prob={fill_prob:.0%}, dist={ask_distance:.4f})")
            else:
                logger.debug(f"⛔ SELL fill blocked by inventory limit on {token_id[:8]}")
    
    def _collateral_locked(self) -> float:
        """Calculate total collateral locked in active BUY orders."""
        locked = 0.0
        for pair in self._active_quotes.values():
            if pair.bid and pair.bid.order_id:
                locked += pair.bid.price * pair.bid.size
            if pair.ask and pair.ask.order_id:
                locked += pair.ask.price * pair.ask.size
        return locked

    def _log_trade(self, token_id: str, side: str, price: float, size: float, market_name: str = ""):
        """Log a trade fill for dashboard display."""
        trade = {
            "time": time.time(),
            "token_id": token_id,
            "side": side,
            "price": price,
            "size": size,
            "notional": round(price * size, 2),
            "market": market_name or token_id[:16],
            "engine": "MM",
            "mode": "PAPER" if self._paper_mode else "LIVE",
        }
        self._trade_log.append(trade)
        if len(self._trade_log) > 200:
            self._trade_log = self._trade_log[-200:]

    async def _cancel_quotes(self, token_id: str):
        """Cancel existing quotes for a token."""
        pair = self._active_quotes.get(token_id)
        if not pair:
            return

        if pair.bid and pair.bid.order_id:
            self._recently_cancelled.add(pair.bid.order_id)
            await self.clob.cancel_order(pair.bid.order_id)
            pair.bid = None  # Clear immediately to prevent phantom detection
        if pair.ask and pair.ask.order_id:
            self._recently_cancelled.add(pair.ask.order_id)
            await self.clob.cancel_order(pair.ask.order_id)
            pair.ask = None  # Clear immediately to prevent phantom detection
    
    # ── Loop 3: Monitor Fills (every 10s) ──
        # Cancel extra levels
        if pair:
            for eb in getattr(pair, 'extra_bids', []):
                if eb.order_id:
                    try:
                        await self.clob.cancel_order(eb.order_id)
                    except Exception:
                        pass
            for ea in getattr(pair, 'extra_asks', []):
                if ea.order_id:
                    try:
                        await self.clob.cancel_order(ea.order_id)
                    except Exception:
                        pass

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
                        market_name = next(
                            (m.question for m in self._active_markets if m.token_id_yes == token_id or m.token_id_no == token_id),
                            token_id[:16]
                        )
                        if pair.bid and pair.bid.order_id:
                            if not any(o.get("id") == pair.bid.order_id for o in open_orders):
                                if pair.bid.order_id in self._recently_cancelled:
                                    # Was cancelled by us, NOT a fill
                                    logger.debug(f"Bid {pair.bid.order_id[:12]} was cancelled, not filled")
                                    self._recently_cancelled.discard(pair.bid.order_id)
                                    pair.bid = None
                                else:
                                    # Genuinely filled
                                    self.inventory.record_fill(
                                        token_id, "BUY", pair.bid.price, pair.bid.size
                                    )
                                    self.riskguard.record_fill(token_id, "BUY", pair.bid.price, pair.bid.size)
                                    self._fill_count += 1
                                    self._log_trade(token_id, "BUY", pair.bid.price, pair.bid.size, market_name)
                                    pair.bid = None

                        if pair.ask and pair.ask.order_id:
                            if not any(o.get("id") == pair.ask.order_id for o in open_orders):
                                if pair.ask.order_id in self._recently_cancelled:
                                    logger.debug(f"Ask {pair.ask.order_id[:12]} was cancelled, not filled")
                                    self._recently_cancelled.discard(pair.ask.order_id)
                                    pair.ask = None
                                else:
                                    self.inventory.record_fill(
                                        token_id, "SELL", pair.ask.price, pair.ask.size
                                    )
                                    self.riskguard.record_fill(token_id, "SELL", pair.ask.price, pair.ask.size)
                                    self._fill_count += 1
                                    self._total_spread_captured += pair.ask.price - pair.fair_value
                                    self._log_trade(token_id, "SELL", pair.ask.price, pair.ask.size, market_name)
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

                # Refresh actual CLOB balance for collateral checks
                if not self._paper_mode:
                    try:
                        bal = await self.clob.get_balance()
                        if bal and bal > 0:
                            self._last_clob_balance = float(bal)
                    except Exception:
                        pass

                # Cleanup stale cancelled IDs (keep last 100)
                if len(self._recently_cancelled) > 100:
                    self._recently_cancelled = set(list(self._recently_cancelled)[-50:])

            except Exception as e:
                logger.error(f"Monitor loop error: {e}", exc_info=True)

            await asyncio.sleep(10)
    
    # ── Loop 4: RiskGuard KPI Evaluation (every 30s) ──

    async def _riskguard_loop(self):
        """Periodically evaluate KPIs and apply auto-fixes."""
        await asyncio.sleep(25)  # Wait for initial data

        while self._running:
            try:
                actions = await self.riskguard.evaluate()

                # Log summary periodically
                halted = len(self.riskguard._halted_markets)
                active_actions = {k: v.value for k, v in actions.items() if v != RiskAction.NONE}
                if active_actions:
                    logger.info(
                        f"🛡️ RiskGuard: {len(active_actions)} actions active, "
                        f"{halted} halted | {active_actions}"
                    )
            except Exception as e:
                logger.error(f"RiskGuard loop error: {e}", exc_info=True)

            await asyncio.sleep(self.config.riskguard_check_interval)

    # ── Loop 5: OU Recalibration (every 5 min) ──
    
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
    
    # ── Loop 6: Exit Rules (SL/TP) ──

    async def _exit_rules_loop(self):
        """Check positions against stop-loss and take-profit levels."""
        await asyncio.sleep(45)  # Wait for initial data

        while self._running:
            try:
                await self._check_exit_rules()
            except Exception as e:
                logger.error(f"Exit rules error: {e}", exc_info=True)
            await asyncio.sleep(self.config.exit_check_interval)

    async def _check_exit_rules(self):
        """Evaluate all positions for SL/TP exits."""
        now = time.time()

        for token_id, pos in list(self.inventory._positions.items()):
            if pos.net_position < 1:
                continue  # No position to exit

            # Skip if in cooldown
            if now - pos.last_exit_time < self.config.exit_cooldown:
                continue

            # Get current market price
            mid = await self._get_mid_price(token_id)
            if mid <= 0:
                continue

            # Update peak price for trailing stop
            if mid > pos.peak_price:
                pos.peak_price = mid

            position_value = pos.net_position * pos.avg_entry
            current_value = pos.net_position * mid
            pnl_pct = (mid - pos.avg_entry) / pos.avg_entry if pos.avg_entry > 0 else 0

            market_name = self._token_to_market_name(token_id)

            # ── STOP-LOSS ──
            if self.config.sl_enabled and pnl_pct <= -self.config.sl_pct:
                if position_value >= self.config.sl_min_position_value:
                    loss_usd = position_value - current_value
                    logger.warning(
                        f"🛑 STOP-LOSS triggered: {market_name} "
                        f"entry=${pos.avg_entry:.4f} now=${mid:.4f} "
                        f"({pnl_pct*100:.1f}%) loss=${loss_usd:.2f}"
                    )
                    await self._execute_exit(token_id, pos.net_position, mid, "STOP_LOSS", market_name)
                    pos.last_exit_time = now
                    continue

            # ── TRAILING STOP (after TP1 hit) ──
            if self.config.tp_enabled and pos.tp1_hit and pos.peak_price > 0:
                trail_trigger = pos.peak_price * (1 - self.config.tp_trailing_pct)
                if mid <= trail_trigger:
                    remaining = pos.net_position
                    logger.info(
                        f"📉 TRAILING STOP triggered: {market_name} "
                        f"peak=${pos.peak_price:.4f} now=${mid:.4f} "
                        f"trail={self.config.tp_trailing_pct*100:.0f}%"
                    )
                    await self._execute_exit(token_id, remaining, mid, "TRAILING_STOP", market_name)
                    pos.last_exit_time = now
                    continue

            # ── TAKE-PROFIT Level 2: 100% gain → sell 75% ──
            if self.config.tp_enabled and not pos.tp2_hit and pnl_pct >= self.config.tp_full_pct:
                sell_size = round(pos.net_position * 0.75, 1)
                if sell_size >= 1:
                    logger.info(
                        f"🎯 TAKE-PROFIT L2 (2x): {market_name} "
                        f"entry=${pos.avg_entry:.4f} now=${mid:.4f} "
                        f"({pnl_pct*100:.1f}%) selling 75%"
                    )
                    await self._execute_exit(token_id, sell_size, mid, "TP_L2", market_name)
                    pos.tp2_hit = True
                    pos.tp1_hit = True
                    pos.last_exit_time = now
                    continue

            # ── TAKE-PROFIT Level 1: 50% gain → sell half ──
            if self.config.tp_enabled and not pos.tp1_hit and pnl_pct >= self.config.tp_pct:
                sell_size = round(pos.net_position * 0.50, 1)
                if sell_size >= 1:
                    logger.info(
                        f"🎯 TAKE-PROFIT L1 (50%%): {market_name} "
                        f"entry=${pos.avg_entry:.4f} now=${mid:.4f} "
                        f"({pnl_pct*100:.1f}%) selling 50%%"
                    )
                    await self._execute_exit(token_id, sell_size, mid, "TP_L1", market_name)
                    pos.tp1_hit = True
                    pos.peak_price = mid  # Start tracking peak for trailing
                    pos.last_exit_time = now
                    continue

    async def _execute_exit(self, token_id: str, size: float, price: float,
                            reason: str, market_name: str):
        """Execute an exit sell order."""
        if self._paper_mode:
            self.inventory.record_fill(token_id, "SELL", price, size)
            self._log_trade(token_id, "SELL", price, size, market_name)
            logger.info(f"📝 PAPER EXIT ({reason}): SELL {size:.1f} @ ${price:.4f} {market_name}")
            return

        try:
            # Cancel any existing ask for this token first
            if token_id in self._active_quotes and self._active_quotes[token_id].ask:
                ask = self._active_quotes[token_id].ask
                if ask.order_id:
                    await self.clob.cancel_order(ask.order_id)
                self._active_quotes[token_id].ask = None

            # Place aggressive sell (slightly below mid for fast fill)
            sell_price = round(max(price * 0.99, 0.001), 3)  # 1% below mid
            order = await self.clob.place_limit_order(
                token_id, "SELL", sell_price, size, "GTC"
            )
            if order:
                order_id = order if isinstance(order, str) else order.get("id", "")
                logger.info(
                    f"🔴 EXIT ORDER ({reason}): SELL {size:.1f} @ ${sell_price:.4f} "
                    f"{market_name} → {order_id[:20]}"
                )
                self.inventory.record_fill(token_id, "SELL", sell_price, size)
                self._log_trade(token_id, "SELL", sell_price, size, market_name)

                # Telegram alert
                if self.telegram:
                    emoji = "🛑" if "STOP" in reason else "🎯"
                    await self.telegram.send(
                        f"{emoji} {reason} EXIT\n"
                        f"Market: {market_name}\n"
                        f"SELL {size:.1f} @ ${sell_price:.4f}\n"
                        f"Reason: {reason}"
                    )
        except Exception as e:
            logger.error(f"Exit order failed ({reason}): {e}")

    async def _get_mid_price(self, token_id: str) -> float:
        """Get current mid price for a token."""
        try:
            ob = await self.clob.get_orderbook(token_id)
            if ob:
                bids = ob.get("bids", [])
                asks = ob.get("asks", [])
                if bids and asks:
                    best_bid = max(float(b.get("price", 0)) for b in bids)
                    best_ask = min(float(a.get("price", 0)) for a in asks if float(a.get("price", 0)) > 0)
                    return (best_bid + best_ask) / 2
        except Exception:
            pass
        return 0.0

    def _token_to_market_name(self, token_id: str) -> str:
        """Map token_id to market name."""
        for m in self._active_markets:
            if m.token_id_yes == token_id:
                return m.question
            if m.token_id_no == token_id:
                return m.question + " (NO)"
        return token_id[:16] + "..."

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
                        (m.question for m in self._active_markets if m.token_id_yes == tid or m.token_id_no == tid),
                        tid[:16] + "..."
                    ),
                    "url": next(
                        (m.url for m in self._active_markets if m.token_id_yes == tid or m.token_id_no == tid),
                        ""
                    ),
                }
                for tid, p in self._active_quotes.items()
            },
            "quote_count": self._quote_count,
            "fill_count": self._fill_count,
            "spread_captured": round(self._total_spread_captured, 4),
            "kill_switch": self.inventory.check_kill_switch(),
            "token_names": {
                **{m.token_id_yes: {"question": m.question, "url": m.url} for m in self._active_markets},
                **{m.token_id_no: {"question": m.question + " (NO)", "url": m.url} for m in self._active_markets},
            },
            "inventory": self.inventory.get_stats(),
            "pnl": {
                "realized": round(self.inventory.total_realized_pnl, 2),
                "unrealized": round(self.inventory.total_unrealized_pnl, 2),
                "daily": round(self.inventory.daily_pnl, 2),
                "total": round(
                    self.inventory.total_realized_pnl + self.inventory.total_unrealized_pnl, 2
                ),
            },
            "regimes": self.hmm.get_all_regimes() if self.hmm else {},
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
            "trade_log": self._trade_log[-50:],
            "riskguard": self.riskguard.get_stats(),
        }
    
    def save_state(self) -> Dict:
        """Save full engine state for persistence across restarts."""
        return {
            "started_at": self._started_at,
            "quote_count": self._quote_count,
            "fill_count": self._fill_count,
            "total_spread_captured": self._total_spread_captured,
            "paper_fills": self._paper_fills[-200:],  # Keep last 200 fills
            "trade_log": self._trade_log[-200:],
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
        self._trade_log = state.get("trade_log", [])
        if "inventory" in state:
            self.inventory.load_state(state["inventory"])
        logger.info(
            f"Restored MM state: {self._fill_count} fills, "
            f"${self._total_spread_captured:.2f} spread captured, "
            f"rpnl=${self.inventory.total_realized_pnl:.2f}"
        )
