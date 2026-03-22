"""
PolyEdge v4 — Football Feed
Polls api-football.com v3 for live match events.

Emits FeedEvents:
  GOAL, RED_CARD, PENALTY, OWN_GOAL, MATCH_START, MATCH_END, HALF_TIME

Usage:
  feed = FootballFeed(api_key="...")
  events = await feed.poll()   # Returns list of FeedEvent
  feed.has_live_matches()      # True if any matches are live

API: https://v3.football.api-sports.io
Free tier: 100 requests/day — smart rate-limit recovery with budget tracking.

Rate-limit strategy:
  - Track daily request budget (max 90 to leave headroom)
  - On 429 / rate-limit error: back off 30 min, then retry
  - Idle polling: every 5 min (was 2 min) to conserve budget
  - Live polling: max 4 matches per cycle (was 8) with 2s gaps
  - Budget exhausted: sleep until midnight UTC reset
"""

import asyncio
import aiohttp
import logging
import time
import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
from enum import Enum

logger = logging.getLogger("polyedge.feeds.football")

# ─── Import FeedEvent from sniper or define locally ──────────

try:
    from engines.resolution_sniper_v2 import FeedEvent, EventType
except ImportError:
    class EventType(Enum):
        GOAL = "GOAL"
        RED_CARD = "RED_CARD"
        PENALTY = "PENALTY"
        OWN_GOAL = "OWN_GOAL"
        MATCH_START = "MATCH_START"
        MATCH_END = "MATCH_END"
        HALF_TIME = "HALF_TIME"
        CUSTOM = "CUSTOM"

    @dataclass
    class FeedEvent:
        event_type: EventType
        source: str
        timestamp: float
        received_at: float
        data: Dict
        keywords: List[str]
        confidence: float


# ─── Constants ───────────────────────────────────────────────

API_BASE = "https://v3.football.api-sports.io"

# Top leagues to monitor (api-football IDs)
TOP_LEAGUES = {
    39: "Premier League",
    140: "La Liga",
    135: "Serie A",
    78: "Bundesliga",
    61: "Ligue 1",
    2: "Champions League",
    3: "Europa League",
    848: "Conference League",
    253: "MLS",
    71: "Serie B",
    94: "Primeira Liga",
    88: "Eredivisie",
    144: "Belgian Pro League",
    40: "Championship",
    41: "League One",
    42: "League Two",
    203: "Liga MX",
}

# Event types from API → our EventType
API_EVENT_MAP = {
    "Goal": EventType.GOAL,
    "Normal Goal": EventType.GOAL,
    "Own Goal": EventType.OWN_GOAL,
    "Penalty": EventType.PENALTY,
    "Missed Penalty": EventType.PENALTY,
    "Red Card": EventType.RED_CARD,
    "Yellow Card": None,  # Ignore yellows
}


# ─── Match State Tracker ────────────────────────────────────

@dataclass
class MatchState:
    """Tracks a live match and its last known events."""
    fixture_id: int
    home_team: str
    away_team: str
    league: str
    league_id: int
    status: str = ""  # 1H, HT, 2H, FT, etc.
    home_score: int = 0
    away_score: int = 0
    elapsed: int = 0
    last_event_count: int = 0
    seen_event_ids: Set[str] = field(default_factory=set)
    started_at: float = 0.0


class FootballFeed:
    """
    Polls api-football.com for live match events.
    
    Methods:
        poll()              → List[FeedEvent]
        has_live_matches()  → bool
    """
    
    def __init__(self, api_key: str, leagues: Optional[Dict[int, str]] = None):
        self.api_key = api_key
        self.leagues = leagues or TOP_LEAGUES
        self._matches: Dict[int, MatchState] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._request_count = 0
        self._last_fixtures_fetch = 0.0
        self._fixtures_cache_ttl = 300  # 5 min

        # ── Rate-limit recovery ──
        self._rate_limited_until = 0.0       # Timestamp when backoff expires (0 = not limited)
        self._daily_budget = 90              # Max requests/day (leave 10 headroom from 100)
        self._daily_requests = 0             # Requests made today
        self._budget_reset_date = self._today_utc()  # Reset counter at midnight UTC
        self._backoff_duration = 1800        # 30 min backoff on rate limit (was infinite)
        self._consecutive_429s = 0           # Track repeated rate limits
        self._first_poll_done = False        # First poll: seed seen_events, don't emit
    
    @property
    def headers(self) -> Dict:
        return {
            "x-apisports-key": self.api_key,
            "x-rapidapi-host": "v3.football.api-sports.io",
        }
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self.headers)
        return self._session
    
    @staticmethod
    def _today_utc() -> str:
        return datetime.datetime.utcnow().strftime("%Y-%m-%d")

    def _check_budget(self) -> bool:
        """Check if we have API budget remaining. Resets at midnight UTC."""
        today = self._today_utc()
        if today != self._budget_reset_date:
            logger.info(f"Daily budget reset: {self._daily_requests} requests used yesterday")
            self._daily_requests = 0
            self._budget_reset_date = today
            self._consecutive_429s = 0  # Fresh day, reset backoff tracking
        return self._daily_requests < self._daily_budget

    def _apply_rate_limit_backoff(self, reason: str):
        """Apply progressive backoff instead of permanent disable."""
        self._consecutive_429s += 1
        # Progressive backoff: 30min, 1h, 2h, max 4h
        backoff = min(self._backoff_duration * (2 ** (self._consecutive_429s - 1)), 14400)
        self._rate_limited_until = time.time() + backoff
        remaining = self._daily_budget - self._daily_requests
        logger.warning(
            f"Football feed rate-limited ({reason}) — backing off {backoff/60:.0f}min. "
            f"Budget: {self._daily_requests}/{self._daily_budget} used, "
            f"retries: {self._consecutive_429s}"
        )

    async def _api_get(self, endpoint: str, params: Dict = None) -> Optional[Dict]:
        """Make API request with smart rate-limit recovery."""
        now = time.time()

        # Check backoff timer (recoverable, not permanent)
        if now < self._rate_limited_until:
            remaining = (self._rate_limited_until - now) / 60
            logger.debug(f"Football feed in backoff — {remaining:.0f}min remaining")
            return None

        # Check daily budget
        if not self._check_budget():
            logger.debug(f"Football feed daily budget exhausted ({self._daily_requests}/{self._daily_budget})")
            return None

        session = await self._get_session()
        url = f"{API_BASE}/{endpoint}"
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                self._request_count += 1
                self._daily_requests += 1

                if resp.status == 200:
                    data = await resp.json()
                    if data.get("errors"):
                        err = data['errors']
                        err_str = str(err).lower()
                        if 'ratelimit' in err_str or 'rate limit' in err_str or 'too many' in err_str or 'requests' in err_str:
                            self._apply_rate_limit_backoff("API error response")
                        else:
                            logger.warning(f"API error: {err}")
                        return None
                    # Success — reset consecutive 429 counter
                    self._consecutive_429s = 0
                    return data
                elif resp.status == 429:
                    self._apply_rate_limit_backoff("HTTP 429")
                    return None
                else:
                    logger.warning(f"API returned {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"API request failed: {e}")
            return None
    
    def has_live_matches(self) -> bool:
        """Are there any currently live matches?"""
        live_statuses = {"1H", "2H", "ET", "P", "BT", "LIVE"}
        return any(m.status in live_statuses for m in self._matches.values())
    
    async def poll(self) -> List[FeedEvent]:
        """
        Poll for new events. Returns list of FeedEvent objects.
        
        Strategy:
        1. Fetch live fixtures (cached 5 min when no live matches)
        2. For each live match, fetch events
        3. Diff against last known state
        4. Emit new events
        """
        events: List[FeedEvent] = []
        now = time.time()
        
        # Refresh fixtures periodically
        if now - self._last_fixtures_fetch > self._fixtures_cache_ttl:
            await self._refresh_fixtures()
            self._last_fixtures_fetch = now
        
        # Poll events for each live match (budget-aware: max 4 per cycle, 2s gap)
        polled = 0
        max_per_cycle = 4  # Conservative: save budget for fixtures refresh
        for fid, match in list(self._matches.items()):
            if match.status in ("FT", "AET", "PEN", "CANC", "PST", "ABD"):
                continue

            if polled >= max_per_cycle:
                break

            # Skip if budget is low
            if not self._check_budget():
                break

            new_events = await self._poll_match_events(match)
            events.extend(new_events)
            polled += 1

            # Rate limit: 2 seconds between API calls (was 1s)
            if polled < max_per_cycle:
                await asyncio.sleep(2.0)
            
            # Also check status transitions (no API call)
            status_events = await self._check_status_change(match)
            events.extend(status_events)
        
        # Mark first poll done — future polls will emit events
        if not self._first_poll_done and polled > 0:
            self._first_poll_done = True
            logger.info(f"First poll: seeded {sum(len(m.seen_event_ids) for m in self._matches.values())} existing events (suppressed)")

        # Clean up finished matches (keep for 30 min)
        finished = [fid for fid, m in self._matches.items()
                    if m.status in ("FT", "AET", "PEN") and now - m.started_at > 1800]
        for fid in finished:
            del self._matches[fid]

        return events
    
    async def _refresh_fixtures(self):
        """Fetch currently live fixtures."""
        data = await self._api_get("fixtures", {"live": "all"})
        if not data or not data.get("response"):
            return
        
        for fix in data["response"]:
            fid = fix["fixture"]["id"]
            status_short = fix["fixture"]["status"]["short"]
            
            home = fix["teams"]["home"]["name"]
            away = fix["teams"]["away"]["name"]
            league_name = fix["league"]["name"]
            league_id = fix["league"]["id"]
            
            # Only track leagues we care about
            if league_id not in self.leagues and len(self._matches) > 20:
                continue
            
            if fid not in self._matches:
                self._matches[fid] = MatchState(
                    fixture_id=fid,
                    home_team=home,
                    away_team=away,
                    league=league_name,
                    league_id=league_id,
                    status=status_short,
                    home_score=fix["goals"]["home"] or 0,
                    away_score=fix["goals"]["away"] or 0,
                    elapsed=fix["fixture"]["status"]["elapsed"] or 0,
                    started_at=time.time(),
                )
                logger.info(f"Tracking: {home} vs {away} ({league_name}) [{status_short}]")
            else:
                m = self._matches[fid]
                m.status = status_short
                m.home_score = fix["goals"]["home"] or 0
                m.away_score = fix["goals"]["away"] or 0
                m.elapsed = fix["fixture"]["status"]["elapsed"] or 0
    
    async def _poll_match_events(self, match: MatchState) -> List[FeedEvent]:
        """Get new events for a specific match."""
        data = await self._api_get("fixtures/events", {"fixture": match.fixture_id})
        if not data or not data.get("response"):
            return []

        events = []
        all_events = data["response"]

        for ev in all_events:
            # Create unique ID for dedup
            ev_id = f"{match.fixture_id}_{ev['time']['elapsed']}_{ev['type']}_{ev['player']['id']}"

            if ev_id in match.seen_event_ids:
                continue
            match.seen_event_ids.add(ev_id)

            # First poll: seed dedup set but don't emit (avoids stale events on restart)
            if not self._first_poll_done:
                continue
            
            # Map API event to our type
            event_type = API_EVENT_MAP.get(ev["type"])
            if event_type is None:
                # Check detail field
                event_type = API_EVENT_MAP.get(ev.get("detail", ""))
            if event_type is None:
                continue
            
            # Build keywords for market matching
            keywords = self._build_keywords(match, ev)
            
            feed_event = FeedEvent(
                event_type=event_type,
                source="football",
                timestamp=time.time(),
                received_at=time.time(),
                data={
                    "fixture_id": match.fixture_id,
                    "home_team": match.home_team,
                    "away_team": match.away_team,
                    "league": match.league,
                    "league_id": match.league_id,
                    "score": f"{match.home_score}-{match.away_score}",
                    "home_score": match.home_score,
                    "away_score": match.away_score,
                    "minute": ev["time"]["elapsed"],
                    "elapsed": ev["time"]["elapsed"],
                    "player": ev["player"]["name"],
                    "team": ev["team"]["name"],
                    "detail": ev.get("detail", ""),
                },
                keywords=keywords,
                confidence=0.95,
            )
            events.append(feed_event)
            
            logger.info(
                f"⚽ {event_type.value}: {ev['team']['name']} — "
                f"{ev['player']['name']} ({ev['time']['elapsed']}') "
                f"[{match.home_team} {match.home_score}-{match.away_score} {match.away_team}]"
            )
        
        match.last_event_count = len(all_events)
        return events
    
    async def _check_status_change(self, match: MatchState) -> List[FeedEvent]:
        """Check for halftime, full time, etc."""
        events = []
        
        # We track status transitions via the status field
        # This is called after _refresh_fixtures updates status
        
        if match.status == "HT":
            ev_id = f"{match.fixture_id}_HT"
            if ev_id not in match.seen_event_ids:
                match.seen_event_ids.add(ev_id)
                keywords = self._build_match_keywords(match)
                events.append(FeedEvent(
                    event_type=EventType.HALF_TIME,
                    source="football",
                    timestamp=time.time(),
                    received_at=time.time(),
                    data={
                        "fixture_id": match.fixture_id,
                        "home_team": match.home_team,
                        "away_team": match.away_team,
                        "league": match.league,
                        "home_score": match.home_score,
                        "away_score": match.away_score,
                        "score": f"{match.home_score}-{match.away_score}",
                    },
                    keywords=keywords,
                    confidence=0.95,
                ))
                logger.info(f"⏸️ HT: {match.home_team} {match.home_score}-{match.away_score} {match.away_team}")
        
        elif match.status in ("FT", "AET", "PEN"):
            ev_id = f"{match.fixture_id}_FT"
            if ev_id not in match.seen_event_ids:
                match.seen_event_ids.add(ev_id)
                keywords = self._build_match_keywords(match)
                
                # Determine winner
                if match.home_score > match.away_score:
                    winner = match.home_team
                elif match.away_score > match.home_score:
                    winner = match.away_team
                else:
                    winner = "draw"
                
                events.append(FeedEvent(
                    event_type=EventType.MATCH_END,
                    source="football",
                    timestamp=time.time(),
                    received_at=time.time(),
                    data={
                        "fixture_id": match.fixture_id,
                        "home_team": match.home_team,
                        "away_team": match.away_team,
                        "league": match.league,
                        "home_score": match.home_score,
                        "away_score": match.away_score,
                        "score": f"{match.home_score}-{match.away_score}",
                        "winner": winner,
                    },
                    keywords=keywords + [winner.lower()] if winner != "draw" else keywords + ["draw"],
                    confidence=0.99,
                ))
                logger.info(
                    f"🏁 FT: {match.home_team} {match.home_score}-{match.away_score} "
                    f"{match.away_team} → {winner}"
                )
        
        return events
    
    def _build_keywords(self, match: MatchState, event: Dict) -> List[str]:
        """Build keyword list for market matching."""
        kw = self._build_match_keywords(match)
        # Add player name
        player = event.get("player", {}).get("name", "")
        if player:
            kw.extend(player.lower().split())
        return kw
    
    def _build_match_keywords(self, match: MatchState) -> List[str]:
        """Base keywords for a match."""
        keywords = []
        # Team names (full + parts)
        for team in [match.home_team, match.away_team]:
            keywords.append(team.lower())
            keywords.extend(team.lower().split())
        # League
        keywords.append(match.league.lower())
        keywords.extend(match.league.lower().split())
        return list(set(keywords))
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
