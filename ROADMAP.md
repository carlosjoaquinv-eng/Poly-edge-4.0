# PolyEdge v4 — Roadmap

**Started**: March 2026
**Deposit**: $507 | **Current Equity**: ~$463 | **ROI**: -8.7%
**Server**: DigitalOcean NYC (159.223.126.102)
**Last Updated**: 2026-04-03

---

## Milestone 1: Foundation & Capital Preservation (Days 1-4) ✅

### Phase 1: Critical Bug Fixes — Day 1 ✅
- [x] SL infinite loop fix (Columbus Blue Jackets retrying 123 times)
- [x] MM phantom inventory sync (cancelled orders counted as fills)
- [x] Real PnL tracking (equity-based via data-api, not internal tracker)

### Phase 2: Dashboard & Reporting — Day 2 ✅
- [x] On-chain Positions tab (all positions with live P&L from data-api)
- [x] Equity curve chart in PnL tab ($507 deposit reference line)
- [x] Daily PnL tracker (bar chart + table)
- [x] Telegram daily report (midnight UTC summary)

### Phase 3: MM Profitability — Day 3 ✅
- [x] Phantom fill prevention (cancelled orders no longer recorded as fills)
- [x] Market selection fix (0 → 1+ markets)
- [x] can_sell fix (no selling without real inventory)
- [x] Force-ASK when inventory high
- [x] Inventory cap enforcement (>80% = sell-only mode)
- [x] Skew factor config (MM_SKEW_FACTOR=2.0)
- [x] MM config tuning (spread 1.5%, refresh 15s, max 5 markets)

### Phase 4: Risk Management & Infrastructure — Day 4 ✅
- [x] Hybrid Dynamic TP (entry-price tiers: <5c/5-15c/15-35c/>35c)
- [x] SL Ratchet (SL floor rises with profit, never drops)
- [x] Trailing stop tightening (trail shrinks as profit grows)
- [x] Proxy upgrade (Webshare Rotating Residential, selective proxy)
- [x] Exit state persistence (ratchet/TP survives PM2 restarts)
- [x] SL breakeven buffer (2% anti-stop-hunt protection)
- [x] Smart exit pricing (orderbook best_bid instead of -1% haircut)
- [x] Auto backup cron (state + config every 6h)
- [ ] ~~HTTPS for dashboard~~ (deferred — self-signed works, needs domain)
- [ ] ~~Raspberry Pi 5 + WireGuard~~ (deferred — needs hardware purchase)

---

## Milestone 2: Revenue Optimization (Days 5-8) — IN PROGRESS

### Phase 5: MM Competitive Quoting — Day 5 ✅
- [x] Smart mid calculation (Gamma anchor + CLOB dust filter)
- [x] Dead market penalty (>10¢ spread penalized in scoring)
- [x] target_half_spread 2.5→1.5¢ (competitive in 3-5¢ markets)
- [x] Selective proxy (GET direct, POST via rotating residential)
- [x] Liquidity checker connected (blocks illiquid markets pre-trade)
- [x] Capital preservation gate (25% reserve, 3% per trade, 70% max exposure)
- [x] Breakeven floor (never sell below entry + 0.5¢)
- [x] Risk profiles A/B/C (Conservative/Balanced/Aggressive)
- [x] Risk calculator module with Kelly criterion
- [ ] Increase quote size when more USDC freed up

### Phase 6: Sniper v3 — Day 6 ✅
- [x] Football feed smart rate-limit recovery (30min backoff, not permanent)
- [x] Stale event filter (first poll suppression)
- [x] Sniper edge validation: 10¢ gap, 80% confidence (backtest-optimized)
- [x] Crypto feed expanded (BTC, ETH, SOL, DOGE, XRP, SUI)
- [x] Weather feed (Open-Meteo, 12 cities, no API key needed)
- [x] Multi-sport feed (NBA + NHL via api-sports.io)
- [x] Backtester built (13 trades analyzed, 75% WR at 10¢+)
- [x] Auto-promotion system (paper → LIVE when criteria met)
- [ ] Multi-wallet separation (separate MM vs Sniper funds)

### Phase 7: Position Management — Day 7 ✅
- [x] Manual sell button per position (dashboard + Telegram /sell)
- [x] Bulk sell losers
- [x] Position age tracking (days held, days to resolve)
- [x] Market resolution alerts (Telegram, 3 days before + on resolution)
- [x] Auto-claim resolved positions (sell @ $0.99 on resolution)
- [x] Dead token filter (hide positions < $0.05)

### Phase 8: Dashboard & UX — Day 8 ✅
- [x] Sidebar navigation (collapsible, icons + labels)
- [x] SVG professional icons (grid, trending, dollar, cube, gear)
- [x] Mobile responsive (sidebar → bottom bar)
- [x] Telegram commands (/sell, /ap, /status, /kill, /resume, /stop)
- [x] Remember username on login
- [ ] Inventory panel: show market names instead of token IDs

---

## Milestone 3: Intelligence & Scale (Days 9-14) — PLANNED

### Phase 9: Whale Tracking & Confluences
> Track large Polymarket wallets to identify smart money flow and create confluence signals with existing feeds.
- [ ] Identify top 20 whale wallets on Polymarket (>$100K volume)
- [ ] On-chain monitoring via Polygon RPC (track large buys/sells)
- [ ] Whale signal events: emit when whale buys >$1K in a market
- [ ] Confluence scoring: whale + sniper signal = higher confidence
- [ ] Whale dashboard panel (top wallets, recent moves)
- [ ] Anti-whale filter: don't enter markets where whales are exiting
- [ ] Telegram alerts: "Whale 0xABC bought $5K YES on Lakers"

### Phase 10: Analytics & Intelligence
- [ ] Market profitability analysis (which markets make money, which lose)
- [ ] Spread analysis heatmap (best times to trade)
- [ ] Competitor analysis (track other MM wallets on same markets)
- [ ] Meta Strategist auto-tuning (AI adjusts MM params based on performance)
- [ ] Historical performance dashboard (daily/weekly/monthly charts)

### Phase 11: Infrastructure Hardening
- [ ] Move bot to Raspberry Pi 5 (save $6/mo DigitalOcean)
- [ ] Health check alerts (Telegram if bot crashes/disconnects)
- [ ] Auto-restart on WS disconnect (PM2 + watchdog)
- [ ] Rate limit monitoring (track API usage vs limits)
- [ ] Database for trade history (SQLite instead of JSON logs)
- [ ] HTTPS with real domain (DuckDNS + Let's Encrypt)

### Phase 12: New Verticals
- [ ] Crypto event markets (ETH ETF, BTC halving, token launches)
- [ ] Political markets (elections, policy decisions)
- [ ] Custom market scanner (find high-spread opportunities automatically)
- [ ] Multi-account support (run multiple wallets independently)
- [ ] Multi-level quoting (3 BID + 3 ASK per market)

---

## Architecture

```
┌─────────────────────────────────────────────┐
│                 Orchestrator                │
├──────────┬──────────┬───────────┬───────────┤
│    MM    │  Sniper  │   Meta    │   Exit    │
│  Engine  │  Engine  │ Strategist│  Manager  │
├──────────┴──────────┴───────────┴───────────┤
│              CLOB Client (v4)               │
│   Selective Proxy (GET direct, POST proxy)  │
├─────────────────────────────────────────────┤
│  Feeds: Football │ NBA │ NHL │ Crypto │ Wx  │
├─────────────────────────────────────────────┤
│  Risk: Liquidity Checker │ Capital Gate     │
│        Breakeven Floor │ SL Ratchet         │
├─────────────────────────────────────────────┤
│  Dashboard API (aiohttp:8081) + Sidebar UI  │
│  Telegram Bot (commands + alerts)           │
└─────────────────────────────────────────────┘

Infra: DigitalOcean NYC → Webshare Rotating Residential → Polymarket
Wallet: 0xC0637B5E (proxy) ← 0xea6b1Df8 (EOA/Rabby)
```

## Key Metrics to Track

| Metric | Target | Current |
|--------|--------|---------|
| Daily PnL | >$0 (positive) | ~$0 (MM just reactivated) |
| MM Fill Rate (sells) | >60% | ~30% (improving) |
| Sniper Win Rate | >60% | 46% paper (new params: 75% backtest) |
| Uptime | >99% | ~98% (PM2 restarts on deploy) |
| Equity drawdown | <15% from peak | -8.7% from deposit |
| Active MM markets | 3-5 | 4 |
| Capital reserve | >25% free | ~8% ($38 of $463) |
| Exit state persistence | 100% | ✅ |

## Philosophy

1. **Preserve Capital** — SL ratchet, capital gate, reserve 25%, breakeven floor
2. **Be the House** — capture spread, don't predict outcomes
3. **Compound Small Wins** — 1-3c per trade, thousands of trades
4. **Protect Winners** — dynamic TP, trailing stops, never turn profit into loss
5. **Data-Driven Decisions** — backtest before deploying, paper before live
6. **Let Winners Run** — trail tightens with profit, TP sells partial not all

## Code Stats

| Metric | Value |
|--------|-------|
| Python LOC | ~11,000 |
| HTML/CSS/JS LOC | ~26,000 |
| Total files | 19 .py + 12 .html |
| Commits | 60+ |
| Modules | 12 (MM, Sniper, Exit, Risk, Liquidity, 5 Feeds, Bridge, Monitor) |
