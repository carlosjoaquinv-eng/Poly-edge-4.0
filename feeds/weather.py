"""
PolyEdge v4 — Weather Feed (Open-Meteo)
=========================================
Polls Open-Meteo API for extreme weather events relevant to Polymarket.

NO API KEY NEEDED — Open-Meteo is free and unlimited.

Emits FeedEvents:
  CUSTOM (subtype: HEAT_RECORD, COLD_RECORD, STORM, HIGH_WIND)

Polymarket weather markets:
  - "Will temperature in [city] exceed X°F?"
  - "Will 2026 be the hottest year on record?"
  - "Category 4+ hurricane in Atlantic this season?"
  - "Snow in [city] before [date]?"

API: https://api.open-meteo.com/v1
Free tier: Unlimited, no API key required.

Strategy:
  - Poll key cities every 30 min for temperature extremes
  - Check wind speed for hurricane/storm markets
  - Track daily highs for record temperature markets
  - First poll establishes baselines (no events emitted)
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

API_BASE = "https://api.open-meteo.com/v1/forecast"

# Cities to monitor — chosen for Polymarket weather markets
MONITORED_CITIES = {
    "New York": {"lat": 40.71, "lon": -74.01, "keywords": ["new york", "nyc", "manhattan"]},
    "Los Angeles": {"lat": 34.05, "lon": -118.24, "keywords": ["los angeles", "la", "california"]},
    "Phoenix": {"lat": 33.45, "lon": -112.07, "keywords": ["phoenix", "arizona"]},
    "Miami": {"lat": 25.76, "lon": -80.19, "keywords": ["miami", "florida"]},
    "Chicago": {"lat": 41.88, "lon": -87.63, "keywords": ["chicago", "illinois"]},
    "Houston": {"lat": 29.76, "lon": -95.37, "keywords": ["houston", "texas"]},
    "Dallas": {"lat": 32.78, "lon": -96.80, "keywords": ["dallas", "texas"]},
    "Denver": {"lat": 39.74, "lon": -104.99, "keywords": ["denver", "colorado"]},
    "London": {"lat": 51.51, "lon": -0.13, "keywords": ["london", "uk", "england"]},
    "Tokyo": {"lat": 35.68, "lon": 139.69, "keywords": ["tokyo", "japan"]},
    "Delhi": {"lat": 28.61, "lon": 77.23, "keywords": ["delhi", "india"]},
    "Sao Paulo": {"lat": -23.55, "lon": -46.63, "keywords": ["sao paulo", "brazil"]},
}

# Temperature thresholds (Fahrenheit) that trigger events
HEAT_THRESHOLDS_F = {
    "New York": 95, "Los Angeles": 105, "Phoenix": 115,
    "Miami": 97, "Chicago": 95, "Houston": 100,
    "Dallas": 105, "Denver": 95,
    "London": 90, "Tokyo": 95, "Delhi": 110,
    "Sao Paulo": 95,
}

COLD_THRESHOLDS_F = {
    "New York": 5, "Los Angeles": 35, "Phoenix": 35,
    "Miami": 42, "Chicago": -5, "Houston": 25,
    "Dallas": 20, "Denver": -10,
    "London": 20, "Tokyo": 28, "Delhi": 42,
    "Sao Paulo": 50,
}

# Wind thresholds (mph) for storm alerts
WIND_THRESHOLDS_MPH = {
    "storm": 50,        # Tropical storm force
    "hurricane": 74,    # Category 1 hurricane
    "major": 111,       # Category 3+ (major hurricane)
}


class WeatherFeed:
    """
    Polls Open-Meteo for extreme weather events.
    No API key needed.

    Methods:
        poll()      -> List[FeedEvent]
        get_temps() -> Dict[str, float]
    """

    def __init__(self, api_key: str = ""):
        # api_key param kept for backward compatibility but not used
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_temps: Dict[str, float] = {}
        self._last_winds: Dict[str, float] = {}
        self._seen_events: set = set()
        self._request_count = 0
        self._first_poll_done = False

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
                wind_mph = weather.get("wind_mph", 0)
                self._last_temps[city] = temp_f
                self._last_winds[city] = wind_mph

                # Skip events on first poll (establish baseline)
                if not self._first_poll_done:
                    continue

                # ── HEAT CHECK ──
                heat_thresh = HEAT_THRESHOLDS_F.get(city, 115)
                if temp_f >= heat_thresh:
                    ev_id = f"heat_{city}_{int(temp_f)}"
                    if ev_id not in self._seen_events:
                        self._seen_events.add(ev_id)
                        keywords = info["keywords"] + [
                            "temperature", "heat", "record", "hot",
                            "weather", "exceed", "above", "high"
                        ]
                        events.append(FeedEvent(
                            event_type=EventType.CUSTOM,
                            source="weather",
                            timestamp=time.time(),
                            received_at=time.time(),
                            data={
                                "subtype": "HEAT_RECORD",
                                "city": city,
                                "temp_f": temp_f,
                                "temp_c": round((temp_f - 32) * 5 / 9, 1),
                                "threshold_f": heat_thresh,
                                "wind_mph": wind_mph,
                                "description": weather.get("description", ""),
                            },
                            keywords=keywords,
                            confidence=0.95,
                        ))
                        logger.info(
                            f"🌡️ HEAT: {city} at {temp_f:.0f}°F "
                            f"(threshold {heat_thresh}°F)"
                        )

                # ── COLD CHECK ──
                cold_thresh = COLD_THRESHOLDS_F.get(city, 0)
                if temp_f <= cold_thresh:
                    ev_id = f"cold_{city}_{int(temp_f)}"
                    if ev_id not in self._seen_events:
                        self._seen_events.add(ev_id)
                        keywords = info["keywords"] + [
                            "temperature", "cold", "freeze", "record",
                            "weather", "below", "snow", "winter"
                        ]
                        events.append(FeedEvent(
                            event_type=EventType.CUSTOM,
                            source="weather",
                            timestamp=time.time(),
                            received_at=time.time(),
                            data={
                                "subtype": "COLD_RECORD",
                                "city": city,
                                "temp_f": temp_f,
                                "temp_c": round((temp_f - 32) * 5 / 9, 1),
                                "threshold_f": cold_thresh,
                                "wind_mph": wind_mph,
                                "description": weather.get("description", ""),
                            },
                            keywords=keywords,
                            confidence=0.90,
                        ))
                        logger.info(
                            f"🥶 COLD: {city} at {temp_f:.0f}°F "
                            f"(threshold {cold_thresh}°F)"
                        )

                # ── WIND/STORM CHECK ──
                if wind_mph >= WIND_THRESHOLDS_MPH["storm"]:
                    severity = "MAJOR_HURRICANE" if wind_mph >= WIND_THRESHOLDS_MPH["major"] \
                        else "HURRICANE" if wind_mph >= WIND_THRESHOLDS_MPH["hurricane"] \
                        else "STORM"
                    ev_id = f"wind_{city}_{severity}_{int(wind_mph)}"
                    if ev_id not in self._seen_events:
                        self._seen_events.add(ev_id)
                        keywords = info["keywords"] + [
                            "storm", "hurricane", "wind", "severe",
                            "weather", "tropical", "category"
                        ]
                        events.append(FeedEvent(
                            event_type=EventType.CUSTOM,
                            source="weather",
                            timestamp=time.time(),
                            received_at=time.time(),
                            data={
                                "subtype": severity,
                                "city": city,
                                "wind_mph": wind_mph,
                                "temp_f": temp_f,
                                "description": weather.get("description", ""),
                            },
                            keywords=keywords,
                            confidence=0.92 if severity == "STORM" else 0.97,
                        ))
                        logger.info(
                            f"🌀 {severity}: {city} — "
                            f"wind {wind_mph:.0f} mph"
                        )

                await asyncio.sleep(0.3)

            except Exception as e:
                logger.error(f"Weather poll error for {city}: {e}")

        # Mark first poll done
        if not self._first_poll_done:
            self._first_poll_done = True
            logger.info(
                f"Weather feed baseline: {len(self._last_temps)} cities polled "
                f"(events suppressed on first poll)"
            )

        # Trim seen events
        if len(self._seen_events) > 1000:
            self._seen_events = set(list(self._seen_events)[-500:])

        return events

    async def _fetch_weather(self, lat: float, lon: float) -> Optional[Dict]:
        """Fetch current weather from Open-Meteo (no API key needed)."""
        session = await self._get_session()
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_gusts_10m,weather_code",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "timezone": "auto",
        }
        try:
            async with session.get(API_BASE, params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                self._request_count += 1
                if resp.status == 200:
                    data = await resp.json()
                    current = data.get("current", {})
                    temp_f = current.get("temperature_2m", 0)
                    wind_mph = current.get("wind_speed_10m", 0)
                    wind_gusts = current.get("wind_gusts_10m", 0)
                    humidity = current.get("relative_humidity_2m", 0)
                    wmo_code = current.get("weather_code", 0)

                    # WMO weather codes → description
                    description = self._wmo_description(wmo_code)

                    return {
                        "temp_f": temp_f,
                        "wind_mph": max(wind_mph, wind_gusts * 0.7),  # Use gusts if stronger
                        "humidity": humidity,
                        "weather_code": wmo_code,
                        "description": description,
                    }
                else:
                    logger.warning(f"Open-Meteo returned {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"Open-Meteo request failed: {e}")
            return None

    @staticmethod
    def _wmo_description(code: int) -> str:
        """Convert WMO weather code to human description."""
        descriptions = {
            0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
            45: "fog", 48: "depositing rime fog",
            51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
            61: "slight rain", 63: "moderate rain", 65: "heavy rain",
            71: "slight snow", 73: "moderate snow", 75: "heavy snow",
            77: "snow grains",
            80: "slight rain showers", 81: "moderate rain showers", 82: "violent rain showers",
            85: "slight snow showers", 86: "heavy snow showers",
            95: "thunderstorm", 96: "thunderstorm with slight hail",
            99: "thunderstorm with heavy hail",
        }
        return descriptions.get(code, f"code {code}")

    def get_temps(self) -> Dict[str, float]:
        """Get last known temperatures for all cities."""
        return dict(self._last_temps)

    def get_stats(self) -> Dict:
        return {
            "cities_monitored": len(MONITORED_CITIES),
            "cities_polled": len(self._last_temps),
            "requests_total": self._request_count,
            "first_poll_done": self._first_poll_done,
            "events_seen": len(self._seen_events),
        }

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
