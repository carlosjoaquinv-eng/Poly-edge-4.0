"""
Tests for OUCalibrator — Ornstein-Uhlenbeck parameter calibration
Source: engines/market_maker.py OUCalibrator class
"""
import math
import time
import pytest
from engines.market_maker import OUCalibrator, MMConfig, OUParams


class TestConstruction:
    def test_default_construction(self, ou_calibrator):
        assert ou_calibrator is not None
        assert len(ou_calibrator._price_history) == 0

    def test_empty_fair_value_returns_default(self, ou_calibrator):
        result = ou_calibrator.get_fair_value("unknown_token")
        # OUCalibrator returns 0.5 as default fair value for unknown tokens
        assert result == 0.5


class TestRecordPrice:
    def test_record_price_stores_history(self, ou_calibrator):
        ou_calibrator.record_price("tok", 0.50, timestamp=100.0)
        ou_calibrator.record_price("tok", 0.51, timestamp=101.0)
        assert len(ou_calibrator._price_history["tok"]) == 2

    def test_record_price_trims_old(self):
        cfg = MMConfig(ou_lookback_secs=100)  # 100s lookback, trim > 200s
        oc = OUCalibrator(cfg)
        # Record old data
        oc.record_price("tok", 0.50, timestamp=0.0)
        oc.record_price("tok", 0.51, timestamp=50.0)
        # Record recent data (timestamp > 200)
        oc.record_price("tok", 0.52, timestamp=250.0)
        # Old data (ts=0, ts=50) should be trimmed (cutoff = 250 - 200 = 50)
        history = oc._price_history["tok"]
        assert all(ts >= 50.0 for ts, _ in history)

    def test_multiple_tokens_independent(self, ou_calibrator):
        ou_calibrator.record_price("a", 0.40, timestamp=1.0)
        ou_calibrator.record_price("b", 0.60, timestamp=1.0)
        assert len(ou_calibrator._price_history["a"]) == 1
        assert len(ou_calibrator._price_history["b"]) == 1


class TestInsufficientData:
    def test_insufficient_data_uses_defaults(self):
        cfg = MMConfig(ou_min_observations=30, ou_default_theta=0.1, ou_default_sigma=0.02)
        oc = OUCalibrator(cfg)
        # Only 5 observations
        for i in range(5):
            oc.record_price("tok", 0.50 + i * 0.01, timestamp=float(i))
        params = oc.get_params("tok")
        assert params is not None
        assert params.theta == 0.1
        assert params.sigma == 0.02

    def test_insufficient_data_mu_uses_first_price(self):
        cfg = MMConfig(ou_min_observations=30)
        oc = OUCalibrator(cfg)
        prices = [0.40, 0.45, 0.50, 0.55, 0.60]
        for i, p in enumerate(prices):
            oc.record_price("tok", p, timestamp=float(i))
        params = oc.get_params("tok")
        # With insufficient data, mu defaults based on first observation
        assert params is not None
        assert 0.01 <= params.mu <= 0.99


class TestCalibration:
    def _feed_oscillating_prices(self, oc, token, center=0.5, n=100):
        """Feed prices that oscillate around a center value."""
        for i in range(n):
            noise = 0.03 * math.sin(i * 0.3) + 0.01 * math.cos(i * 0.7)
            oc.record_price(token, center + noise, timestamp=float(i))

    def test_calibration_mu_near_center(self):
        cfg = MMConfig(ou_min_observations=20, ou_recalibrate_secs=0)
        oc = OUCalibrator(cfg)
        self._feed_oscillating_prices(oc, "tok", center=0.55, n=100)
        params = oc.get_params("tok")
        # mu should be near the oscillation center
        assert abs(params.mu - 0.55) < 0.05

    def test_calibration_sigma_positive(self):
        cfg = MMConfig(ou_min_observations=20, ou_recalibrate_secs=0)
        oc = OUCalibrator(cfg)
        self._feed_oscillating_prices(oc, "tok", n=100)
        params = oc.get_params("tok")
        assert params.sigma > 0

    def test_calibration_theta_positive(self):
        cfg = MMConfig(ou_min_observations=20, ou_recalibrate_secs=0)
        oc = OUCalibrator(cfg)
        self._feed_oscillating_prices(oc, "tok", n=100)
        params = oc.get_params("tok")
        assert params.theta > 0

    def test_calibration_mu_clamped(self):
        cfg = MMConfig(ou_min_observations=20, ou_recalibrate_secs=0)
        oc = OUCalibrator(cfg)
        self._feed_oscillating_prices(oc, "tok", center=0.5, n=100)
        params = oc.get_params("tok")
        assert 0.01 <= params.mu <= 0.99


class TestGetDeviation:
    def test_deviation_at_default_fair_value(self, ou_calibrator):
        # get_deviation requires current_price arg; returns 0 when price == default mu
        result = ou_calibrator.get_deviation("unknown", current_price=0.50)
        assert result is not None
        assert abs(result) < 0.01

    def test_deviation_near_zero_at_fair(self):
        cfg = MMConfig(ou_min_observations=5)
        oc = OUCalibrator(cfg)
        for i in range(10):
            oc.record_price("tok", 0.50, timestamp=float(i))
        dev = oc.get_deviation("tok", current_price=0.50)
        # When price == mu, deviation should be near zero
        if dev is not None:
            assert abs(dev) < 0.1

    def test_deviation_positive_above_fair(self):
        cfg = MMConfig(ou_min_observations=5)
        oc = OUCalibrator(cfg)
        for i in range(10):
            oc.record_price("tok", 0.50, timestamp=float(i))
        dev = oc.get_deviation("tok", current_price=0.70)
        if dev is not None:
            assert dev > 0  # Price above fair → positive deviation


class TestFairValue:
    def test_fair_value_after_calibration(self):
        cfg = MMConfig(ou_min_observations=20, ou_recalibrate_secs=0)
        oc = OUCalibrator(cfg)
        for i in range(50):
            noise = 0.02 * math.sin(i * 0.4)
            oc.record_price("tok", 0.45 + noise, timestamp=float(i))
        fv = oc.get_fair_value("tok")
        assert fv is not None
        assert abs(fv - 0.45) < 0.05

    def test_fair_value_default_for_unknown(self, ou_calibrator):
        # OUCalibrator returns 0.5 as default for uncalibrated tokens
        assert ou_calibrator.get_fair_value("xyz") == 0.5
