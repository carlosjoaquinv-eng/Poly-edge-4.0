"""
PolyEdge v4 — Multi-Sport Feed
================================
Polls api-sports.io for live events across multiple sports.
Uses the SAME API key as football feed (api-sports.io family).

Sports:
  - Basketball (NBA): v1.basketball.api-sports.io
  - Hockey (NHL): v1.hockey.api-sports.io

Budget: Shared 100 req/day with football feed.
  - Football uses ~30/day when live
  - Basketball + Hockey: ~20/day each
  - Total: ~70/day, leaving 30 headroom

Strategy:
  - Poll live games every 5 min (not 15s like football — budget conservation)
  - Only track top leagues (NBA, NHL)
  - Emit events: SCORE_CHANGE, GAME_END, PERIOD_END
  - First poll suppression (no stale events on restart)
"""

import asyncio
import aiohttp
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

logger = logging.getLogger("polyedge.feeds.sports")

try:
    from engines.resolution_sniper_v2 import FeedEvent, EventType
except ImportError:
    from enum import Enum
    class EventType(Enum):
        CUSTOM = "CUSTOM"
        MATCH_END = "MATCH_END"

    @dataclass
    class FeedEvent:
        event_type: EventType
        source: str
        timestamp: float
        received_at: float
        data: Dict
        keywords: List[str]
        confidence: float


# ─── Sport Configs ───────────────────────────────────────

SPORTS = {
    "basketball": {
        "api_base": "https://v1.basketball.api-sports.io",
        "name": "Basketball",
        "top_leagues": {
            12: "NBA",           # National Basketball Association
            116: "EuroLeague",   # EuroLeague
            117: "ACB",          # Spanish ACB
        },
        "source": "basketball",
    },
    "hockey": {
        "api_base": "https://v1.hockey.api-sports.io",
        "name": "Hockey",
        "top_leagues": {
            57: "NHL",           # National Hockey League
            55: "KHL",           # Kontinental Hockey League
        },
        "source": "hockey",
    },
}


@dataclass
class GameState:
    """Tracks a live game."""
    game_id: int
    sport: str
    home_team: str
    away_team: str
    league: str
    league_id: int
    status: str = ""
    home_score: int = 0
    away_score: int = 0
    period: str = ""
    last_score_hash: str = ""
    seen_events: Set[str] = field(default_factory=set)
    started_at: float = 0.0


class MultiSportFeed:
    """
    Polls api-sports.io for live basketball and hockey events.

    Methods:
        poll() → List[FeedEvent]
    """

    def __init__(self, api_key: str, sports: List[str] = None):
        self.api_key = api_key
        self.sports = sports or ["basketball", "hockey"]
        self._games: Dict[str, GameState] = {}  # "sport_gameId" → GameState
        self._session: Optional[aiohttp.ClientSession] = None
        self._daily_requests = 0
        self._daily_budget = 40  # Share budget: 40 for multi-sport, 50 for football
        self._budget_date = time.strftime("%Y-%m-%d")
        self._first_poll_done: Dict[str, bool] = {}  # per sport
        self._rate_limited_until = 0.0
        self._last_poll: Dict[str, float] = {}  # per sport → last poll time

    @property
    def headers(self) -> Dict:
        return {
            "x-apisports-key": self.api_key,
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self.headers)
        return self._session

    def _check_budget(self) -> bool:
        today = time.strftime("%Y-%m-%d")
        if today != self._budget_date:
            logger.info(f"Multi-sport budget reset: {self._daily_requests} used yesterday")
            self._daily_requests = 0
            self._budget_date = today
        return self._daily_requests < self._daily_budget

    async def poll(self) -> List[FeedEvent]:
        """Poll all configured sports for live events."""
        events: List[FeedEvent] = []
        now = time.time()

        # Check rate limit backoff
        if now < self._rate_limited_until:
            return events

        for sport_key in self.sports:
            sport_cfg = SPORTS.get(sport_key)
            if not sport_cfg:
                continue

            # Poll each sport every 5 minutes
            last = self._last_poll.get(sport_key, 0)
            if now - last < 300:  # 5 min interval
                continue

            if not self._check_budget():
                logger.debug("Multi-sport daily budget exhausted")
                break

            new_events = await self._poll_sport(sport_key, sport_cfg)
            events.extend(new_events)
            self._last_poll[sport_key] = now

            await asyncio.sleep(1)  # Gap between sports

        return events

    async def _poll_sport(self, sport_key: str, cfg: Dict) -> List[FeedEvent]:
        """Poll one sport for live games."""
        events: List[FeedEvent] = []
        api_base = cfg["api_base"]
        source = cfg["source"]

        # Fetch live games
        session = await self._get_session()
        try:
            url = f"{api_base}/games"
            params = {"live": "all"} if sport_key == "basketball" else {"live": "all"}

            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                self._daily_requests += 1

                if resp.status == 429:
                    self._rate_limited_until = time.time() + 1800
                    logger.warning(f"{cfg['name']} rate limited — backing off 30min")
                    return events

                if resp.status != 200:
                    logger.warning(f"{cfg['name']} API returned {resp.status}")
                    return events

                data = await resp.json()
                if data.get("errors"):
                    err_str = str(data["errors"]).lower()
                    if "ratelimit" in err_str or "rate limit" in err_str:
                        self._rate_limited_until = time.time() + 1800
                        logger.warning(f"{cfg['name']} rate limited — backing off 30min")
                    return events

        except Exception as e:
            logger.error(f"{cfg['name']} API error: {e}")
            return events

        games = data.get("response", [])
        first_poll = sport_key not in self._first_poll_done

        for game in games:
            game_id = game.get("id", 0)
            if not game_id:
                continue

            # Parse based on sport
            if sport_key == "basketball":
                home = game.get("teams", {}).get("home", {}).get("name", "?")
                away = game.get("teams", {}).get("away", {}).get("name", "?")
                league_id = game.get("league", {}).get("id", 0)
                league_name = game.get("league", {}).get("name", "?")
                home_score = game.get("scores", {}).get("home", {}).get("total", 0) or 0
                away_score = game.get("scores", {}).get("away", {}).get("total", 0) or 0
                status = game.get("status", {}).get("short", "")
                period = str(game.get("status", {}).get("timer", ""))
            elif sport_key == "hockey":
                home = game.get("teams", {}).get("home", {}).get("name", "?")
                away = game.get("teams", {}).get("away", {}).get("name", "?")
                league_id = game.get("league", {}).get("id", 0)
                league_name = game.get("league", {}).get("name", "?")
                home_score = game.get("scores", {}).get("home", 0) or 0
                away_score = game.get("scores", {}).get("away", 0) or 0
                status = game.get("status", {}).get("short", "")
                period = str(game.get("periods", {}).get("current", ""))
            else:
                continue

            # Only track top leagues
            if league_id not in cfg["top_leagues"]:
                continue

            key = f"{sport_key}_{game_id}"
            score_hash = f"{home_score}-{away_score}"

            if key not in self._games:
                self._games[key] = GameState(
                    game_id=game_id,
                    sport=sport_key,
                    home_team=home,
                    away_team=away,
                    league=league_name,
                    league_id=league_id,
                    status=status,
                    home_score=home_score,
                    away_score=away_score,
                    period=period,
                    last_score_hash=score_hash,
                    started_at=time.time(),
                )
                if not first_poll:
                    logger.info(f"🏀 Tracking: {home} vs {away} ({league_name}) [{status}]")
            else:
                gs = self._games[key]
                old_score = gs.last_score_hash
                old_status = gs.status

                gs.home_score = home_score
                gs.away_score = away_score
                gs.status = status
                gs.period = period
                gs.last_score_hash = score_hash

                # Don't emit on first poll
                if first_poll:
                    continue

                # SCORE CHANGE event
                if score_hash != old_score:
                    ev_id = f"{key}_score_{score_hash}"
                    if ev_id not in gs.seen_events:
                        gs.seen_events.add(ev_id)
                        keywords = self._build_keywords(gs)
                        events.append(FeedEvent(
                            event_type=EventType.CUSTOM,
                            source=source,
                            timestamp=time.time(),
                            received_at=time.time(),
                            data={
                                "subtype": "SCORE_CHANGE",
                                "sport": sport_key,
                                "game_id": game_id,
                                "home_team": home,
                                "away_team": away,
                                "league": league_name,
                                "score": score_hash,
                                "home_score": home_score,
                                "away_score": away_score,
                                "period": period,
                                "status": status,
                            },
                            keywords=keywords,
                            confidence=0.95,
                        ))
                        emoji = "🏀" if sport_key == "basketball" else "🏒"
                        logger.info(
                            f"{emoji} SCORE: {home} {home_score}-{away_score} {away} "
                            f"({league_name}) [{period}]"
                        )

                # GAME END event
                if status in ("FT", "AOT", "AP") and old_status not in ("FT", "AOT", "AP"):
                    ev_id = f"{key}_end"
                    if ev_id not in gs.seen_events:
                        gs.seen_events.add(ev_id)
                        winner = home if home_score > away_score else away if away_score > home_score else "draw"
                        keywords = self._build_keywords(gs) + [winner.lower()]
                        events.append(FeedEvent(
                            event_type=EventType.MATCH_END,
                            source=source,
                            timestamp=time.time(),
                            received_at=time.time(),
                            data={
                                "subtype": "GAME_END",
                                "sport": sport_key,
                                "game_id": game_id,
                                "home_team": home,
                                "away_team": away,
                                "league": league_name,
                                "score": score_hash,
                                "winner": winner,
                            },
                            keywords=keywords,
                            confidence=0.99,
                        ))
                        emoji = "🏀" if sport_key == "basketball" else "🏒"
                        logger.info(
                            f"{emoji} FINAL: {home} {home_score}-{away_score} {away} → {winner}"
                        )

        if first_poll:
            self._first_poll_done[sport_key] = True
            logger.info(f"{cfg['name']}: first poll — seeded {len([g for g in self._games.values() if g.sport == sport_key])} games (suppressed)")

        # Cleanup finished games (>1h old)
        now = time.time()
        finished = [k for k, g in self._games.items()
                    if g.sport == sport_key and g.status in ("FT", "AOT", "AP", "CANC")
                    and now - g.started_at > 3600]
        for k in finished:
            del self._games[k]

        return events

    def _build_keywords(self, gs: GameState) -> List[str]:
        """Build keywords for market matching."""
        keywords = []
        for team in [gs.home_team, gs.away_team]:
            keywords.append(team.lower())
            keywords.extend(team.lower().split())
        keywords.append(gs.league.lower())
        keywords.extend(gs.league.lower().split())
        keywords.append(gs.sport)
        # Add sport-specific keywords
        if gs.sport == "basketball":
            keywords.extend(["nba", "basketball", "finals", "playoffs"])
        elif gs.sport == "hockey":
            keywords.extend(["nhl", "hockey", "stanley cup", "playoffs"])
        return list(set(keywords))

    def has_live_games(self) -> bool:
        live_statuses = {"Q1", "Q2", "Q3", "Q4", "OT", "HT", "BT",
                         "P1", "P2", "P3", "OT", "LIVE", "1H", "2H"}
        return any(g.status in live_statuses for g in self._games.values())

    def get_stats(self) -> Dict:
        return {
            "games_tracked": len(self._games),
            "daily_requests": self._daily_requests,
            "daily_budget": self._daily_budget,
            "sports": list(self._first_poll_done.keys()),
            "live_games": sum(1 for g in self._games.values() if g.status not in ("FT", "AOT", "AP", "CANC")),
        }

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
