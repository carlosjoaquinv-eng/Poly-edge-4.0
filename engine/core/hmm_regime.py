"""
PolyEdge v4 — Volatility-Based Regime Detection
=================================================
Classifies market regimes based on rolling price volatility.
Used by MarketMakerEngine to adapt spread width and position sizing.

Detection via rolling standard deviation of log returns compared
against historical median volatility. No external ML dependencies.

Regimes:
  LOW_VOL   — tight spreads, larger positions (calm market)
  NORMAL    — default multipliers (baseline)
  HIGH_VOL  — wide spreads, smaller positions (volatile)
  TRENDING  — wider spreads, minimal positions (directional move)
"""

import math
import time
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


logger = logging.getLogger("polyedge.hmm_regime")


# ── Regime Enum ──

class Regime(Enum):
    LOW_VOL = "LOW_VOL"
    NORMAL = "NORMAL"
    HIGH_VOL = "HIGH_VOL"
    TRENDING = "TRENDING"


# ── Configuration ──

@dataclass
class RegimeConfig:
    """Tunable parameters for regime detection."""
    min_observations: int = 30          # Min data points before classifying
    max_history: int = 500              # Max observations per token
    lookback_window: int = 20           # Rolling window for current volatility
    vol_low_threshold: float = 0.5      # current_vol < 0.5× median → LOW_VOL
    vol_high_threshold: float = 1.8     # current_vol > 1.8× median → HIGH_VOL
    trend_threshold: float = 0.6        # |mean_return| > 0.6× current_vol → TRENDING
    reclassify_interval: float = 30.0   # Seconds between reclassifications

    # Spread multipliers per regime
    spread_multipliers: Dict[str, float] = field(default_factory=lambda: {
        "LOW_VOL": 0.8,
        "NORMAL": 1.0,
        "HIGH_VOL": 1.5,
        "TRENDING": 1.3,
    })

    # Position size multipliers per regime
    position_multipliers: Dict[str, float] = field(default_factory=lambda: {
        "LOW_VOL": 1.2,
        "NORMAL": 1.0,
        "HIGH_VOL": 0.7,
        "TRENDING": 0.5,
    })


# ── Internal State Per Token ──

@dataclass
class _TokenState:
    """Internal regime state for a single token/market."""
    prices: List[Tuple[float, float]] = field(default_factory=list)  # (timestamp, price)
    regime: Regime = Regime.NORMAL
    volatility: float = 0.0
    median_volatility: float = 0.0
    trend_strength: float = 0.0
    n_observations: int = 0
    last_classified_at: float = 0.0


# ── Regime Detector ──

class RegimeDetector:
    """
    Volatility-based regime detector for prediction markets.

    Tracks per-token price history and classifies the current market regime
    using rolling standard deviation of log returns vs historical median.

    Returns safe defaults (multiplier = 1.0) when insufficient data.
    """

    def __init__(self, config: RegimeConfig = None):
        self.config = config or RegimeConfig()
        self._states: Dict[str, _TokenState] = {}

    def _get_state(self, token_id: str) -> _TokenState:
        """Get or create state for a token."""
        if token_id not in self._states:
            self._states[token_id] = _TokenState()
        return self._states[token_id]

    # ── Public API ──

    def record_price(self, token_id: str, price: float, timestamp: float = None) -> None:
        """Record a price observation. Reclassifies if enough data."""
        if price <= 0:
            return

        ts = timestamp or time.time()
        state = self._get_state(token_id)
        state.prices.append((ts, price))
        state.n_observations += 1

        # Trim to max_history
        if len(state.prices) > self.config.max_history:
            excess = len(state.prices) - self.config.max_history
            state.prices = state.prices[excess:]

        # Reclassify if enough data and interval elapsed
        if (state.n_observations >= self.config.min_observations
                and ts - state.last_classified_at >= self.config.reclassify_interval):
            self._classify(state, ts)

    def get_spread_multiplier(self, token_id: str) -> float:
        """Return spread multiplier for current regime. 1.0 if unknown."""
        if token_id not in self._states:
            return 1.0
        state = self._states[token_id]
        if state.n_observations < self.config.min_observations:
            return 1.0
        return self.config.spread_multipliers.get(state.regime.value, 1.0)

    def get_position_multiplier(self, token_id: str) -> float:
        """Return position size multiplier for current regime. 1.0 if unknown."""
        if token_id not in self._states:
            return 1.0
        state = self._states[token_id]
        if state.n_observations < self.config.min_observations:
            return 1.0
        return self.config.position_multipliers.get(state.regime.value, 1.0)

    def get_all_regimes(self) -> dict:
        """Return regime info for all tracked tokens (dashboard/API use)."""
        result = {}
        for token_id, state in self._states.items():
            result[token_id] = {
                "regime": state.regime.value,
                "volatility": round(state.volatility, 6),
                "median_volatility": round(state.median_volatility, 6),
                "trend_strength": round(state.trend_strength, 6),
                "n_observations": state.n_observations,
            }
        return result

    # ── Classification Logic ──

    def _classify(self, state: _TokenState, now: float) -> None:
        """Classify regime from price history using rolling volatility."""
        prices = [p for _, p in state.prices]
        if len(prices) < self.config.min_observations:
            return

        # Compute log returns
        returns = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0 and prices[i] > 0:
                returns.append(math.log(prices[i] / prices[i - 1]))

        if len(returns) < self.config.lookback_window:
            return

        # Current volatility: std dev of last `lookback_window` returns
        window = self.config.lookback_window
        recent_returns = returns[-window:]
        current_vol = self._std_dev(recent_returns)

        # Historical median volatility: std devs of non-overlapping chunks
        chunk_vols = []
        for start in range(0, len(returns) - window + 1, window):
            chunk = returns[start:start + window]
            if len(chunk) == window:
                chunk_vols.append(self._std_dev(chunk))

        if not chunk_vols:
            return

        median_vol = self._median(chunk_vols)

        # Trend detection: mean of recent returns
        mean_return = sum(recent_returns) / len(recent_returns)
        trend_strength = abs(mean_return) / current_vol if current_vol > 1e-10 else 0.0

        # Save stats
        state.volatility = current_vol
        state.median_volatility = median_vol
        state.trend_strength = trend_strength
        state.last_classified_at = now

        # Classify
        if median_vol < 1e-10:
            state.regime = Regime.NORMAL
            return

        vol_ratio = current_vol / median_vol

        if trend_strength > self.config.trend_threshold:
            state.regime = Regime.TRENDING
        elif vol_ratio < self.config.vol_low_threshold:
            state.regime = Regime.LOW_VOL
        elif vol_ratio > self.config.vol_high_threshold:
            state.regime = Regime.HIGH_VOL
        else:
            state.regime = Regime.NORMAL

        logger.debug(
            "Regime %s: vol=%.6f median=%.6f ratio=%.2f trend=%.3f → %s",
            token_id if 'token_id' in dir() else '?',
            current_vol, median_vol, vol_ratio, trend_strength,
            state.regime.value,
        )

    # ── Math Helpers ──

    @staticmethod
    def _std_dev(values: List[float]) -> float:
        """Compute population standard deviation."""
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        return math.sqrt(variance)

    @staticmethod
    def _median(values: List[float]) -> float:
        """Compute median of a list."""
        if not values:
            return 0.0
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        if n % 2 == 1:
            return sorted_vals[n // 2]
        return (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2.0
