# PolyEdge v4.0 — Polymarket Algorithmic Trading System

Multi-engine trading bot for [Polymarket](https://polymarket.com) prediction markets.

## Architecture

```
main_v4.py                    ← Orchestrator (runs everything)
config_v4.py                  ← Unified configuration
dashboard_v4.html             ← Live monitoring dashboard
engine/core/clob_client_v4.py ← CLOB client with EIP-712 signing
engines/
  market_maker.py             ← Engine 1: OU mean reversion + spread capture
  resolution_sniper_v2.py     ← Engine 2: Event-driven gap trading
  meta_strategist.py          ← Engine 3: Claude Haiku parameter optimizer
```

## Engines

| Engine | Strategy | Revenue Target |
|--------|----------|---------------|
| **Market Maker** | Two-sided quotes with OU fair value, inventory skew, stealth jitter | $200-270/mo |
| **Resolution Sniper** | Maps real-world events (goals, crypto thresholds) to market gaps | $150-200/mo |
| **Meta Strategist** | LLM reviews performance every 12h, adjusts parameters | Optimizer |

## Quick Start

```bash
# Install dependencies
pip install py-clob-client aiohttp eth-account

# Paper mode (default)
python main_v4.py

# With PM2
pm2 start main_v4.py --name polyedge-v4 --interpreter python3

# Dashboard at http://localhost:8081
```

## Environment Variables

```bash
PAPER_MODE=true              # true for paper, false for live
PRIVATE_KEY=                 # Polygon wallet private key
POLYMARKET_API_KEY=          # CLOB API credentials
POLYMARKET_API_SECRET=
POLYMARKET_API_PASSPHRASE=
ANTHROPIC_API_KEY=           # For meta-strategist (Haiku)
TELEGRAM_BOT_TOKEN=          # Alerts
TELEGRAM_CHAT_ID=
FOOTBALL_API_KEY=            # API-Football for live match events
```

## Risk Limits

- Market Maker: $50/market, $200 total, $50/day kill switch
- Sniper: $75/trade, $300 total, $25 stop loss
- Combined max exposure: $500
- Bankroll: $5,000

## Dashboard

8-panel real-time dashboard on `:8081`:
- System Status, MM Overview, Quote Book, Inventory
- Sniper Trades, Gap Radar, Event Log, Meta Strategist

## Stack

- Python 3.10+ / asyncio
- `py-clob-client` for EIP-712 signed orders on Polygon
- `aiohttp` for HTTP server + async requests
- Claude Haiku for meta-optimization (~$3/month)
