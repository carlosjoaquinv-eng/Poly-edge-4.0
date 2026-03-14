"""
Tests for config_v4.py — Unified Configuration
"""
import pytest
from config_v4 import Config


class TestDefaultConstruction:
    def test_default_config(self):
        c = Config()
        assert c is not None

    def test_default_paper_mode(self):
        c = Config()
        assert c.PAPER_MODE is True

    def test_default_bankroll(self):
        c = Config()
        assert c.BANKROLL == 5000.0

    def test_sub_configs_created(self):
        c = Config()
        assert c.mm is not None
        assert c.sniper is not None
        assert c.meta is not None

    def test_default_crypto_symbols(self):
        c = Config()
        assert "BTCUSDT" in c.CRYPTO_SYMBOLS


class TestEnvVarLoading:
    def test_paper_mode_false(self, monkeypatch):
        monkeypatch.setenv("PAPER_MODE", "false")
        c = Config()
        assert c.PAPER_MODE is False

    def test_paper_mode_zero(self, monkeypatch):
        monkeypatch.setenv("PAPER_MODE", "0")
        c = Config()
        assert c.PAPER_MODE is False

    def test_bankroll_from_env(self, monkeypatch):
        monkeypatch.setenv("BANKROLL", "10000")
        c = Config()
        assert c.BANKROLL == 10000.0

    def test_mm_max_markets_override(self, monkeypatch):
        monkeypatch.setenv("MM_MAX_MARKETS", "10")
        c = Config()
        assert c.mm.max_markets == 10

    def test_mm_half_spread_override(self, monkeypatch):
        monkeypatch.setenv("MM_HALF_SPREAD", "0.025")
        c = Config()
        assert c.mm.target_half_spread == 0.025

    def test_sniper_min_gap_override(self, monkeypatch):
        monkeypatch.setenv("SNIPER_MIN_GAP", "5.0")
        c = Config()
        assert c.sniper.min_gap_cents == 5.0

    def test_meta_interval_override(self, monkeypatch):
        monkeypatch.setenv("META_INTERVAL_HOURS", "6")
        c = Config()
        assert c.meta.run_interval_hours == 6.0

    def test_dashboard_port_override(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_PORT", "9090")
        c = Config()
        assert c.DASHBOARD_PORT == 9090

    def test_credentials_from_env(self, monkeypatch):
        monkeypatch.setenv("PRIVATE_KEY", "0xdeadbeef")
        c = Config()
        assert c.PRIVATE_KEY == "0xdeadbeef"


class TestValidation:
    def test_paper_mode_no_critical(self):
        c = Config()
        warnings = c.validate()
        critical = [w for w in warnings if "CRITICAL" in w]
        assert len(critical) == 0

    def test_live_no_private_key(self, monkeypatch):
        monkeypatch.setenv("PAPER_MODE", "false")
        monkeypatch.delenv("PRIVATE_KEY", raising=False)
        c = Config()
        warnings = c.validate()
        assert any("PRIVATE_KEY" in w for w in warnings)

    def test_low_bankroll_warning(self, monkeypatch):
        monkeypatch.setenv("BANKROLL", "50")
        c = Config()
        warnings = c.validate()
        assert any("Low bankroll" in w for w in warnings)

    def test_no_telegram_warning(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        c = Config()
        warnings = c.validate()
        assert any("TELEGRAM" in w for w in warnings)


class TestSummary:
    def test_summary_returns_string(self):
        c = Config()
        s = c.summary()
        assert isinstance(s, str)
        assert "PolyEdge" in s
        assert "PAPER" in s

    def test_summary_contains_engine_info(self):
        c = Config()
        s = c.summary()
        assert "Market Maker" in s
        assert "Sniper" in s
        assert "Meta" in s
