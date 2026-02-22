"""
PolyEdge v4 — Unified Configuration
=====================================
Central configuration for all engines + shared infrastructure.
Loads from environment variables with sensible defaults.
"""

import os
import logging

from engines.market_maker import MMConfig
from engines.resolution_sniper_v2 import SniperConfig
from engines.meta_strategist import MetaConfig


class Config:
    """
    Master configuration for PolyEdge v4.
    
    Hierarchy:
      1. Environment variables (highest priority)
      2. Config file (config_v4.json) — TODO
      3. Defaults (coded here)
    """
    
    # ── Mode ──
    PAPER_MODE: bool = True
    
    # ── Identity ──
    VERSION: str = "4.0.0"
    INSTANCE_NAME: str = "polyedge-v4"
    
    # ── Bankroll ──
    BANKROLL: float = 5000.0
    
    # ── Polymarket CLOB ──
    CLOB_API_URL: str = "https://clob.polymarket.com"
    GAMMA_API_URL: str = "https://gamma-api.polymarket.com"
    CLOB_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    CHAIN_ID: int = 137  # Polygon mainnet
    
    # ── Credentials (from env) ──
    PRIVATE_KEY: str = ""
    POLYMARKET_API_KEY: str = ""
    POLYMARKET_API_SECRET: str = ""
    POLYMARKET_API_PASSPHRASE: str = ""
    ANTHROPIC_API_KEY: str = ""
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    
    # ── Feeds ──
    FOOTBALL_API_KEY: str = ""
    FOOTBALL_POLL_LIVE: int = 15        # seconds
    FOOTBALL_POLL_IDLE: int = 120       # seconds
    CRYPTO_POLL_INTERVAL: int = 5       # seconds
    CRYPTO_SYMBOLS: list = None
    
    # ── Dashboard ──
    DASHBOARD_PORT: int = 8081          # v4 on 8081, v3.1 stays on 8080
    DASHBOARD_HOST: str = "0.0.0.0"
    
    # ── Logging ──
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "logs/polyedge_v4.log"
    
    # ── Data Store ──
    DATA_DIR: str = "data_v4"
    
    # ── Engine Configs ──
    mm: MMConfig = None
    sniper: SniperConfig = None
    meta: MetaConfig = None
    
    def __init__(self):
        self.CRYPTO_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        self.mm = MMConfig()
        self.sniper = SniperConfig()
        self.meta = MetaConfig()
        self._load_env()
    
    def _load_env(self):
        """Load configuration from environment variables."""
        # Mode
        self.PAPER_MODE = os.environ.get("PAPER_MODE", "true").lower() in ("true", "1", "yes")
        
        # Bankroll
        self.BANKROLL = float(os.environ.get("BANKROLL", str(self.BANKROLL)))
        
        # Credentials
        self.PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
        self.POLYMARKET_API_KEY = os.environ.get("POLYMARKET_API_KEY", "")
        self.POLYMARKET_API_SECRET = os.environ.get("POLYMARKET_API_SECRET", "")
        self.POLYMARKET_API_PASSPHRASE = os.environ.get("POLYMARKET_API_PASSPHRASE", "")
        self.ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
        self.TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
        
        # Feeds
        self.FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY", "")
        
        # Dashboard
        self.DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", str(self.DASHBOARD_PORT)))
        
        # Logging
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", self.LOG_LEVEL)
        
        # MM overrides
        if os.environ.get("MM_MAX_MARKETS"):
            self.mm.max_markets = int(os.environ["MM_MAX_MARKETS"])
        if os.environ.get("MM_MAX_POSITION"):
            self.mm.max_position_per_market = float(os.environ["MM_MAX_POSITION"])
        if os.environ.get("MM_MAX_INVENTORY"):
            self.mm.max_total_inventory = float(os.environ["MM_MAX_INVENTORY"])
        if os.environ.get("MM_HALF_SPREAD"):
            self.mm.target_half_spread = float(os.environ["MM_HALF_SPREAD"])
        if os.environ.get("MM_KELLY"):
            self.mm.kelly_fraction = float(os.environ["MM_KELLY"])
        
        # Sniper overrides
        if os.environ.get("SNIPER_MIN_GAP"):
            self.sniper.min_gap_cents = float(os.environ["SNIPER_MIN_GAP"])
        if os.environ.get("SNIPER_MIN_EDGE"):
            self.sniper.min_edge_pct = float(os.environ["SNIPER_MIN_EDGE"])
        if os.environ.get("SNIPER_MAX_TRADE"):
            self.sniper.max_trade_size = float(os.environ["SNIPER_MAX_TRADE"])
        if os.environ.get("SNIPER_MAX_EXPOSURE"):
            self.sniper.max_total_exposure = float(os.environ["SNIPER_MAX_EXPOSURE"])
        
        # Meta overrides
        if os.environ.get("META_INTERVAL_HOURS"):
            self.meta.run_interval_hours = float(os.environ["META_INTERVAL_HOURS"])
        if os.environ.get("META_AUTO_APPLY"):
            self.meta.auto_apply_adjustments = os.environ["META_AUTO_APPLY"].lower() in ("true", "1")
    
    def validate(self) -> list:
        """Validate config and return list of warnings."""
        warnings = []
        
        if not self.PAPER_MODE:
            if not self.PRIVATE_KEY:
                warnings.append("CRITICAL: PRIVATE_KEY not set — cannot trade live")
            if not self.POLYMARKET_API_KEY:
                warnings.append("WARNING: POLYMARKET_API_KEY not set")
        
        if not self.TELEGRAM_BOT_TOKEN:
            warnings.append("WARNING: TELEGRAM_BOT_TOKEN not set — no alerts")
        
        if not self.ANTHROPIC_API_KEY:
            warnings.append("WARNING: ANTHROPIC_API_KEY not set — meta-strategist will use mock mode")
        
        if not self.FOOTBALL_API_KEY:
            warnings.append("WARNING: FOOTBALL_API_KEY not set — football feed disabled")
        
        if self.BANKROLL < 100:
            warnings.append(f"WARNING: Low bankroll ${self.BANKROLL}")
        
        return warnings
    
    def summary(self) -> str:
        """Human-readable config summary."""
        mode = "PAPER" if self.PAPER_MODE else "🔴 LIVE"
        return (
            f"PolyEdge v{self.VERSION} — {mode}\n"
            f"Bankroll: ${self.BANKROLL:,.0f}\n"
            f"─── Market Maker ───\n"
            f"  Max markets: {self.mm.max_markets}\n"
            f"  Position limit: ${self.mm.max_position_per_market}/market, ${self.mm.max_total_inventory} total\n"
            f"  Half-spread: {self.mm.target_half_spread*100:.1f}¢\n"
            f"  Kelly: {self.mm.kelly_fraction}\n"
            f"─── Sniper ───\n"
            f"  Min gap: {self.sniper.min_gap_cents}¢ | Min edge: {self.sniper.min_edge_pct}%\n"
            f"  Max trade: ${self.sniper.max_trade_size} | Max exposure: ${self.sniper.max_total_exposure}\n"
            f"  Kelly: {self.sniper.kelly_fraction}\n"
            f"─── Meta Strategist ───\n"
            f"  Model: {self.meta.model}\n"
            f"  Interval: {self.meta.run_interval_hours}h\n"
            f"  Auto-apply: {'ON' if self.meta.auto_apply_adjustments else 'OFF'}\n"
            f"─── Feeds ───\n"
            f"  Football: {'✅' if self.FOOTBALL_API_KEY else '❌'}\n"
            f"  Crypto: {', '.join(self.CRYPTO_SYMBOLS)}\n"
            f"─── Infra ───\n"
            f"  Dashboard: :{self.DASHBOARD_PORT}\n"
            f"  Telegram: {'✅' if self.TELEGRAM_BOT_TOKEN else '❌'}\n"
            f"  Anthropic: {'✅' if self.ANTHROPIC_API_KEY else '❌'}\n"
        )
