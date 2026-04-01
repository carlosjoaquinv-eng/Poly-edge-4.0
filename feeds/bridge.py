"""
PolyEdge v4 — Feed Bridge
===========================
Bridges existing feeds (football.py, crypto.py) to Engine 2.
Polls feeds and converts their events to FeedEvent objects
that the Resolution Sniper can consume.
Extracted from main_v4.py.
"""

import asyncio
import time
import logging

from engines.resolution_sniper_v2 import ResolutionSniperV2, FeedEvent


class FeedBridge:
    """
    Bridges existing feeds (football.py, crypto.py) to Engine 2.

    Polls feeds and converts their events to FeedEvent objects
    that the Resolution Sniper can consume.

    In production, this imports from feeds/ directory.
    """

    def __init__(self, config, sniper: ResolutionSniperV2):
        self.config = config
        self.sniper = sniper
        self.logger = logging.getLogger("polyedge.feeds")
        self._running = False

        # Feed instances (lazy-loaded)
        self._football = None
        self._crypto = None
        self._weather = None
        self._sports = None

    async def start(self):
        self._running = True
        self.logger.info("Feed bridge starting...")

        tasks = []

        # Start football feed if API key is available
        if self.config.FOOTBALL_API_KEY:
            tasks.append(self._football_loop())
            self.logger.info("Football feed enabled")
        else:
            self.logger.info("Football feed disabled (no API key)")

        # Crypto feed always runs (no API key needed)
        tasks.append(self._crypto_loop())

        # Multi-sport feed (NBA/NHL) — uses same football API key
        if self.config.FOOTBALL_API_KEY:
            tasks.append(self._sports_loop())
            self.logger.info("Multi-sport feed enabled (NBA/NHL)")

        # Weather feed if API key available
        if getattr(self.config, "WEATHER_API_KEY", ""):
            tasks.append(self._weather_loop())
            self.logger.info("Weather feed enabled")
        self.logger.info(f"Crypto feed enabled: {self.config.CRYPTO_SYMBOLS}")

        if tasks:
            await asyncio.gather(*tasks)

    async def stop(self):
        self._running = False

    async def _football_loop(self):
        """Poll football feed for live match events."""
        try:
            # Try importing existing feed
            from feeds.football import FootballFeed
            self._football = FootballFeed(self.config.FOOTBALL_API_KEY)
            self.logger.info("Football feed loaded from feeds/football.py")
        except ImportError:
            self.logger.warning("feeds/football.py not found — using stub")
            return

        while self._running:
            try:
                events = await self._football.poll()
                for event in events:
                    # Convert to v4 FeedEvent format if needed
                    if hasattr(event, 'event_type'):
                        await self.sniper.on_event(event)
                    else:
                        # Adapt from v3.1 format
                        from engines.resolution_sniper_v2 import EventType as ET
                        fe = FeedEvent(
                            event_type=ET(event.get("type", "CUSTOM")),
                            source="football",
                            timestamp=event.get("timestamp", time.time()),
                            received_at=time.time(),
                            data=event.get("data", {}),
                            keywords=event.get("keywords", []),
                            confidence=event.get("confidence", 0.9),
                        )
                        await self.sniper.on_event(fe)

                interval = self.config.FOOTBALL_POLL_LIVE if self._football.has_live_matches() \
                    else self.config.FOOTBALL_POLL_IDLE
                await asyncio.sleep(interval)

            except Exception as e:
                self.logger.error(f"Football feed error: {e}")
                await asyncio.sleep(30)

    async def _crypto_loop(self):
        """Poll crypto feed for price events."""
        try:
            from feeds.crypto import CryptoFeed
            self._crypto = CryptoFeed(self.config.CRYPTO_SYMBOLS)
            self.logger.info("Crypto feed loaded from feeds/crypto.py")
        except ImportError:
            self.logger.warning("feeds/crypto.py not found — using stub")
            # Stub: just poll Binance directly
            await self._crypto_stub_loop()
            return

        while self._running:
            try:
                events = await self._crypto.poll()
                for event in events:
                    if hasattr(event, 'event_type'):
                        await self.sniper.on_event(event)

                await asyncio.sleep(self.config.CRYPTO_POLL_INTERVAL)

            except Exception as e:
                self.logger.error(f"Crypto feed error: {e}")
                await asyncio.sleep(10)

    async def _crypto_stub_loop(self):
        """Minimal crypto feed when feeds/crypto.py isn't available."""
        import aiohttp
        from engines.resolution_sniper_v2 import EventType as ET

        prices: dict = {}

        while self._running:
            try:
                async with aiohttp.ClientSession() as session:
                    for symbol in self.config.CRYPTO_SYMBOLS:
                        async with session.get(
                            f"https://api.binance.us/api/v3/ticker/price",
                            params={"symbol": symbol},
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                price = float(data["price"])

                                old_price = prices.get(symbol)
                                prices[symbol] = price

                                if old_price:
                                    change_pct = (price - old_price) / old_price * 100
                                    if abs(change_pct) > 5:
                                        event_type = ET.PRICE_PUMP if change_pct > 0 else ET.PRICE_DUMP
                                        event = FeedEvent(
                                            event_type=event_type,
                                            source="crypto",
                                            timestamp=time.time(),
                                            received_at=time.time(),
                                            data={
                                                "symbol": symbol,
                                                "price": price,
                                                "change_pct": change_pct,
                                            },
                                            keywords=[symbol.lower(), symbol.replace("USDT","").lower()],
                                            confidence=0.95,
                                        )
                                        await self.sniper.on_event(event)

                await asyncio.sleep(self.config.CRYPTO_POLL_INTERVAL)

            except Exception as e:
                self.logger.error(f"Crypto stub error: {e}")
                await asyncio.sleep(10)


    async def _sports_loop(self):
        """Poll multi-sport feed for NBA/NHL events."""
        try:
            from feeds.sports import MultiSportFeed
            self._sports = MultiSportFeed(self.config.FOOTBALL_API_KEY, ["basketball", "hockey"])
            self.logger.info("Multi-sport feed loaded (NBA/NHL)")
        except ImportError as e:
            self.logger.warning(f"feeds/sports.py not found: {e}")
            return

        while self._running:
            try:
                events = await self._sports.poll()
                for event in events:
                    if hasattr(event, "event_type"):
                        await self.sniper.on_event(event)
                # Poll every 60s (games polled internally every 5min per sport)
                await asyncio.sleep(60)
            except Exception as e:
                self.logger.error(f"Multi-sport feed error: {e}")
                await asyncio.sleep(30)

    async def _weather_loop(self):
        """Poll weather feed for extreme weather events."""
        try:
            from feeds.weather import WeatherFeed
            self._weather = WeatherFeed(self.config.WEATHER_API_KEY)
            self.logger.info("Weather feed loaded from feeds/weather.py")
        except ImportError:
            self.logger.warning("feeds/weather.py not found")
            return

        while self._running:
            try:
                events = await self._weather.poll()
                for event in events:
                    if hasattr(event, "event_type"):
                        await self.sniper.on_event(event)
                # Poll every 30 min (weather changes slowly)
                await asyncio.sleep(1800)
            except Exception as e:
                self.logger.error(f"Weather feed error: {e}")
                await asyncio.sleep(60)
