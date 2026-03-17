"""
PolyEdge v4 — Main Entry Point
=================================
Runs all 3 engines + feeds concurrently:
  - Engine 1: Market Maker (spread capture)
  - Engine 2: Resolution Sniper v2 (event-driven)
  - Engine 3: Meta Strategist (LLM optimizer every 12h)
  - Football Feed (live match events)
  - Crypto Feed (price thresholds)
  - Dashboard API (stats + monitoring)

Usage:
  python main_v4.py              # Paper mode (default)
  PAPER_MODE=false python main_v4.py  # Live mode (requires funded wallet)

PM2:
  pm2 start main_v4.py --name polyedge-v4 --interpreter python3
"""

import asyncio
import signal
import sys
import os

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orchestrator import PolyEdgeV4


def main():
    """Entry point for PolyEdge v4."""
    app = PolyEdgeV4()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Handle SIGINT/SIGTERM
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(app.shutdown()))

    try:
        loop.run_until_complete(app.start())
    except KeyboardInterrupt:
        loop.run_until_complete(app.shutdown())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
