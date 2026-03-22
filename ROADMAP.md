# PolyEdge v4 — Roadmap

**Started**: March 2026
**Deposit**: $507 | **Current Equity**: ~$463 | **ROI**: -8.7%
**Server**: DigitalOcean NYC (159.223.126.102)
**Last Updated**: 2026-03-22

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

## Milestone 2: Revenue Optimization (Days 5-7)

### Phase 5: MM Competitive Quoting — Day 5 ✅
> MM was quoting at $0.01 on dead markets. Fixed to competitive prices.
- [x] Smart mid calculation (Gamma anchor + CLOB dust filter)
- [x] Dead market penalty (>10¢ spread penalized in scoring)
- [x] max_spread_cents 15→8¢ (reject 98¢ dead markets)
- [x] target_half_spread 2.5→1.5¢ (competitive in 3-5¢ markets)
- [x] Selective proxy (GET direct, POST via rotating residential)
- [x] Quote size tuned for available balance ($5 max)
- [x] Validated BID+ASK two-sided on Sweden WC (0.326/0.355)
- [ ] Inventory panel: show market names instead of token IDs
- [ ] Profile selector A/B/C in Config tab (Conservative/Balanced/Aggressive)
- [ ] Monitor buy/sell fill ratio (need fills to measure)
- [ ] Increase quote size when more USDC freed up

### Phase 6: Sniper v3 — Day 6
> Sniper in paper mode, -$7.50 PnL, 30% win rate. Football API exhausted.
- [ ] Football feed alternative (football-data.org — free tier has 429 errors)
- [ ] Weather feed module (OpenWeatherMap for weather markets)
- [ ] Filter "live events only" (sniper fires on old events at startup)
- [ ] Sniper edge validation (only trade when gap > 5c AND confidence > 80%)
- [ ] Paper mode validation: run 48h, need >50% win rate before going live
- [ ] Multi-wallet separation (separate MM vs Sniper funds)

### Phase 7: Position Management — Day 7
- [ ] Manual sell button per position in Positions tab
- [ ] Bulk sell (close all losing positions)
- [ ] Position age tracking (how long held, expected resolution date)
- [ ] Market resolution alerts (Telegram notification when market resolves)
- [ ] Auto-claim resolved positions (collect winnings automatically)

---

## Milestone 3: Scale & Automation (Days 8-10)

### Phase 8: Analytics & Intelligence
- [ ] Market profitability analysis (which markets make money, which lose)
- [ ] Spread analysis heatmap (best times to trade)
- [ ] Competitor analysis (track other MM wallets on same markets)
- [ ] Meta Strategist auto-tuning (AI adjusts MM params based on performance)

### Phase 9: Infrastructure Hardening
- [ ] Move bot to Raspberry Pi 5 (save $6/mo DigitalOcean)
- [ ] Health check alerts (Telegram if bot crashes/disconnects)
- [ ] Auto-restart on WS disconnect (PM2 + watchdog)
- [ ] Rate limit monitoring (track API usage vs limits)
- [ ] Database for trade history (SQLite instead of JSON logs)
- [ ] HTTPS with real domain (DuckDNS + Let's Encrypt)

### Phase 10: New Verticals
- [ ] Crypto event markets (ETH ETF, BTC halving, token launches)
- [ ] Political markets (elections, policy decisions)
- [ ] Custom market scanner (find high-spread opportunities automatically)
- [ ] Multi-account support (run multiple wallets independently)

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
│         Polymarket API + WebSocket          │
├─────────────────────────────────────────────┤
│    Feeds: Football │ Crypto │ Orderbook     │
├─────────────────────────────────────────────┤
│  Dashboard API (aiohttp:8081) + HTML UI     │
│  Telegram Notifier + Daily Reports          │
└─────────────────────────────────────────────┘

Infra: DigitalOcean NYC → Webshare Rotating Residential → Polymarket
Wallet: 0xC0637B5E (proxy) ← 0xea6b1Df8 (EOA/Rabby)
```

## Key Metrics to Track

| Metric | Target | Current |
|--------|--------|---------|
| Daily PnL | >$0 (positive) | ~$0 |
| MM Fill Rate (sells) | >60% | Pending (just started competitive quoting) |
| Sniper Win Rate | >50% | 30% (paper, feed exhausted) |
| Uptime | >99% | ~95% (PM2 restarts) |
| Equity drawdown | <15% from peak | -8.7% from deposit |
| Active MM markets | 3-5 | 2-3 (Sweden, Italy, BitBoy) |
| Exit state persistence | 100% | ✅ (survives restarts) |

## Philosophy

1. **Preserve Capital** — SL ratchet, inventory caps, kill switch
2. **Be the House** — capture spread, don't predict outcomes
3. **Compound Small Wins** — 1-3c per trade, thousands of trades
4. **Protect Winners** — dynamic TP, trailing stops, never turn profit into loss
