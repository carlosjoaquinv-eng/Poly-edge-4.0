"""
PolyEdge v4 — Trading Engines
==============================
Engine 1: MarketMakerEngine (spread capture + OU mean reversion)
Engine 2: ResolutionSniperV2 (event-driven + gap detection)
Engine 3: MetaStrategist (Claude Haiku optimizer every 12h)
"""

from .market_maker import (
    MarketMakerEngine,
    MMConfig,
    OUCalibrator,
    InventoryManager,
    SpreadAnalyzer,
    QuoteGenerator,
    MarketInfo,
    OUParams,
    Quote,
    QuotePair,
    QuoteSide,
    InventoryState,
)

from .resolution_sniper_v2 import (
    ResolutionSniperV2,
    SniperConfig,
    MarketMapper,
    GapDetector,
    SniperExecutor,
    GapSignal,
    SniperTrade,
    TradeUrgency,
)

__all__ = [
    # Engine 1
    "MarketMakerEngine",
    "MMConfig",
    "OUCalibrator",
    "InventoryManager",
    "SpreadAnalyzer",
    "QuoteGenerator",
    "MarketInfo",
    "OUParams",
    "Quote",
    "QuotePair",
    "QuoteSide",
    "InventoryState",
    # Engine 2
    "ResolutionSniperV2",
    "SniperConfig",
    "MarketMapper",
    "GapDetector",
    "SniperExecutor",
    "GapSignal",
    "SniperTrade",
    "TradeUrgency",
]
