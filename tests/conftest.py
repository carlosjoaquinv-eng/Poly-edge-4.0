"""
PolyEdge v4 — Shared Test Fixtures
"""
import sys
import os
import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.market_maker import MMConfig, InventoryState, OUCalibrator, InventoryManager
from engine.core.hmm_regime import RegimeDetector, RegimeConfig


@pytest.fixture
def mm_config():
    """Default MMConfig for tests."""
    return MMConfig()


@pytest.fixture
def regime_config():
    """Default RegimeConfig for tests."""
    return RegimeConfig()


@pytest.fixture
def regime_detector():
    """Fresh RegimeDetector with default config."""
    return RegimeDetector()


@pytest.fixture
def ou_calibrator(mm_config):
    """Fresh OUCalibrator."""
    return OUCalibrator(mm_config)


@pytest.fixture
def inventory_manager(mm_config):
    """Fresh InventoryManager."""
    return InventoryManager(mm_config)
