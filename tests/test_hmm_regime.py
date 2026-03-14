"""
Tests for engine/core/hmm_regime.py — Volatility-Based Regime Detection
"""
import math
import pytest
from engine.core.hmm_regime import RegimeDetector, RegimeConfig, Regime


class TestConstruction:
    def test_default_construction(self):
        r = RegimeDetector()
        assert r.config is not None
        assert r.config.min_observations == 30

    def test_custom_config(self):
        cfg = RegimeConfig(min_observations=50, max_history=200)
        r = RegimeDetector(cfg)
        assert r.config.min_observations == 50
        assert r.config.max_history == 200

    def test_regime_config_defaults(self):
        cfg = RegimeConfig()
        assert cfg.spread_multipliers["NORMAL"] == 1.0
        assert cfg.position_multipliers["NORMAL"] == 1.0
        assert cfg.spread_multipliers["HIGH_VOL"] == 1.5
        assert cfg.position_multipliers["HIGH_VOL"] == 0.7


class TestInsufficientData:
    def test_spread_multiplier_unknown_token(self, regime_detector):
        assert regime_detector.get_spread_multiplier("unknown_token") == 1.0

    def test_position_multiplier_unknown_token(self, regime_detector):
        assert regime_detector.get_position_multiplier("unknown_token") == 1.0

    def test_few_observations_returns_normal(self, regime_detector):
        for i in range(10):
            regime_detector.record_price("tok", 0.5 + i * 0.001)
        assert regime_detector.get_spread_multiplier("tok") == 1.0
        assert regime_detector.get_position_multiplier("tok") == 1.0

    def test_record_price_creates_state(self, regime_detector):
        regime_detector.record_price("new_tok", 0.5)
        regimes = regime_detector.get_all_regimes()
        assert "new_tok" in regimes
        assert regimes["new_tok"]["n_observations"] == 1


class TestRegimeClassification:
    def _feed_stable_prices(self, detector, token, n=60):
        """Feed very stable prices (low vol)."""
        # First build a history with moderate vol so median is established
        for i in range(n):
            noise = 0.005 * math.sin(i * 0.5)
            detector.record_price(token, 0.5 + noise, timestamp=float(i))
        # Then feed very stable prices so current vol << median
        for i in range(n, n + 30):
            detector.record_price(token, 0.5 + 0.0001 * (i % 3), timestamp=float(i))

    def _feed_volatile_prices(self, detector, token, n=60):
        """Feed prices with increasing volatility."""
        # Moderate baseline
        for i in range(n):
            noise = 0.002 * math.sin(i * 0.3)
            detector.record_price(token, 0.5 + noise, timestamp=float(i))
        # Then very volatile
        for i in range(n, n + 30):
            big_noise = 0.05 * math.sin(i * 2.0)
            detector.record_price(token, 0.5 + big_noise, timestamp=float(i))

    def _feed_trending_prices(self, detector, token, n=80):
        """Feed consistently rising prices."""
        for i in range(n):
            price = 0.3 + (i / n) * 0.4  # 0.3 → 0.7 linear rise
            detector.record_price(token, price, timestamp=float(i))

    def test_normal_regime_moderate_prices(self):
        """Consistent moderate volatility → NORMAL."""
        r = RegimeDetector(RegimeConfig(min_observations=20, lookback_window=10, reclassify_interval=0))
        for i in range(60):
            noise = 0.01 * math.sin(i * 0.5)
            r.record_price("tok", 0.5 + noise, timestamp=float(i))
        regimes = r.get_all_regimes()
        assert regimes["tok"]["regime"] in ("NORMAL", "LOW_VOL")

    def test_high_vol_regime(self):
        """Sudden spike in volatility → HIGH_VOL."""
        r = RegimeDetector(RegimeConfig(min_observations=20, lookback_window=10, reclassify_interval=0))
        self._feed_volatile_prices(r, "tok")
        regime = r.get_all_regimes()["tok"]["regime"]
        assert regime in ("HIGH_VOL", "TRENDING")

    def test_trending_regime(self):
        """Consistently rising prices → TRENDING."""
        r = RegimeDetector(RegimeConfig(min_observations=20, lookback_window=10, reclassify_interval=0))
        self._feed_trending_prices(r, "tok")
        regime = r.get_all_regimes()["tok"]["regime"]
        assert regime == "TRENDING"


class TestMultipliers:
    def test_low_vol_spread_multiplier(self):
        cfg = RegimeConfig()
        assert cfg.spread_multipliers["LOW_VOL"] == 0.8

    def test_high_vol_spread_multiplier(self):
        cfg = RegimeConfig()
        assert cfg.spread_multipliers["HIGH_VOL"] == 1.5

    def test_trending_position_multiplier(self):
        cfg = RegimeConfig()
        assert cfg.position_multipliers["TRENDING"] == 0.5

    def test_low_vol_position_multiplier(self):
        cfg = RegimeConfig()
        assert cfg.position_multipliers["LOW_VOL"] == 1.2


class TestHistoryManagement:
    def test_max_history_trimming(self):
        r = RegimeDetector(RegimeConfig(max_history=50))
        for i in range(100):
            r.record_price("tok", 0.5, timestamp=float(i))
        state = r._states["tok"]
        assert len(state.prices) <= 50

    def test_get_all_regimes_format(self, regime_detector):
        regime_detector.record_price("a", 0.5)
        regime_detector.record_price("b", 0.6)
        regimes = regime_detector.get_all_regimes()
        assert "a" in regimes
        assert "b" in regimes
        for key in ("regime", "volatility", "median_volatility", "trend_strength", "n_observations"):
            assert key in regimes["a"]

    def test_rejects_negative_price(self, regime_detector):
        regime_detector.record_price("tok", -1.0)
        assert "tok" not in regime_detector._states
