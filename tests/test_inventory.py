"""
Tests for InventoryManager — Position tracking, limits, skew
Source: engines/market_maker.py InventoryManager class
"""
import pytest
from engines.market_maker import InventoryManager, MMConfig


class TestInitialState:
    def test_empty_inventory(self, inventory_manager):
        assert inventory_manager.total_exposure == 0.0
        assert inventory_manager.total_realized_pnl == 0.0

    def test_get_position_creates_default(self, inventory_manager):
        pos = inventory_manager.get_position("new_token")
        assert pos.net_position == 0.0
        assert pos.avg_entry == 0.0
        assert pos.n_trades == 0


class TestRecordFillBuy:
    def test_buy_increases_position(self, inventory_manager):
        inventory_manager.record_fill("tok", "BUY", price=0.50, size=100.0)
        pos = inventory_manager.get_position("tok")
        assert pos.net_position == 100.0
        assert pos.avg_entry == 0.50

    def test_buy_updates_avg_entry(self, inventory_manager):
        inventory_manager.record_fill("tok", "BUY", price=0.40, size=100.0)
        inventory_manager.record_fill("tok", "BUY", price=0.60, size=100.0)
        pos = inventory_manager.get_position("tok")
        assert pos.net_position == 200.0
        assert abs(pos.avg_entry - 0.50) < 0.001

    def test_buy_covering_short(self, inventory_manager):
        # Open short
        inventory_manager.record_fill("tok", "SELL", price=0.60, size=100.0)
        # Cover short (buy back lower = profit)
        inventory_manager.record_fill("tok", "BUY", price=0.50, size=100.0)
        pos = inventory_manager.get_position("tok")
        assert abs(pos.net_position) < 0.01
        assert pos.realized_pnl == pytest.approx(10.0, abs=0.01)  # (0.60 - 0.50) * 100

    def test_buy_trade_count(self, inventory_manager):
        inventory_manager.record_fill("tok", "BUY", price=0.50, size=10.0)
        inventory_manager.record_fill("tok", "BUY", price=0.55, size=10.0)
        pos = inventory_manager.get_position("tok")
        assert pos.n_trades == 2


class TestRecordFillSell:
    def test_sell_from_long_realizes_pnl(self, inventory_manager):
        inventory_manager.record_fill("tok", "BUY", price=0.40, size=100.0)
        inventory_manager.record_fill("tok", "SELL", price=0.60, size=100.0)
        pos = inventory_manager.get_position("tok")
        assert abs(pos.net_position) < 0.01
        assert pos.realized_pnl == pytest.approx(20.0, abs=0.01)  # (0.60 - 0.40) * 100

    def test_sell_partial_from_long(self, inventory_manager):
        inventory_manager.record_fill("tok", "BUY", price=0.50, size=100.0)
        inventory_manager.record_fill("tok", "SELL", price=0.60, size=50.0)
        pos = inventory_manager.get_position("tok")
        assert pos.net_position == 50.0
        assert pos.realized_pnl == pytest.approx(5.0, abs=0.01)  # (0.60 - 0.50) * 50

    def test_sell_opens_short(self, inventory_manager):
        inventory_manager.record_fill("tok", "SELL", price=0.60, size=100.0)
        pos = inventory_manager.get_position("tok")
        assert pos.net_position == -100.0
        assert pos.avg_entry == 0.60

    def test_sell_losing_trade(self, inventory_manager):
        inventory_manager.record_fill("tok", "BUY", price=0.60, size=100.0)
        inventory_manager.record_fill("tok", "SELL", price=0.50, size=100.0)
        pos = inventory_manager.get_position("tok")
        assert pos.realized_pnl == pytest.approx(-10.0, abs=0.01)  # (0.50 - 0.60) * 100


class TestCanBuy:
    def test_can_buy_within_limits(self, inventory_manager):
        assert inventory_manager.can_buy("tok", size=10.0, price=0.50) is True

    def test_can_buy_exceeds_notional(self):
        cfg = MMConfig(max_position_per_market=50.0)
        im = InventoryManager(cfg)
        # 200 units at $0.50 = $100 > $50 limit
        assert im.can_buy("tok", size=200.0, price=0.50) is False

    def test_can_buy_exceeds_total_exposure(self):
        cfg = MMConfig(max_total_inventory=100.0)
        im = InventoryManager(cfg)
        im.record_fill("tok1", "BUY", price=0.50, size=150.0)  # $75 exposure
        # Adding $50 more would exceed $100 total
        assert im.can_buy("tok2", size=100.0, price=0.50) is False

    def test_can_buy_exceeds_units(self):
        cfg = MMConfig(max_units_per_market=100.0)
        im = InventoryManager(cfg)
        im.record_fill("tok", "BUY", price=0.10, size=90.0)
        # 90 + 20 = 110 > 100 unit cap
        assert im.can_buy("tok", size=20.0, price=0.10) is False


class TestCanSell:
    def test_can_sell_within_limits(self, inventory_manager):
        assert inventory_manager.can_sell("tok", size=10.0, price=0.50) is True

    def test_can_sell_exceeds_units(self):
        cfg = MMConfig(max_units_per_market=100.0)
        im = InventoryManager(cfg)
        im.record_fill("tok", "SELL", price=0.50, size=90.0)
        # -90 - 20 = -110, abs = 110 > 100
        assert im.can_sell("tok", size=20.0, price=0.50) is False


class TestComputeSkew:
    def test_zero_inventory_zero_skew(self, inventory_manager):
        skew = inventory_manager.compute_skew("tok")
        assert skew == 0.0

    def test_long_position_negative_skew(self, inventory_manager):
        inventory_manager.record_fill("tok", "BUY", price=0.50, size=50.0)
        skew = inventory_manager.compute_skew("tok")
        # Long inventory → negative skew (attract sells)
        assert skew < 0.0

    def test_short_position_positive_skew(self, inventory_manager):
        inventory_manager.record_fill("tok", "SELL", price=0.50, size=50.0)
        skew = inventory_manager.compute_skew("tok")
        # Short inventory → positive skew (attract buys)
        assert skew > 0.0

    def test_nonlinear_skew_grows_faster(self):
        cfg = MMConfig(inventory_skew_nonlinear=True)
        im = InventoryManager(cfg)
        # Small position
        im.record_fill("a", "BUY", price=0.50, size=20.0)
        skew_small = abs(im.compute_skew("a"))
        # Larger position
        im.record_fill("b", "BUY", price=0.50, size=100.0)
        skew_large = abs(im.compute_skew("b"))
        # Skew should grow faster than linearly
        ratio_size = 100.0 / 20.0  # 5x
        ratio_skew = skew_large / skew_small if skew_small > 0 else 999
        assert ratio_skew > ratio_size  # nonlinear = grows faster

    def test_skew_boost_doubles(self, inventory_manager):
        inventory_manager.record_fill("tok", "BUY", price=0.50, size=50.0)
        skew_1x = abs(inventory_manager.compute_skew("tok", skew_boost=1.0))
        skew_2x = abs(inventory_manager.compute_skew("tok", skew_boost=2.0))
        assert abs(skew_2x / skew_1x - 2.0) < 0.1  # ~2x


class TestKillSwitch:
    def test_not_triggered_initially(self, inventory_manager):
        assert inventory_manager.check_kill_switch() is False

    def test_triggered_on_big_loss(self):
        cfg = MMConfig(max_loss_per_day=50.0)
        im = InventoryManager(cfg)
        # Buy high, sell low for $60 loss
        im.record_fill("tok", "BUY", price=0.60, size=200.0)
        im.record_fill("tok", "SELL", price=0.30, size=200.0)
        assert im.check_kill_switch() is True

    def test_reset_daily_clears_pnl(self, inventory_manager):
        inventory_manager._daily_pnl = -100.0
        inventory_manager.reset_daily()
        assert inventory_manager._daily_pnl == 0.0
        assert inventory_manager.check_kill_switch() is False


class TestStats:
    def test_get_stats_format(self, inventory_manager):
        inventory_manager.record_fill("tok", "BUY", price=0.50, size=10.0)
        stats = inventory_manager.get_stats()
        assert "active_markets" in stats
        assert "total_exposure" in stats
        assert "positions" in stats
        assert "tok" in stats["positions"]
