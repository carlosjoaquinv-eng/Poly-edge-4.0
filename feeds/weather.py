"""
PolyEdge v4 — Weather Feed
Polls OpenWeatherMap API for extreme weather events relevant to Polymarket.

Emits FeedEvents:
  CUSTOM (subtype: HEAT_RECORD, COLD_RECORD, HURRICANE, STORM)

Polymarket has weather markets like:
  - "Will temperature in [city] exceed X°F?"
  - "Will 2026 be the hottest year on record?"
  - "Category 4+ hurricane in Atlantic this season?"

API: https://api.openweathermap.org/data/2.5
Free tier: 60 calls/min, 1000 calls/day — plenty for our use.

Strategy:
  - Poll key cities every 30 min for temperature records
  - Check severe weather alerts for hurricane/storm markets
  - Emit events when thresholds are crossed
"""

import asyncio
import aiohttp
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from enum import Enum

logger = logging.getLogger("polyedge.feeds.weather")

try:
    from engines.resolution_sniper_v2 import FeedEvent, EventType
except ImportError:
    class EventType(Enum):
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


# ─── Configuration ───────────────────────────────────────────

API_BASE = "https://api.openweathermap.org/data/2.5"

# Cities to monitor — chosen for Polymarket weather markets
MONITORED_CITIES = {
    "New York": {"lat": 40.71, "lon": -74.01, "keywords": ["new york", "nyc", "manhattan"]},
    "Los Angeles": {"lat": 34.05, "lon": -118.24, "keywords": ["los angeles", "la", "california"]},
    "Phoenix": {"lat": 33.45, "lon": -112.07, "keywords": ["phoenix", "arizona"]},
    "Miami": {"lat": 25.76, "lon": -80.19, "keywords": ["miami", "florida"]},
    "Chicago": {"lat": 41.88, "lon": -87.63, "keywords": ["chicago", "illinois"]},
    "Houston": {"lat": 29.76, "lon": -95.37, "keywords": ["houston", "texas"]},
    "London": {"lat": 51.51, "lon": -0.13, "keywords": ["london", "uk", "england"]},
    "Tokyo": {"lat": 35.68, "lon": 139.69, "keywords": ["tokyo", "japan"]},
    "Delhi": {"lat": 28.61, "lon": 77.23, "keywords": ["delhi", "india"]},
}

# Temperature thresholds (Fahrenheit) that trigger events
HEAT_THRESHOLDS_F = {
    "New York": 100, "Los Angeles": 110, "Phoenix": 120,
    "Miami": 100, "Chicago": 100, "Houston": 105,
    "London": 95, "Tokyo": 100, "Delhi": 115,
}

COLD_THRESHOLDS_F = {
    "New York": 0, "Los Angeles": 32, "Phoenix": 32,
    "Miami": 40, "Chicago": -10, "Houston": 20,
    "London": 15, "Tokyo": 25, "Delhi": 40,
}


class WeatherFeed:
    """
    Polls OpenWeatherMap for extreme weather events.

    Methods:
        poll()  → List[FeedEvent]
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_temps: Dict[str, float] = {}  # city → last temp_f
        self._seen_events: set = set()
        self._request_count = 0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def poll(self) -> List[FeedEvent]:
        """Poll all monitored cities for weather events."""
        events: List[FeedEvent] = []

        for city, info in MONITORED_CITIES.items():
            try:
                weather = await self._fetch_weather(info["lat"], info["lon"])
                if not weather:
                    continue

                temp_f = weather.get("temp_f", 0)
                self._last_temps[city] = temp_f

                # Check heat threshold
                heat_thresh = HEAT_THRESHOLDS_F.get(city, 120)
                if temp_f >= heat_thresh:
                    ev_id = f"heat_{city}_{int(temp_f)}"
                    if ev_id not in self._seen_events:
                        self._seen_events.add(ev_id)
                        keywords = info["keywords"] + ["temperature", "heat", "record", "hot", "weather"]
                        events.append(FeedEvent(
                            event_type=EventType.CUSTOM,
                            source="weather",
                            timestamp=time.time(),
                            received_at=time.time(),
                            data={
                                "subtype": "HEAT_RECORD",
                                "city": city,
                                "temp_f": temp_f,
                                "temp_c": round((temp_f - 32) * 5/9, 1),
                                "threshold_f": heat_thresh,
                                "description": weather.get("description", ""),
                            },
                            keywords=keywords,
                            confidence=0.95,
                        ))
                        logger.info(f"🌡️ HEAT: {city} at {temp_f:.0f}°F (threshold {heat_thresh}°F)")

                # Check cold threshold
                cold_thresh = COLD_THRESHOLDS_F.get(city, 0)
                if temp_f <= cold_thresh:
                    ev_id = f"cold_{city}_{int(temp_f)}"
                    if ev_id not in self._seen_events:
                        self._seen_events.add(ev_id)
                        keywords = info["keywords"] + ["temperature", "cold", "freeze", "record", "weather"]
                        events.append(FeedEvent(
                            event_type=EventType.CUSTOM,
                            source="weather",
                            timestamp=time.time(),
                            received_at=time.time(),
                            data={
                                "subtype": "COLD_RECORD",
                                "city": city,
                                "temp_f": temp_f,
                                "temp_c": round((temp_f - 32) * 5/9, 1),
                                "threshold_f": cold_thresh,
                                "description": weather.get("description", ""),
                            },
                            keywords=keywords,
                            confidence=0.90,
                        ))
                        logger.info(f"🥶 COLD: {city} at {temp_f:.0f}°F (threshold {cold_thresh}°F)")

                # Check for severe weather (storms, hurricanes)
                weather_id = weather.get("weather_id", 800)
                if weather_id < 300:  # Thunderstorm group (200-299)
                    ev_id = f"storm_{city}_{weather_id}"
                    if ev_id not in self._seen_events:
                        self._seen_events.add(ev_id)
                        keywords = info["keywords"] + ["storm", "hurricane", "severe", "weather"]
                        events.append(FeedEvent(
                            event_type=EventType.CUSTOM,
                            source="weather",
                            timestamp=time.time(),
                            received_at=time.time(),
                            data={
                                "subtype": "STORM",
                                "city": city,
                                "weather_id": weather_id,
                                "description": weather.get("description", ""),
                                "wind_mph": weather.get("wind_mph", 0),
                            },
                            keywords=keywords,
                            confidence=0.85,
                        ))
                        logger.info(f"⛈️ STORM: {city} — {weather.get('description', '')} (wind {weather.get('wind_mph', 0):.0f} mph)")

                # Small delay between API calls
                await asyncio.sleep(0.5)

            except Exception as e:
                logger.error(f"Weather poll error for {city}: {e}")

        # Trim seen events (keep last 500)
        if len(self._seen_events) > 1000:
            self._seen_events = set(list(self._seen_events)[-500:])

        return events

    async def _fetch_weather(self, lat: float, lon: float) -> Optional[Dict]:
        """Fetch current weather for a location."""
        session = await self._get_session()
        params = {
            "lat": lat,
            "lon": lon,
            "appid": self.api_key,
            "units": "imperial",  # Fahrenheit
        }
        try:
            async with session.get(f"{API_BASE}/weather", params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                self._request_count += 1
                if resp.status == 200:
                    data = await resp.json()
                    main = data.get("main", {})
                    wind = data.get("wind", {})
                    weather_list = data.get("weather", [{}])
                    return {
                        "temp_f": main.get("temp", 0),
                        "feels_like_f": main.get("feels_like", 0),
                        "humidity": main.get("humidity", 0),
                        "wind_mph": wind.get("speed", 0),
                        "description": weather_list[0].get("description", "") if weather_list else "",
                        "weather_id": weather_list[0].get("id", 800) if weather_list else 800,
                    }
                elif resp.status == 401:
                    logger.error("OpenWeatherMap API key invalid — check WEATHER_API_KEY")
                    return None
                else:
                    logger.warning(f"Weather API returned {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"Weather API request failed: {e}")
            return None

    def get_temps(self) -> Dict[str, float]:
        """Get last known temperatures for all cities."""
        return dict(self._last_temps)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
