"""
PolyEdge v4 — Engine 2: Resolution Sniper v2
==============================================
Event-driven trading engine that connects real-world events to Polymarket positions.

How it works:
  1. Feeds (football, crypto, future: politics, weather) emit FeedEvents
  2. MarketMapper maps events → relevant Polymarket condition_ids
  3. GapDetector computes post-event fair value vs current market price
  4. If gap > threshold → execute sniper trade before market corrects
  5. Confidence-weighted sizing with decay (edge shrinks as market adjusts)

Examples:
  - Goal scored in Man City vs Arsenal → "Man City to win Premier League" market
    should move up, but Polymarket is slow (30-120s delay) → buy the gap
  - BTC crosses $100K → "BTC above 100K by March" at 0.82 should jump to ~0.95
    → buy before price catches up
  - Match ends 0-0 draw → "Over 2.5 goals" resolves NO → buy NO at 0.91

Revenue target: $150-200/month (fewer trades but higher edge per trade)
Risk: $75/trade max, $300 total exposure
"""

import asyncio
import math
import time
import random
import logging
from typing import Dict, List, Optional, Tuple, Set, Callable
from dataclasses import dataclass, field
from collections import defaultdict
from enum import Enum

logger = logging.getLogger("polyedge.sniper")


# ─────────────────────────────────────────────
# Imports from existing codebase
# ─────────────────────────────────────────────
# These reference the existing feeds/base.py types
# In production: from feeds.base import FeedEvent, EventType

class EventType(Enum):
    """Mirror of feeds.base.EventType — kept here for standalone testing."""
    GOAL = "GOAL"
    RED_CARD = "RED_CARD"
    PENALTY = "PENALTY"
    OWN_GOAL = "OWN_GOAL"
    MATCH_START = "MATCH_START"
    MATCH_END = "MATCH_END"
    HALF_TIME = "HALF_TIME"
    PRICE_THRESHOLD_CROSS = "PRICE_THRESHOLD_CROSS"
    PRICE_PUMP = "PRICE_PUMP"
    PRICE_DUMP = "PRICE_DUMP"
    # v4 additions
    POLITICAL_EVENT = "POLITICAL_EVENT"
    WEATHER_EVENT = "WEATHER_EVENT"
    CUSTOM = "CUSTOM"


@dataclass
class FeedEvent:
    """Mirror of feeds.base.FeedEvent."""
    event_type: EventType
    source: str          # "football", "crypto", etc.
    timestamp: float
    received_at: float
    data: Dict           # Source-specific payload
    keywords: List[str]  # For market matching
    confidence: float    # 0-1


# ─────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────

class TradeUrgency(Enum):
    """How fast we need to execute."""
    CRITICAL = "CRITICAL"    # <30s window (goal, match end)
    HIGH = "HIGH"            # <2min window (red card, crypto threshold)
    MEDIUM = "MEDIUM"        # <10min window (half-time, pump/dump)
    LOW = "LOW"              # <1h window (political, weather)


@dataclass
class MarketMapping:
    """Links a Polymarket market to event patterns."""
    condition_id: str
    token_id_yes: str
    token_id_no: str
    question: str
    category: str              # "football", "crypto", "politics"
    keywords: List[str]        # Terms that match this market
    team_home: str = ""        # For football: "Manchester City"
    team_away: str = ""        # For football: "Arsenal"
    league_id: int = 0         # API-Football league ID
    crypto_symbol: str = ""    # For crypto: "BTCUSDT"
    threshold_price: float = 0 # For crypto thresholds: 100000
    threshold_direction: str = "" # "above" or "below"
    current_price: float = 0.5
    liquidity: float = 0
    end_date_iso: str = ""
    last_updated: float = 0.0

    @property
    def is_stale(self) -> bool:
        return time.time() - self.last_updated > 600  # 10 min


@dataclass
class GapSignal:
    """A detected gap between fair value and market price."""
    condition_id: str
    token_id: str
    direction: str             # "BUY" or "SELL"
    market_question: str
    current_price: float
    fair_value: float
    gap_cents: float           # Gap in cents
    edge_pct: float            # Expected return %
    confidence: float          # 0-100
    urgency: TradeUrgency
    trigger_event: FeedEvent
    decay_start: float         # Timestamp when gap was detected
    reasoning: str             # Human-readable explanation
    
    @property
    def age_seconds(self) -> float:
        return time.time() - self.decay_start
    
    @property
    def decayed_edge(self) -> float:
        """Edge decays as market catches up. Half-life = 60 seconds."""
        half_life = 60.0
        decay = math.exp(-0.693 * self.age_seconds / half_life)
        return self.edge_pct * decay
    
    @property
    def is_expired(self) -> bool:
        """Gap is expired if edge has decayed below 1%."""
        return self.decayed_edge < 1.0


@dataclass
class SniperTrade:
    """Executed or pending sniper trade."""
    signal: GapSignal
    size: float
    entry_price: float
    order_id: Optional[str] = None
    filled: bool = False
    fill_price: float = 0.0
    fill_time: float = 0.0
    pnl: float = 0.0
    status: str = "pending"  # pending, filled, cancelled, expired


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

@dataclass
class SniperConfig:
    """Resolution Sniper v2 configuration."""
    # Gap detection
    min_gap_cents: float = 3.0          # 3¢ minimum gap to trade
    min_confidence: float = 60.0        # Minimum event confidence
    min_edge_pct: float = 3.0           # 3% minimum expected return
    max_event_age_secs: float = 120     # Ignore events older than 2 min
    
    # Execution
    max_trade_size: float = 75.0        # $75 max per sniper trade
    min_trade_size: float = 10.0        # $10 minimum
    max_total_exposure: float = 300.0   # $300 total across all sniper positions
    max_concurrent_trades: int = 6      # Max simultaneous positions
    kelly_fraction: float = 0.3         # 30% Kelly for sniper (higher edge = more aggressive)
    
    # Timing
    execution_delay_ms: int = 500       # 500ms delay for stealth
    gap_expiry_secs: float = 300        # 5 min max gap lifetime
    market_refresh_secs: float = 120    # Refresh market mappings every 2 min
    
    # Football-specific
    goal_fair_value_shift: float = 0.12   # A goal shifts win probability ~12%
    red_card_fair_value_shift: float = 0.08
    penalty_fair_value_shift: float = 0.05  # Awarded but not yet taken
    match_end_near_resolution: float = 0.97  # If match ends, outcome ~97% certain
    
    # Crypto-specific
    threshold_cross_shift: float = 0.15   # Crossing a key level shifts ~15%
    pump_dump_shift: float = 0.08         # 5%+ move shifts related markets ~8%
    
    # Risk
    max_loss_per_trade: float = 25.0    # Stop loss per trade
    fee_rate: float = 0.01             # 1% Polymarket fee
    
    # Stealth
    size_jitter_pct: float = 0.12       # ±12% random size
    time_jitter_ms: int = 2000          # ±2s random timing


# ─────────────────────────────────────────────
# Market Mapper
# ─────────────────────────────────────────────

class MarketMapper:
    """
    Maps real-world events to Polymarket condition_ids.
    
    Two strategies:
      1. Keyword matching — event keywords vs market question text
      2. Structured mapping — pre-configured links (team → market, symbol → market)
    
    The mapper maintains an index of active markets and their associations.
    """
    
    def __init__(self, config: SniperConfig):
        self.config = config
        
        # Indexes
        self._markets: Dict[str, MarketMapping] = {}  # condition_id → mapping
        self._keyword_index: Dict[str, List[str]] = defaultdict(list)  # keyword → [condition_ids]
        self._team_index: Dict[str, List[str]] = defaultdict(list)     # team_name → [condition_ids]
        self._crypto_index: Dict[str, List[str]] = defaultdict(list)   # symbol → [condition_ids]
        self._league_index: Dict[int, List[str]] = defaultdict(list)   # league_id → [condition_ids]
        
        self._last_index_build = 0.0
    
    def index_markets(self, raw_markets: List[Dict]):
        """
        Build search indexes from raw Polymarket API data.
        
        Extracts team names, crypto symbols, and keywords from market questions
        to enable fast event→market lookups.
        """
        self._markets.clear()
        self._keyword_index.clear()
        self._team_index.clear()
        self._crypto_index.clear()
        self._league_index.clear()
        
        for m in raw_markets:
            try:
                # Parse token IDs from Gamma API format
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
                
                cid = m.get("conditionId", m.get("condition_id", ""))
                question = m.get("question", "").lower()
                category = m.get("category", "").lower()
                
                # Parse outcome prices for current_price
                outcome_prices_raw = m.get("outcomePrices", "[]")
                if isinstance(outcome_prices_raw, str):
                    try:
                        outcome_prices = _json.loads(outcome_prices_raw)
                    except:
                        outcome_prices = []
                else:
                    outcome_prices = outcome_prices_raw or []
                
                current_price = float(outcome_prices[0]) if outcome_prices else 0.5
                
                mapping = MarketMapping(
                    condition_id=cid,
                    token_id_yes=clob_token_ids[0],
                    token_id_no=clob_token_ids[1],
                    question=m.get("question", ""),
                    category=category,
                    keywords=self._extract_keywords(question),
                    current_price=current_price,
                    liquidity=float(m.get("liquidityNum", m.get("liquidity", 0)) or 0),
                    end_date_iso=m.get("endDate", m.get("end_date_iso", "")),
                    last_updated=time.time(),
                )
                
                # Detect football markets
                self._detect_football_market(mapping, question)
                
                # Detect crypto markets
                self._detect_crypto_market(mapping, question)
                
                self._markets[cid] = mapping
                
                # Build keyword index
                for kw in mapping.keywords:
                    self._keyword_index[kw].append(cid)
                
                # Build team index
                if mapping.team_home:
                    self._team_index[mapping.team_home.lower()].append(cid)
                if mapping.team_away:
                    self._team_index[mapping.team_away.lower()].append(cid)
                
                # Build crypto index
                if mapping.crypto_symbol:
                    self._crypto_index[mapping.crypto_symbol].append(cid)
                
            except Exception as e:
                continue
        
        self._last_index_build = time.time()
        logger.info(
            f"Indexed {len(self._markets)} markets: "
            f"{len(self._team_index)} teams, "
            f"{len(self._crypto_index)} crypto symbols, "
            f"{sum(len(v) for v in self._keyword_index.values())} keyword links"
        )
    
    def find_markets_for_event(self, event: FeedEvent) -> List[MarketMapping]:
        """
        Find all Polymarket markets relevant to a feed event.
        
        Returns markets sorted by relevance (most relevant first).
        """
        candidates: Dict[str, float] = {}  # condition_id → relevance score
        
        if event.source == "football":
            self._match_football_event(event, candidates)
        elif event.source == "crypto":
            self._match_crypto_event(event, candidates)
        
        # Keyword fallback for all event types
        self._match_keywords(event, candidates)
        
        # Sort by relevance, return mappings
        sorted_cids = sorted(candidates.keys(), key=lambda c: candidates[c], reverse=True)
        results = []
        for cid in sorted_cids[:10]:  # Top 10 matches
            mapping = self._markets.get(cid)
            if mapping:
                results.append(mapping)
        
        return results
    
    def _match_football_event(self, event: FeedEvent, candidates: Dict[str, float]):
        """Match football events to markets via team names and league."""
        data = event.data
        
        # Direct team match (highest relevance)
        home = data.get("home_team", "").lower()
        away = data.get("away_team", "").lower()
        
        for team in [home, away]:
            if team:
                for cid in self._team_index.get(team, []):
                    candidates[cid] = candidates.get(cid, 0) + 10.0
                
                # Partial match: check if team name is substring of indexed teams
                for indexed_team, cids in self._team_index.items():
                    if team in indexed_team or indexed_team in team:
                        for cid in cids:
                            candidates[cid] = candidates.get(cid, 0) + 5.0
        
        # League match (lower relevance — affects league-level markets)
        league_id = data.get("league_id", 0)
        if league_id:
            for cid in self._league_index.get(league_id, []):
                candidates[cid] = candidates.get(cid, 0) + 3.0
    
    def _match_crypto_event(self, event: FeedEvent, candidates: Dict[str, float]):
        """Match crypto events to markets via symbol and price levels."""
        data = event.data
        symbol = data.get("symbol", "")
        
        if symbol:
            for cid in self._crypto_index.get(symbol, []):
                candidates[cid] = candidates.get(cid, 0) + 10.0
            
            # Also check base asset (BTC from BTCUSDT)
            base = symbol.replace("USDT", "").replace("USD", "")
            for indexed_sym, cids in self._crypto_index.items():
                if base in indexed_sym:
                    for cid in cids:
                        candidates[cid] = candidates.get(cid, 0) + 5.0
    
    def _match_keywords(self, event: FeedEvent, candidates: Dict[str, float]):
        """Keyword-based fuzzy matching for any event type."""
        for kw in event.keywords:
            kw_lower = kw.lower()
            for cid in self._keyword_index.get(kw_lower, []):
                candidates[cid] = candidates.get(cid, 0) + 2.0
            
            # Substring match against all keywords
            for indexed_kw, cids in self._keyword_index.items():
                if kw_lower in indexed_kw or indexed_kw in kw_lower:
                    for cid in cids:
                        candidates[cid] = candidates.get(cid, 0) + 1.0
    
    def _detect_football_market(self, mapping: MarketMapping, question: str):
        """Extract team names and league from market question."""
        # Common patterns:
        # "Will Manchester City win the Premier League?"
        # "Manchester City vs Arsenal: Who will win?"
        # "Will Liverpool finish in the top 4?"
        
        # Known team names (expandable)
        teams = [
            "manchester city", "manchester united", "arsenal", "liverpool",
            "chelsea", "tottenham", "newcastle", "aston villa", "brighton",
            "west ham", "crystal palace", "everton", "nottingham forest",
            "brentford", "fulham", "bournemouth", "wolves", "burnley",
            "sheffield united", "luton town",
            # La Liga
            "real madrid", "barcelona", "atletico madrid", "real sociedad",
            "athletic bilbao", "villarreal", "betis", "sevilla",
            # Bundesliga
            "bayern munich", "borussia dortmund", "rb leipzig", "bayer leverkusen",
            # Serie A
            "inter milan", "ac milan", "juventus", "napoli", "roma", "lazio",
            # Ligue 1
            "psg", "paris saint-germain", "marseille", "lyon", "monaco",
        ]
        
        found_teams = []
        for team in teams:
            if team in question:
                found_teams.append(team)
        
        if len(found_teams) >= 2:
            mapping.team_home = found_teams[0].title()
            mapping.team_away = found_teams[1].title()
            mapping.category = "football"
        elif len(found_teams) == 1:
            mapping.team_home = found_teams[0].title()
            mapping.category = "football"
        
        # Detect league
        league_map = {
            "premier league": 39, "la liga": 140, "bundesliga": 78,
            "serie a": 135, "ligue 1": 61, "champions league": 2,
            "europa league": 3, "fa cup": 45,
        }
        for league_name, league_id in league_map.items():
            if league_name in question:
                mapping.league_id = league_id
                break
    
    def _detect_crypto_market(self, mapping: MarketMapping, question: str):
        """Extract crypto symbol and threshold from market question."""
        # Patterns:
        # "Will Bitcoin be above $100,000 by March 31?"
        # "Will ETH reach $5,000?"
        # "Will Solana hit $300?"
        
        symbol_map = {
            "bitcoin": "BTCUSDT", "btc": "BTCUSDT",
            "ethereum": "ETHUSDT", "eth": "ETHUSDT",
            "solana": "SOLUSDT", "sol": "SOLUSDT",
            "cardano": "ADAUSDT", "ada": "ADAUSDT",
            "dogecoin": "DOGEUSDT", "doge": "DOGEUSDT",
            "xrp": "XRPUSDT", "ripple": "XRPUSDT",
        }
        
        for name, symbol in symbol_map.items():
            if name in question:
                mapping.crypto_symbol = symbol
                mapping.category = "crypto"
                break
        
        # Extract price threshold
        import re
        price_match = re.search(r'\$[\d,]+(?:\.\d+)?', question)
        if price_match:
            try:
                price_str = price_match.group().replace("$", "").replace(",", "")
                mapping.threshold_price = float(price_str)
            except ValueError:
                pass
        
        # Detect direction
        if any(w in question for w in ["above", "over", "exceed", "reach", "hit", "surpass"]):
            mapping.threshold_direction = "above"
        elif any(w in question for w in ["below", "under", "drop", "fall"]):
            mapping.threshold_direction = "below"
    
    @staticmethod
    def _extract_keywords(question: str) -> List[str]:
        """Extract searchable keywords from a market question."""
        # Remove common stop words
        stop_words = {
            "will", "the", "be", "by", "in", "on", "at", "to", "of", "a", "an",
            "is", "and", "or", "for", "with", "this", "that", "it", "its",
            "what", "who", "when", "where", "how", "does", "do", "has", "have",
            "before", "after", "above", "below", "over", "under", "more", "than",
        }
        
        import re
        words = re.findall(r'[a-z]+', question)
        keywords = [w for w in words if w not in stop_words and len(w) > 2]
        return keywords
    
    def get_stats(self) -> Dict:
        return {
            "total_markets": len(self._markets),
            "football_teams": len(self._team_index),
            "crypto_symbols": len(self._crypto_index),
            "keyword_entries": len(self._keyword_index),
            "index_age_secs": round(time.time() - self._last_index_build, 0),
        }


# ─────────────────────────────────────────────
# Gap Detector
# ─────────────────────────────────────────────

class GapDetector:
    """
    Computes fair value shift from events and detects tradeable gaps.
    
    For each event type, estimates how much the true probability should shift,
    then compares against the current market price to find gaps.
    """
    
    def __init__(self, config: SniperConfig):
        self.config = config
        self._recent_signals: List[GapSignal] = []
        self._seen_events: Set[str] = set()  # Dedup by event key
    
    def analyze_event(self, event: FeedEvent, 
                       markets: List[MarketMapping]) -> List[GapSignal]:
        """
        Analyze a feed event against matched markets to find gaps.
        
        Returns list of tradeable gap signals, sorted by edge.
        """
        # Dedup
        event_key = f"{event.source}:{event.event_type.value}:{event.timestamp}"
        if event_key in self._seen_events:
            return []
        self._seen_events.add(event_key)
        
        # Trim old events from dedup set
        if len(self._seen_events) > 10000:
            self._seen_events = set(list(self._seen_events)[-5000:])
        
        # Check event freshness
        age = time.time() - event.timestamp
        if age > self.config.max_event_age_secs:
            logger.debug(f"Skipping stale event ({age:.0f}s old): {event.event_type.value}")
            return []
        
        signals = []
        
        for market in markets:
            signal = self._compute_gap(event, market)
            if signal:
                signals.append(signal)
        
        # Sort by edge (highest first)
        signals.sort(key=lambda s: s.edge_pct, reverse=True)
        
        if signals:
            logger.info(
                f"🎯 {len(signals)} gap(s) from {event.event_type.value}: "
                + " | ".join(f"{s.gap_cents:.1f}¢ edge on {s.market_question[:40]}" for s in signals[:3])
            )
        
        self._recent_signals.extend(signals)
        # Keep only recent signals
        cutoff = time.time() - 3600
        self._recent_signals = [s for s in self._recent_signals if s.decay_start > cutoff]
        
        return signals
    
    def _compute_gap(self, event: FeedEvent, 
                      market: MarketMapping) -> Optional[GapSignal]:
        """Compute fair value gap for a specific event-market pair."""
        
        current_price = market.current_price
        if current_price <= 0.01 or current_price >= 0.99:
            return None  # Already resolved
        
        # Estimate fair value shift based on event type
        fair_value, confidence, urgency, reasoning = self._estimate_fair_value(
            event, market, current_price
        )
        
        if fair_value is None:
            return None
        
        # Clamp fair value
        fair_value = max(0.02, min(0.98, fair_value))
        
        # Compute gap
        gap = fair_value - current_price
        gap_cents = abs(gap) * 100
        
        if gap_cents < self.config.min_gap_cents:
            return None
        
        # Direction
        if gap > 0:
            direction = "BUY"
            token_id = market.token_id_yes
            edge_pct = (fair_value - current_price) / current_price * 100
        else:
            direction = "SELL"  # Or buy NO token
            token_id = market.token_id_no
            edge_pct = (current_price - fair_value) / (1 - current_price) * 100
            # Adjust: buying NO token at (1 - current_price)
            current_price = 1 - current_price
            fair_value = 1 - fair_value
        
        # Subtract fees
        edge_pct -= self.config.fee_rate * 100  # 1% fee
        
        if edge_pct < self.config.min_edge_pct:
            return None
        
        if confidence < self.config.min_confidence:
            return None
        
        return GapSignal(
            condition_id=market.condition_id,
            token_id=token_id,
            direction=direction,
            market_question=market.question,
            current_price=current_price,
            fair_value=fair_value,
            gap_cents=gap_cents,
            edge_pct=edge_pct,
            confidence=confidence,
            urgency=urgency,
            trigger_event=event,
            decay_start=time.time(),
            reasoning=reasoning,
        )
    
    def _estimate_fair_value(self, event: FeedEvent, market: MarketMapping,
                              current_price: float) -> Tuple[Optional[float], float, TradeUrgency, str]:
        """
        Estimate post-event fair value for a market.
        
        Returns: (fair_value, confidence, urgency, reasoning)
        """
        et = event.event_type
        data = event.data
        
        # ── Football Events ──
        
        if et == EventType.GOAL:
            return self._football_goal(event, market, current_price)
        
        if et == EventType.RED_CARD:
            return self._football_red_card(event, market, current_price)
        
        if et == EventType.PENALTY:
            return self._football_penalty(event, market, current_price)
        
        if et == EventType.MATCH_END:
            return self._football_match_end(event, market, current_price)
        
        if et == EventType.HALF_TIME:
            return self._football_half_time(event, market, current_price)
        
        # ── Crypto Events ──
        
        if et == EventType.PRICE_THRESHOLD_CROSS:
            return self._crypto_threshold(event, market, current_price)
        
        if et in (EventType.PRICE_PUMP, EventType.PRICE_DUMP):
            return self._crypto_pump_dump(event, market, current_price)
        
        return None, 0, TradeUrgency.LOW, ""
    
    # ── Football Fair Value Estimators ──
    
    def _football_goal(self, event: FeedEvent, market: MarketMapping,
                        current_price: float) -> Tuple[Optional[float], float, TradeUrgency, str]:
        """
        Goal scored → shift win probability.
        
        Logic:
          - If scoring team is market.team_home and market asks "will team_home win?"
            → shift UP by goal_fair_value_shift
          - Adjusted by match minute (later goals matter more)
          - Adjusted by current score differential
        """
        data = event.data
        scoring_team = data.get("team", "").lower()
        minute = data.get("minute", 45)
        home_score = data.get("home_score", 0)
        away_score = data.get("away_score", 0)
        
        question_lower = market.question.lower()
        
        # Determine if goal helps or hurts the market's subject
        team_home_lower = market.team_home.lower() if market.team_home else ""
        team_away_lower = market.team_away.lower() if market.team_away else ""
        
        is_home_goal = scoring_team and (scoring_team in team_home_lower or team_home_lower in scoring_team)
        is_away_goal = scoring_team and (scoring_team in team_away_lower or team_away_lower in scoring_team)
        
        # Determine market direction (does a home goal help or hurt?)
        home_positive = team_home_lower and team_home_lower in question_lower and \
                        any(w in question_lower for w in ["win", "champion", "title", "top", "qualify"])
        
        if not (is_home_goal or is_away_goal):
            return None, 0, TradeUrgency.HIGH, ""
        
        # Base shift (adjusted by minute — later goals are more decisive)
        minute_factor = 0.5 + 0.5 * (minute / 90.0)  # 0.5 at 0', 1.0 at 90'
        base_shift = self.config.goal_fair_value_shift * minute_factor
        
        # Score differential matters: first goal > equalizer > extending lead
        score_diff = home_score - away_score
        if abs(score_diff) == 1:
            base_shift *= 1.2  # Close game, goal matters more
        elif abs(score_diff) >= 3:
            base_shift *= 0.5  # Blowout, goal matters less
        
        # Apply direction
        if (is_home_goal and home_positive) or (is_away_goal and not home_positive):
            fair_value = current_price + base_shift
            reasoning = f"Goal by {scoring_team} at {minute}' favors market outcome (+{base_shift*100:.1f}¢)"
        else:
            fair_value = current_price - base_shift
            reasoning = f"Goal by {scoring_team} at {minute}' hurts market outcome (-{base_shift*100:.1f}¢)"
        
        confidence = min(85, 60 + minute_factor * 25) * event.confidence
        
        return fair_value, confidence, TradeUrgency.CRITICAL, reasoning
    
    def _football_red_card(self, event: FeedEvent, market: MarketMapping,
                            current_price: float) -> Tuple[Optional[float], float, TradeUrgency, str]:
        """Red card → shift against the carded team."""
        data = event.data
        team = data.get("team", "").lower()
        minute = data.get("minute", 45)
        
        team_home_lower = market.team_home.lower() if market.team_home else ""
        question_lower = market.question.lower()
        
        is_home_card = team and (team in team_home_lower or team_home_lower in team)
        home_positive = team_home_lower and team_home_lower in question_lower
        
        minute_factor = 0.5 + 0.5 * (minute / 90.0)
        shift = self.config.red_card_fair_value_shift * minute_factor
        
        # Red card hurts the team that got it
        if (is_home_card and home_positive) or (not is_home_card and not home_positive):
            fair_value = current_price - shift
            reasoning = f"Red card to {team} at {minute}' hurts outcome (-{shift*100:.1f}¢)"
        else:
            fair_value = current_price + shift
            reasoning = f"Red card to {team} at {minute}' helps outcome (+{shift*100:.1f}¢)"
        
        confidence = min(75, 55 + minute_factor * 20) * event.confidence
        
        return fair_value, confidence, TradeUrgency.HIGH, reasoning
    
    def _football_penalty(self, event: FeedEvent, market: MarketMapping,
                           current_price: float) -> Tuple[Optional[float], float, TradeUrgency, str]:
        """Penalty awarded → smaller shift (not yet scored)."""
        data = event.data
        team = data.get("team", "").lower()
        
        # Penalty conversion rate ~76%
        shift = self.config.penalty_fair_value_shift * 0.76
        
        team_home_lower = market.team_home.lower() if market.team_home else ""
        question_lower = market.question.lower()
        is_home_pen = team and (team in team_home_lower or team_home_lower in team)
        home_positive = team_home_lower and team_home_lower in question_lower
        
        if (is_home_pen and home_positive) or (not is_home_pen and not home_positive):
            fair_value = current_price + shift
            reasoning = f"Penalty to {team} — 76% conversion rate (+{shift*100:.1f}¢)"
        else:
            fair_value = current_price - shift
            reasoning = f"Penalty to {team} against favored outcome (-{shift*100:.1f}¢)"
        
        return fair_value, 55 * event.confidence, TradeUrgency.HIGH, reasoning
    
    def _football_match_end(self, event: FeedEvent, market: MarketMapping,
                             current_price: float) -> Tuple[Optional[float], float, TradeUrgency, str]:
        """Match ended → outcome is (nearly) certain."""
        data = event.data
        home_score = data.get("home_score", 0)
        away_score = data.get("away_score", 0)
        
        question_lower = market.question.lower()
        team_home_lower = market.team_home.lower() if market.team_home else ""
        
        home_positive = team_home_lower and team_home_lower in question_lower
        
        # Determine result
        if home_score > away_score:
            home_won = True
        elif away_score > home_score:
            home_won = False
        else:
            # Draw — depends on market type
            return None, 0, TradeUrgency.CRITICAL, ""
        
        if (home_won and home_positive) or (not home_won and not home_positive):
            fair_value = self.config.match_end_near_resolution
            reasoning = f"Match ended {home_score}-{away_score}, outcome confirmed"
        else:
            fair_value = 1 - self.config.match_end_near_resolution
            reasoning = f"Match ended {home_score}-{away_score}, outcome against market"
        
        return fair_value, 90 * event.confidence, TradeUrgency.CRITICAL, reasoning
    
    def _football_half_time(self, event: FeedEvent, market: MarketMapping,
                             current_price: float) -> Tuple[Optional[float], float, TradeUrgency, str]:
        """Half-time — use score to adjust, but less certain than full-time."""
        data = event.data
        home_score = data.get("home_score", 0)
        away_score = data.get("away_score", 0)
        
        if home_score == away_score:
            return None, 0, TradeUrgency.MEDIUM, ""  # Draw at HT — low signal
        
        # HT leader wins ~75% of the time historically
        question_lower = market.question.lower()
        team_home_lower = market.team_home.lower() if market.team_home else ""
        home_positive = team_home_lower and team_home_lower in question_lower
        home_leading = home_score > away_score
        
        shift = 0.06 * abs(home_score - away_score)  # 6¢ per goal lead
        
        if (home_leading and home_positive) or (not home_leading and not home_positive):
            fair_value = current_price + shift
            reasoning = f"HT: {home_score}-{away_score}, leading team wins ~75% (+{shift*100:.1f}¢)"
        else:
            fair_value = current_price - shift
            reasoning = f"HT: {home_score}-{away_score}, trailing team disadvantaged (-{shift*100:.1f}¢)"
        
        return fair_value, 55 * event.confidence, TradeUrgency.MEDIUM, reasoning
    
    # ── Crypto Fair Value Estimators ──
    
    def _crypto_threshold(self, event: FeedEvent, market: MarketMapping,
                           current_price: float) -> Tuple[Optional[float], float, TradeUrgency, str]:
        """
        Crypto price crossed a key threshold → shift probability.
        
        Example: BTC crosses $100K, market "BTC above $100K by March" at 0.82
        → fair value should be ~0.95 (already above, question is whether it stays)
        """
        data = event.data
        symbol = data.get("symbol", "")
        price = data.get("price", 0)
        direction = data.get("direction", "")  # "above" or "below"
        threshold = data.get("threshold", 0)
        
        if not market.threshold_price:
            return None, 0, TradeUrgency.HIGH, ""
        
        # Check if the threshold cross is relevant to this market
        price_distance_pct = abs(price - market.threshold_price) / max(market.threshold_price, 1) * 100
        
        if price_distance_pct > 10:
            # Price is far from market's threshold — weak signal
            shift = self.config.threshold_cross_shift * 0.3
            confidence = 40
        else:
            # Price crossed near the market's threshold — strong signal
            shift = self.config.threshold_cross_shift
            confidence = 80
        
        # Direction alignment
        market_wants_above = market.threshold_direction == "above"
        price_went_above = direction == "above"
        
        if market_wants_above == price_went_above:
            fair_value = current_price + shift
            reasoning = f"{symbol} crossed ${threshold:,.0f} ({direction}) — favors market (+{shift*100:.1f}¢)"
        else:
            fair_value = current_price - shift
            reasoning = f"{symbol} crossed ${threshold:,.0f} ({direction}) — against market (-{shift*100:.1f}¢)"
        
        return fair_value, confidence * event.confidence, TradeUrgency.HIGH, reasoning
    
    def _crypto_pump_dump(self, event: FeedEvent, market: MarketMapping,
                           current_price: float) -> Tuple[Optional[float], float, TradeUrgency, str]:
        """Crypto pump/dump → shift related markets."""
        data = event.data
        symbol = data.get("symbol", "")
        change_pct = data.get("change_pct", 0)
        is_pump = event.event_type == EventType.PRICE_PUMP
        
        # Scale shift by magnitude of move
        magnitude = min(abs(change_pct) / 10.0, 2.0)  # Cap at 2x
        shift = self.config.pump_dump_shift * magnitude
        
        market_wants_above = market.threshold_direction == "above"
        
        if (is_pump and market_wants_above) or (not is_pump and not market_wants_above):
            fair_value = current_price + shift
            direction_word = "pump" if is_pump else "dump"
            reasoning = f"{symbol} {direction_word} {change_pct:+.1f}% — favors market (+{shift*100:.1f}¢)"
        else:
            fair_value = current_price - shift
            direction_word = "pump" if is_pump else "dump"
            reasoning = f"{symbol} {direction_word} {change_pct:+.1f}% — against market (-{shift*100:.1f}¢)"
        
        return fair_value, 60 * event.confidence, TradeUrgency.MEDIUM, reasoning
    
    def get_recent_signals(self, limit: int = 20) -> List[GapSignal]:
        """Get recent gap signals for dashboard."""
        return sorted(
            self._recent_signals, 
            key=lambda s: s.decay_start, 
            reverse=True
        )[:limit]
    
    def get_stats(self) -> Dict:
        active = [s for s in self._recent_signals if not s.is_expired]
        return {
            "total_signals": len(self._recent_signals),
            "active_signals": len(active),
            "avg_edge": round(
                sum(s.edge_pct for s in active) / max(len(active), 1), 2
            ),
            "event_types_seen": len(self._seen_events),
        }


# ─────────────────────────────────────────────
# Execution Manager
# ─────────────────────────────────────────────

class SniperExecutor:
    """Executes sniper trades with sizing, risk management, and stealth."""
    
    def __init__(self, config: SniperConfig):
        self.config = config
        self._active_trades: List[SniperTrade] = []
        self._completed_trades: List[SniperTrade] = []
        self._total_exposure: float = 0.0
    
    def should_execute(self, signal: GapSignal) -> Tuple[bool, str]:
        """Pre-trade risk checks."""
        # Kill switch: max concurrent trades
        active = [t for t in self._active_trades if t.status == "filled"]
        if len(active) >= self.config.max_concurrent_trades:
            return False, f"Max concurrent trades ({self.config.max_concurrent_trades})"
        
        # Total exposure limit
        if self._total_exposure >= self.config.max_total_exposure:
            return False, f"Max exposure ${self.config.max_total_exposure}"
        
        # Check edge hasn't decayed
        if signal.is_expired:
            return False, f"Signal expired (edge {signal.decayed_edge:.1f}%)"
        
        # Minimum edge after decay
        if signal.decayed_edge < self.config.min_edge_pct:
            return False, f"Edge too low after decay ({signal.decayed_edge:.1f}%)"
        
        # Don't double-up on same market
        for t in self._active_trades:
            if t.signal.condition_id == signal.condition_id and t.status in ("pending", "filled"):
                return False, "Already have position in this market"
        
        return True, "OK"
    
    def compute_size(self, signal: GapSignal) -> float:
        """
        Kelly-based sizing with decay adjustment.
        
        size = kelly_fraction * edge / variance * max_size
        Capped by max_trade_size and remaining exposure budget.
        """
        edge = signal.decayed_edge / 100.0
        
        # Estimate variance from confidence (lower confidence = higher variance)
        variance = max(0.01, (100 - signal.confidence) / 100.0)
        
        kelly = edge / variance
        size = kelly * self.config.kelly_fraction * self.config.max_trade_size
        
        # Apply jitter for stealth
        jitter = 1.0 + random.uniform(
            -self.config.size_jitter_pct,
            self.config.size_jitter_pct
        )
        size *= jitter
        
        # Clamp
        remaining_budget = self.config.max_total_exposure - self._total_exposure
        size = max(self.config.min_trade_size, min(size, self.config.max_trade_size, remaining_budget))
        
        return round(size, 1)
    
    def record_execution(self, signal: GapSignal, size: float, 
                          entry_price: float, order_id: str = None) -> SniperTrade:
        """Record a trade execution."""
        trade = SniperTrade(
            signal=signal,
            size=size,
            entry_price=entry_price,
            order_id=order_id,
            filled=True,
            fill_price=entry_price,
            fill_time=time.time(),
            status="filled",
        )
        self._active_trades.append(trade)
        self._total_exposure += size
        return trade
    
    def close_trade(self, trade: SniperTrade, exit_price: float):
        """Close a trade and compute PnL."""
        if trade.signal.direction == "BUY":
            trade.pnl = (exit_price - trade.fill_price) * trade.size
        else:
            trade.pnl = (trade.fill_price - exit_price) * trade.size
        
        # Subtract fees
        trade.pnl -= self.config.fee_rate * trade.size * 2  # Entry + exit fee
        
        trade.status = "closed"
        self._total_exposure -= trade.size
        self._completed_trades.append(trade)
        self._active_trades.remove(trade)
    
    def get_stats(self) -> Dict:
        completed = self._completed_trades
        wins = [t for t in completed if t.pnl > 0]
        
        return {
            "active_trades": len(self._active_trades),
            "completed_trades": len(completed),
            "win_rate": round(len(wins) / max(len(completed), 1) * 100, 1),
            "total_pnl": round(sum(t.pnl for t in completed), 2),
            "avg_pnl": round(sum(t.pnl for t in completed) / max(len(completed), 1), 2),
            "total_exposure": round(self._total_exposure, 2),
            "active_positions": [
                {
                    "market": t.signal.market_question,
                    "direction": t.signal.direction,
                    "size": t.size,
                    "entry": t.fill_price,
                    "edge": round(t.signal.decayed_edge, 1),
                    "age_s": round(t.signal.age_seconds, 0),
                }
                for t in self._active_trades if t.status == "filled"
            ],
        }
    
    def save_state(self) -> Dict:
        """Save executor state for persistence."""
        def _trade_to_dict(t: SniperTrade) -> Dict:
            return {
                "size": t.size,
                "entry_price": t.entry_price,
                "order_id": t.order_id,
                "filled": t.filled,
                "fill_price": t.fill_price,
                "fill_time": t.fill_time,
                "pnl": t.pnl,
                "status": t.status,
                "market_question": t.signal.market_question[:80] if t.signal else "",
                "direction": t.signal.direction if t.signal else "",
                "edge": t.signal.edge_cents if t.signal else 0,
            }
        return {
            "active_trades": [_trade_to_dict(t) for t in self._active_trades],
            "completed_trades": [_trade_to_dict(t) for t in self._completed_trades[-200:]],
            "total_exposure": self._total_exposure,
        }
    
    def load_state(self, state: Dict):
        """Restore completed trades for stats continuity (active trades not restored)."""
        if not state:
            return
        completed_data = state.get("completed_trades", [])
        for td in completed_data:
            from types import SimpleNamespace
            fake_signal = SimpleNamespace(
                market_question=td.get("market_question", ""),
                direction=td.get("direction", ""),
                edge_cents=td.get("edge", 0),
                decayed_edge=td.get("edge", 0),
                age_seconds=0,
                is_expired=True,
            )
            trade = SniperTrade(
                signal=fake_signal,
                size=td.get("size", 0),
                entry_price=td.get("entry_price", 0),
                fill_price=td.get("fill_price", 0),
                fill_time=td.get("fill_time", 0),
                pnl=td.get("pnl", 0),
                status=td.get("status", "completed"),
                filled=td.get("filled", True),
            )
            self._completed_trades.append(trade)
        logger.info(f"Restored {len(self._completed_trades)} completed sniper trades")


# ─────────────────────────────────────────────
# Resolution Sniper v2 Engine (Main Orchestrator)
# ─────────────────────────────────────────────

class ResolutionSniperV2:
    """
    Engine 2: Resolution Sniper v2
    
    Lifecycle:
      1. Feeds emit events (goals, crypto thresholds, etc.)
      2. MarketMapper finds relevant Polymarket markets
      3. GapDetector computes fair value gap
      4. SniperExecutor sizes and executes trades
      5. Monitor positions, close when gap narrows
    
    Runs as async loop inside main_v4.py orchestrator.
    Consumes events from the shared event bus (feeds).
    """
    
    def __init__(self, config: SniperConfig, clob_client, data_store, telegram):
        self.config = config
        self.clob = clob_client
        self.store = data_store
        self.telegram = telegram
        
        # Sub-components
        self.mapper = MarketMapper(config)
        self.gap_detector = GapDetector(config)
        self.executor = SniperExecutor(config)
        
        # State
        self._running = False
        self._paper_mode = True
        self._paused = False  # Manual pause via /stop command
        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._raw_markets: List[Dict] = []
        
        # Stats
        self._started_at = 0.0
        self._events_processed = 0
        self._gaps_detected = 0
        self._trades_executed = 0
    
    # ── Event Ingestion ──
    
    async def on_event(self, event: FeedEvent):
        """
        Called by feeds when an event occurs.
        This is the main entry point — feeds push events here.
        """
        try:
            await self._event_queue.put(event)
        except asyncio.QueueFull:
            logger.warning("Event queue full, dropping event")
    
    # ── Lifecycle ──
    
    async def start(self, paper_mode: bool = True):
        """Start the sniper engine."""
        self._running = True
        self._paper_mode = paper_mode
        self._started_at = time.time()
        
        mode_str = "PAPER" if paper_mode else "LIVE"
        logger.info(f"Resolution Sniper v2 starting in {mode_str} mode")
        
        if self.telegram:
            await self.telegram.send(
                f"🎯 Sniper v2 started ({mode_str})\n"
                f"Max exposure: ${self.config.max_total_exposure}\n"
                f"Min gap: {self.config.min_gap_cents}¢ | Min edge: {self.config.min_edge_pct}%"
            )
        
        await asyncio.gather(
            self._event_processing_loop(),
            self._market_refresh_loop(),
            self._position_monitor_loop(),
        )
    
    async def stop(self):
        """Gracefully stop."""
        self._running = False
        logger.info("Resolution Sniper v2 stopping...")
        
        stats = self.get_stats()
        if self.telegram:
            await self.telegram.send(
                f"🛑 Sniper v2 stopped\n"
                f"Events: {self._events_processed} | "
                f"Gaps: {self._gaps_detected} | "
                f"Trades: {self._trades_executed}\n"
                f"PnL: ${stats['executor']['total_pnl']:.2f}"
            )
    
    # ── Loop 1: Event Processing (real-time) ──
    
    async def _event_processing_loop(self):
        """Process events from the queue as they arrive."""
        while self._running:
            try:
                if self._paused:
                    await asyncio.sleep(5)
                    continue
                
                # Wait for event with timeout
                try:
                    event = await asyncio.wait_for(
                        self._event_queue.get(), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    continue
                
                self._events_processed += 1
                
                logger.info(
                    f"📡 Event: {event.event_type.value} from {event.source} "
                    f"({len(event.keywords)} keywords, conf={event.confidence:.0%})"
                )
                
                # Find matching markets
                markets = self.mapper.find_markets_for_event(event)
                
                if not markets:
                    logger.debug(f"No market matches for {event.event_type.value}")
                    continue
                
                logger.info(f"Matched {len(markets)} markets for {event.event_type.value}")
                
                # Update market prices before gap detection
                for market in markets:
                    price = await self.clob.get_price(market.token_id_yes)
                    if price:
                        market.current_price = price
                        market.last_updated = time.time()
                
                # Detect gaps
                signals = self.gap_detector.analyze_event(event, markets)
                self._gaps_detected += len(signals)
                
                # Execute on best signals
                for signal in signals[:3]:  # Max 3 trades per event
                    await self._try_execute(signal)
                
            except Exception as e:
                logger.error(f"Event processing error: {e}", exc_info=True)
    
    async def _try_execute(self, signal: GapSignal):
        """Attempt to execute a sniper trade."""
        # Risk checks
        can_trade, reason = self.executor.should_execute(signal)
        if not can_trade:
            logger.info(f"⛔ Sniper blocked: {reason} | {signal.market_question[:40]}")
            return
        
        # Compute size
        size = self.executor.compute_size(signal)
        
        # Stealth delay
        delay_ms = self.config.execution_delay_ms + random.randint(
            -self.config.time_jitter_ms, self.config.time_jitter_ms
        )
        await asyncio.sleep(max(0, delay_ms / 1000.0))
        
        # Check edge hasn't decayed during delay
        if signal.decayed_edge < self.config.min_edge_pct:
            logger.info(f"⏰ Edge decayed during delay ({signal.decayed_edge:.1f}%)")
            return
        
        if self._paper_mode:
            # Paper mode: simulate fill at current price
            trade = self.executor.record_execution(
                signal=signal,
                size=size,
                entry_price=signal.current_price,
            )
            self._trades_executed += 1
            
            log_msg = (
                f"📗 PAPER SNIPER {signal.direction} ${size:.0f} "
                f"@ {signal.current_price:.3f} | "
                f"fair={signal.fair_value:.3f} edge={signal.decayed_edge:.1f}% "
                f"| {signal.market_question[:50]}"
            )
            logger.info(log_msg)
            
            if self.telegram:
                await self.telegram.send(
                    f"🎯 Sniper Trade (PAPER)\n"
                    f"{signal.direction} ${size:.0f} @ {signal.current_price:.3f}\n"
                    f"Fair: {signal.fair_value:.3f} | Edge: {signal.decayed_edge:.1f}%\n"
                    f"Trigger: {signal.trigger_event.event_type.value}\n"
                    f"📊 {signal.market_question[:60]}\n"
                    f"💡 {signal.reasoning}"
                )
        else:
            # Live mode: place market order
            order = await self.clob.place_order(
                token_id=signal.token_id,
                side="BUY",
                size=size,
            )
            
            if order:
                trade = self.executor.record_execution(
                    signal=signal,
                    size=size,
                    entry_price=signal.current_price,
                    order_id=order.get("id"),
                )
                self._trades_executed += 1
                logger.info(
                    f"🎯 LIVE SNIPER {signal.direction} ${size:.0f} "
                    f"@ {signal.current_price:.3f} | order={order.get('id')}"
                )
    
    # ── Loop 2: Market Refresh (every 2 min) ──
    
    async def _market_refresh_loop(self):
        """Periodically refresh market index."""
        while self._running:
            try:
                raw_markets = await self.clob.get_markets()
                if raw_markets:
                    self._raw_markets = raw_markets
                    self.mapper.index_markets(raw_markets)
                    logger.info(f"Market index refreshed: {len(raw_markets)} markets")
            except Exception as e:
                logger.error(f"Market refresh error: {e}", exc_info=True)
            
            await asyncio.sleep(self.config.market_refresh_secs)
    
    # ── Loop 3: Position Monitor (every 30s) ──
    
    async def _position_monitor_loop(self):
        """Monitor active positions, close when target reached or expired."""
        await asyncio.sleep(10)
        
        while self._running:
            try:
                for trade in list(self.executor._active_trades):
                    if trade.status != "filled":
                        continue
                    
                    # Get current price
                    current_price = None
                    if not self._paper_mode:
                        current_price_raw = await self.clob.get_price(trade.signal.token_id)
                        if current_price_raw:
                            current_price = current_price_raw
                    else:
                        # Paper mode: simulate price movement toward fair value
                        # BUT with realistic noise and probability of wrong estimate
                        elapsed = time.time() - trade.fill_time
                        convergence = 1 - math.exp(-elapsed / 120)  # 2-min half-life
                        
                        # 30% chance the fair value estimate is partially wrong
                        # Model: actual_target = fair_value + error, where error ~ N(0, gap*0.4)
                        if not hasattr(trade, '_sim_target'):
                            gap = abs(trade.signal.fair_value - trade.fill_price)
                            error = random.gauss(0, gap * 0.4)
                            trade._sim_target = trade.signal.fair_value + error
                            trade._sim_target = max(0.02, min(0.98, trade._sim_target))
                        
                        current_price = trade.fill_price + \
                            (trade._sim_target - trade.fill_price) * convergence
                        # Add market noise
                        current_price += random.gauss(0, 0.008)
                        current_price = max(0.01, min(0.99, current_price))
                    
                    if current_price is None:
                        continue
                    
                    # Check exit conditions
                    should_close = False
                    close_reason = ""
                    
                    # Target reached: price converged to fair value
                    if trade.signal.direction == "BUY" and current_price >= trade.signal.fair_value * 0.95:
                        should_close = True
                        close_reason = "Target reached"
                    elif trade.signal.direction == "SELL" and current_price <= trade.signal.fair_value * 1.05:
                        should_close = True
                        close_reason = "Target reached"
                    
                    # Stop loss
                    unrealized = (current_price - trade.fill_price) * trade.size
                    if trade.signal.direction == "SELL":
                        unrealized = -unrealized
                    
                    if unrealized < -self.config.max_loss_per_trade:
                        should_close = True
                        close_reason = f"Stop loss (${unrealized:.2f})"
                    
                    # Time expiry (5 min max hold for sniper trades)
                    if trade.signal.age_seconds > self.config.gap_expiry_secs:
                        should_close = True
                        close_reason = "Time expiry"
                    
                    if should_close:
                        self.executor.close_trade(trade, current_price)
                        logger.info(
                            f"📕 Sniper closed: {close_reason} | "
                            f"PnL=${trade.pnl:.2f} | {trade.signal.market_question[:40]}"
                        )
                        
                        if self.telegram and abs(trade.pnl) > 1:
                            emoji = "✅" if trade.pnl > 0 else "❌"
                            await self.telegram.send(
                                f"{emoji} Sniper Closed: {close_reason}\n"
                                f"PnL: ${trade.pnl:+.2f}\n"
                                f"Entry: {trade.fill_price:.3f} → Exit: {current_price:.3f}\n"
                                f"📊 {trade.signal.market_question[:50]}"
                            )
                
            except Exception as e:
                logger.error(f"Position monitor error: {e}", exc_info=True)
            
            await asyncio.sleep(30)
    
    # ── Stats & Dashboard ──
    
    def get_stats(self) -> Dict:
        uptime = time.time() - self._started_at if self._started_at else 0
        
        return {
            "engine": "resolution_sniper_v2",
            "status": "running" if self._running else "stopped",
            "mode": "PAPER" if self._paper_mode else "LIVE",
            "uptime_hours": round(uptime / 3600, 1),
            "events_processed": self._events_processed,
            "gaps_detected": self._gaps_detected,
            "trades_executed": self._trades_executed,
            "mapper": self.mapper.get_stats(),
            "gap_detector": self.gap_detector.get_stats(),
            "executor": self.executor.get_stats(),
            "recent_signals": [
                {
                    "market": s.market_question,
                    "gap_cents": round(s.gap_cents, 1),
                    "edge_pct": round(s.decayed_edge, 1),
                    "direction": s.direction,
                    "urgency": s.urgency.value,
                    "trigger": s.trigger_event.event_type.value,
                    "age_s": round(s.age_seconds, 0),
                    "reasoning": s.reasoning,
                }
                for s in self.gap_detector.get_recent_signals(10)
            ],
        }
    
    def save_state(self) -> Dict:
        """Save sniper engine state for persistence."""
        return {
            "started_at": self._started_at,
            "events_processed": self._events_processed,
            "gaps_detected": self._gaps_detected,
            "trades_executed": self._trades_executed,
            "executor": self.executor.save_state(),
        }
    
    def load_state(self, state: Dict):
        """Restore sniper engine state from persistence."""
        if not state:
            return
        self._events_processed = state.get("events_processed", 0)
        self._gaps_detected = state.get("gaps_detected", 0)
        self._trades_executed = state.get("trades_executed", 0)
        if "executor" in state:
            self.executor.load_state(state["executor"])
        logger.info(
            f"Restored Sniper state: {self._trades_executed} trades, "
            f"{self._gaps_detected} gaps detected"
        )
