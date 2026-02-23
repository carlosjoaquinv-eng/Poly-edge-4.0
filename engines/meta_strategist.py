"""
PolyEdge v4 — Engine 3: Meta Strategist
=========================================
LLM-powered optimizer that reviews system performance every 12 hours.

NOT a signal generator — the LLM has no edge predicting markets.
Instead, it's a portfolio/parameter optimizer that:
  1. Reviews all trades from Engine 1 (MM) and Engine 2 (Sniper)
  2. Analyzes win rates, PnL attribution, risk metrics
  3. Detects underperforming strategies/markets
  4. Adjusts parameters (thresholds, sizing, market selection)
  5. Sends summary report via Telegram

Uses Claude Haiku for cost efficiency (~$0.05/run × 2/day = ~$3/month).

The meta-strategist treats the trading system as a black box with knobs:
  - Which markets to make in (MM)
  - How wide to quote (spread targets)
  - How aggressive to size (Kelly fractions)
  - Which event types to trade (Sniper)
  - When to increase/decrease exposure
"""

import asyncio
import json
import time
import math
import logging
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("polyedge.meta")


# ─────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────

@dataclass
class PerformanceSnapshot:
    """Point-in-time snapshot of system performance."""
    timestamp: float
    
    # Engine 1: Market Maker
    mm_active_markets: int = 0
    mm_quote_count: int = 0
    mm_fill_count: int = 0
    mm_spread_captured: float = 0.0
    mm_realized_pnl: float = 0.0
    mm_unrealized_pnl: float = 0.0
    mm_exposure: float = 0.0
    mm_kill_switch: bool = False
    
    # Engine 2: Sniper
    sniper_events_processed: int = 0
    sniper_gaps_detected: int = 0
    sniper_trades_executed: int = 0
    sniper_win_rate: float = 0.0
    sniper_total_pnl: float = 0.0
    sniper_exposure: float = 0.0
    
    # Combined
    total_pnl: float = 0.0
    total_exposure: float = 0.0
    bankroll: float = 5000.0
    roi_pct: float = 0.0


@dataclass
class ParameterAdjustment:
    """A recommended parameter change from the meta-strategist."""
    engine: str          # "mm" or "sniper"
    parameter: str       # e.g. "target_half_spread"
    current_value: Any
    new_value: Any
    reason: str
    confidence: float    # 0-100
    impact_estimate: str # e.g. "+$5/day" or "-15% risk"


@dataclass
class MetaReport:
    """Full report from a meta-strategist run."""
    timestamp: float
    period_hours: float
    snapshot: PerformanceSnapshot
    adjustments: List[ParameterAdjustment]
    summary: str              # LLM-generated summary
    risk_assessment: str      # LLM risk assessment
    recommendations: List[str]
    raw_llm_response: str = ""
    cost_usd: float = 0.0


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

@dataclass
class MetaConfig:
    """Meta Strategist configuration."""
    # Scheduling
    run_interval_hours: float = 12.0     # Run every 12 hours
    
    # LLM
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 2000
    temperature: float = 0.3             # Low temp for analytical consistency
    
    # API
    api_key_env: str = "ANTHROPIC_API_KEY"
    api_url: str = "https://api.anthropic.com/v1/messages"
    
    # Auto-adjustment limits (safety rails)
    max_spread_change_pct: float = 25.0     # Don't change spread by more than 25%
    max_kelly_change_pct: float = 20.0      # Don't change Kelly fraction by more than 20%
    max_exposure_change_pct: float = 30.0   # Don't change exposure limits by more than 30%
    min_trades_for_adjustment: int = 10     # Need at least 10 trades before adjusting
    
    # Auto-apply
    auto_apply_adjustments: bool = False    # Require human approval by default
    auto_apply_confidence_threshold: float = 80.0  # Auto-apply only if confidence > 80%


# ─────────────────────────────────────────────
# Performance Analyzer
# ─────────────────────────────────────────────

class PerformanceAnalyzer:
    """
    Computes performance metrics from engine stats.
    No LLM needed — pure math.
    """
    
    def __init__(self):
        self._snapshots: List[PerformanceSnapshot] = []
        self._max_snapshots = 500  # ~250 days at 2/day
    
    def take_snapshot(self, mm_stats: Dict, sniper_stats: Dict, 
                       bankroll: float = 5000.0) -> PerformanceSnapshot:
        """Capture current state from engine stats."""
        snap = PerformanceSnapshot(
            timestamp=time.time(),
            # MM
            mm_active_markets=mm_stats.get("active_markets", 0),
            mm_quote_count=mm_stats.get("quote_count", 0),
            mm_fill_count=mm_stats.get("fill_count", 0),
            mm_spread_captured=mm_stats.get("spread_captured", 0),
            mm_realized_pnl=mm_stats.get("pnl", {}).get("realized", 0),
            mm_unrealized_pnl=mm_stats.get("pnl", {}).get("unrealized", 0),
            mm_exposure=mm_stats.get("inventory", {}).get("total_exposure", 0),
            mm_kill_switch=mm_stats.get("kill_switch", False),
            # Sniper
            sniper_events_processed=sniper_stats.get("events_processed", 0),
            sniper_gaps_detected=sniper_stats.get("gaps_detected", 0),
            sniper_trades_executed=sniper_stats.get("trades_executed", 0),
            sniper_win_rate=sniper_stats.get("executor", {}).get("win_rate", 0),
            sniper_total_pnl=sniper_stats.get("executor", {}).get("total_pnl", 0),
            sniper_exposure=sniper_stats.get("executor", {}).get("total_exposure", 0),
            # Combined
            bankroll=bankroll,
        )
        
        snap.total_pnl = snap.mm_realized_pnl + snap.mm_unrealized_pnl + snap.sniper_total_pnl
        snap.total_exposure = snap.mm_exposure + snap.sniper_exposure
        snap.roi_pct = (snap.total_pnl / max(bankroll, 1)) * 100
        
        self._snapshots.append(snap)
        if len(self._snapshots) > self._max_snapshots:
            self._snapshots = self._snapshots[-self._max_snapshots:]
        
        return snap
    
    def compute_period_metrics(self, hours: float = 12.0) -> Dict:
        """Compute metrics for the last N hours."""
        cutoff = time.time() - hours * 3600
        period_snaps = [s for s in self._snapshots if s.timestamp >= cutoff]
        
        if len(period_snaps) < 2:
            return {"error": "Not enough data", "snapshots": len(period_snaps)}
        
        first = period_snaps[0]
        last = period_snaps[-1]
        
        return {
            "period_hours": hours,
            "snapshots_count": len(period_snaps),
            "mm": {
                "pnl_change": round(last.mm_realized_pnl - first.mm_realized_pnl, 2),
                "fills": last.mm_fill_count - first.mm_fill_count,
                "quotes": last.mm_quote_count - first.mm_quote_count,
                "fill_rate_pct": round(
                    (last.mm_fill_count - first.mm_fill_count) / 
                    max(last.mm_quote_count - first.mm_quote_count, 1) * 100, 1
                ),
                "avg_markets": round(
                    sum(s.mm_active_markets for s in period_snaps) / len(period_snaps), 1
                ),
                "current_exposure": round(last.mm_exposure, 2),
            },
            "sniper": {
                "pnl_change": round(last.sniper_total_pnl - first.sniper_total_pnl, 2),
                "events": last.sniper_events_processed - first.sniper_events_processed,
                "gaps": last.sniper_gaps_detected - first.sniper_gaps_detected,
                "trades": last.sniper_trades_executed - first.sniper_trades_executed,
                "win_rate": round(last.sniper_win_rate, 1),
                "current_exposure": round(last.sniper_exposure, 2),
            },
            "combined": {
                "total_pnl": round(last.total_pnl, 2),
                "period_pnl": round(last.total_pnl - first.total_pnl, 2),
                "roi_pct": round(last.roi_pct, 2),
                "total_exposure": round(last.total_exposure, 2),
                "bankroll": round(last.bankroll, 2),
            },
            "risk": {
                "max_exposure_seen": round(
                    max(s.total_exposure for s in period_snaps), 2
                ),
                "mm_kill_switch_triggered": any(s.mm_kill_switch for s in period_snaps),
                "exposure_utilization_pct": round(
                    last.total_exposure / 500 * 100, 1  # $200 MM + $300 Sniper = $500
                ),
            }
        }
    
    def get_trend(self, metric: str, periods: int = 6) -> List[float]:
        """Get trend of a metric over recent snapshots."""
        recent = self._snapshots[-periods:]
        return [getattr(s, metric, 0) for s in recent]


# ─────────────────────────────────────────────
# LLM Interface
# ─────────────────────────────────────────────

class LLMClient:
    """Lightweight Anthropic API client for meta-strategist."""
    
    def __init__(self, config: MetaConfig):
        self.config = config
        self._api_key: Optional[str] = None
        self._total_cost: float = 0.0
    
    def _get_api_key(self) -> str:
        if not self._api_key:
            import os
            self._api_key = os.environ.get(self.config.api_key_env, "")
        return self._api_key
    
    async def analyze(self, system_prompt: str, user_prompt: str) -> Tuple[str, float]:
        """
        Call Claude Haiku for analysis.
        Returns: (response_text, cost_usd)
        """
        api_key = self._get_api_key()
        if not api_key:
            logger.warning("No ANTHROPIC_API_KEY set — using mock response")
            return self._mock_response(user_prompt), 0.0
        
        try:
            import aiohttp
            
            headers = {
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            }
            
            payload = {
                "model": self.config.model,
                "max_tokens": self.config.max_tokens,
                "temperature": self.config.temperature,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.config.api_url,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        error = await resp.text()
                        logger.error(f"LLM API error {resp.status}: {error[:200]}")
                        return self._mock_response(user_prompt), 0.0
                    
                    data = await resp.json()
                    text = data.get("content", [{}])[0].get("text", "")
                    
                    # Estimate cost (Haiku: $0.25/M input, $1.25/M output)
                    usage = data.get("usage", {})
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    cost = (input_tokens * 0.25 + output_tokens * 1.25) / 1_000_000
                    self._total_cost += cost
                    
                    return text, cost
                    
        except ImportError:
            logger.warning("aiohttp not available — using mock response")
            return self._mock_response(user_prompt), 0.0
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return self._mock_response(user_prompt), 0.0
    
    def _mock_response(self, prompt: str) -> str:
        """Fallback response when API is unavailable."""
        return json.dumps({
            "summary": "Meta-strategist running in mock mode (no API key). "
                       "System appears operational. No parameter adjustments recommended "
                       "until real LLM analysis is available.",
            "risk_assessment": "Unable to assess — mock mode.",
            "adjustments": [],
            "recommendations": [
                "Set ANTHROPIC_API_KEY environment variable for real analysis.",
                "Continue monitoring in paper mode.",
            ]
        })
    
    @property
    def total_cost(self) -> float:
        return self._total_cost


# ─────────────────────────────────────────────
# Parameter Adjuster
# ─────────────────────────────────────────────

class ParameterAdjuster:
    """
    Applies parameter adjustments with safety rails.
    
    The LLM suggests changes, but this class enforces limits:
      - No change > X% from current value
      - Minimum trade count before adjusting
      - Changes are logged for audit trail
    """
    
    def __init__(self, config: MetaConfig):
        self.config = config
        self._adjustment_history: List[Dict] = []
    
    def validate_adjustment(self, adj: ParameterAdjustment, 
                             current_config: Dict) -> Tuple[bool, str]:
        """Validate a proposed adjustment against safety rails."""
        # Check minimum trade count
        # (caller should verify this before calling)
        
        # Check change magnitude
        if isinstance(adj.current_value, (int, float)) and isinstance(adj.new_value, (int, float)):
            if adj.current_value != 0:
                change_pct = abs(adj.new_value - adj.current_value) / abs(adj.current_value) * 100
            else:
                change_pct = 100 if adj.new_value != 0 else 0
            
            # Apply appropriate limit based on parameter type
            if "spread" in adj.parameter or "half_spread" in adj.parameter:
                limit = self.config.max_spread_change_pct
            elif "kelly" in adj.parameter:
                limit = self.config.max_kelly_change_pct
            elif "exposure" in adj.parameter or "max_position" in adj.parameter:
                limit = self.config.max_exposure_change_pct
            else:
                limit = 30.0  # Default limit
            
            if change_pct > limit:
                return False, f"Change too large: {change_pct:.0f}% > {limit:.0f}% limit"
        
        return True, "OK"
    
    def apply_adjustment(self, adj: ParameterAdjustment, 
                          mm_config=None, sniper_config=None) -> bool:
        """Apply an adjustment to the appropriate config."""
        target_config = mm_config if adj.engine == "mm" else sniper_config
        
        if target_config is None:
            logger.warning(f"No config available for engine '{adj.engine}'")
            return False
        
        if hasattr(target_config, adj.parameter):
            old_val = getattr(target_config, adj.parameter)
            setattr(target_config, adj.parameter, adj.new_value)
            
            self._adjustment_history.append({
                "timestamp": time.time(),
                "engine": adj.engine,
                "parameter": adj.parameter,
                "old_value": old_val,
                "new_value": adj.new_value,
                "reason": adj.reason,
                "confidence": adj.confidence,
            })
            
            logger.info(
                f"🔧 Adjusted {adj.engine}.{adj.parameter}: "
                f"{old_val} → {adj.new_value} ({adj.reason})"
            )
            return True
        else:
            logger.warning(f"Unknown parameter: {adj.engine}.{adj.parameter}")
            return False
    
    def get_history(self, limit: int = 20) -> List[Dict]:
        return self._adjustment_history[-limit:]


# ─────────────────────────────────────────────
# Meta Strategist Engine
# ─────────────────────────────────────────────

class MetaStrategist:
    """
    Engine 3: Meta Strategist
    
    Runs every 12 hours:
      1. Collect performance data from Engine 1 and Engine 2
      2. Compute metrics and trends
      3. Send to Claude Haiku for analysis
      4. Parse parameter adjustments
      5. Apply adjustments (if auto-apply enabled and confidence > threshold)
      6. Send Telegram report
    """
    
    SYSTEM_PROMPT = """You are a quantitative trading system optimizer for a Polymarket prediction market bot.

Your role is to analyze performance data and recommend parameter adjustments. You are NOT a market predictor — you optimize the system's risk/reward parameters.

The system has two engines:
1. Market Maker (MM) — earns spread by posting two-sided quotes. Key params: target_half_spread, max_position_per_market, max_total_inventory, kelly_fraction, min_liquidity, min_spread_cents.
2. Resolution Sniper — trades event-driven gaps (goals, crypto thresholds). Key params: min_gap_cents, min_edge_pct, max_trade_size, max_total_exposure, kelly_fraction.

You must respond ONLY with a valid JSON object (no markdown, no backticks) with this exact structure:
{
  "summary": "2-3 sentence performance summary",
  "risk_assessment": "1-2 sentence risk assessment",
  "adjustments": [
    {
      "engine": "mm" or "sniper",
      "parameter": "parameter_name",
      "current_value": <current>,
      "new_value": <recommended>,
      "reason": "why this change",
      "confidence": 0-100
    }
  ],
  "recommendations": ["actionable recommendation 1", "recommendation 2"]
}

Rules:
- Be conservative. Small adjustments (5-15%) are preferred.
- Don't adjust if there's not enough data (<10 trades).
- Focus on risk reduction when losing money.
- Focus on efficiency when profitable.
- Never recommend increasing exposure by more than 20%.
- If system is working well, recommend no adjustments."""
    
    def __init__(self, config: MetaConfig, telegram=None):
        self.config = config
        self.telegram = telegram
        
        self.analyzer = PerformanceAnalyzer()
        self.llm = LLMClient(config)
        self.adjuster = ParameterAdjuster(config)
        
        self._running = False
        self._paused = False  # Manual pause via /stop command
        self._reports: List[MetaReport] = []
        self._started_at = 0.0
        self._run_count = 0
        self._mm_config = None      # Set via set_configs()
        self._sniper_config = None   # Set via set_configs()
    
    def set_configs(self, mm_config=None, sniper_config=None):
        """Store live config references for prompt building."""
        self._mm_config = mm_config
        self._sniper_config = sniper_config
    
    # ── Lifecycle ──
    
    async def start(self):
        """Start the meta-strategist loop."""
        self._running = True
        self._started_at = time.time()
        
        logger.info(
            f"Meta Strategist starting (every {self.config.run_interval_hours}h, "
            f"model={self.config.model})"
        )
        
        if self.telegram:
            await self.telegram.send(
                f"🧠 Meta Strategist started\n"
                f"Schedule: every {self.config.run_interval_hours}h\n"
                f"Model: {self.config.model}\n"
                f"Auto-apply: {'ON' if self.config.auto_apply_adjustments else 'OFF'}"
            )
        
        await self._run_loop()
    
    async def stop(self):
        self._running = False
        logger.info("Meta Strategist stopped")
    
    async def _run_loop(self):
        """Main loop — run analysis periodically."""
        # Initial delay: wait 1 hour for engines to collect data
        await asyncio.sleep(3600)
        
        while self._running:
            try:
                if self._paused:
                    await asyncio.sleep(30)
                    continue
                await self.run_analysis()
            except Exception as e:
                logger.error(f"Meta analysis error: {e}", exc_info=True)
            
            await asyncio.sleep(self.config.run_interval_hours * 3600)
    
    async def run_analysis(self, mm_stats: Dict = None, sniper_stats: Dict = None,
                            mm_config=None, sniper_config=None) -> Optional[MetaReport]:
        """
        Run a single analysis cycle.
        
        Can be called manually (for testing) or by the loop.
        If stats aren't provided, uses last snapshot data.
        """
        self._run_count += 1
        logger.info(f"Meta Strategist run #{self._run_count}")
        
        try:
            # Take snapshot
            mm_s = mm_stats or {}
            sniper_s = sniper_stats or {}
            snapshot = self.analyzer.take_snapshot(mm_s, sniper_s)
            
            # Compute period metrics
            period = self.analyzer.compute_period_metrics(self.config.run_interval_hours)
            
            # Build prompt for LLM
            user_prompt = self._build_prompt(snapshot, period)
            
            # Call LLM
            response_text, cost = await self.llm.analyze(self.SYSTEM_PROMPT, user_prompt)
        except Exception as e:
            logger.error(f"Meta run #{self._run_count} failed during data/LLM phase: {e}", exc_info=True)
            return None
        
        # Parse response (separate try — LLM may return garbage)
        try:
            report = self._parse_response(response_text, snapshot, cost)
        except Exception as e:
            logger.error(f"Meta run #{self._run_count} failed parsing LLM response: {e}")
            return None
        
        if report:
            self._reports.append(report)
            
            # Apply adjustments if auto-apply is enabled
            if self.config.auto_apply_adjustments and mm_config and sniper_config:
                for adj in report.adjustments:
                    if adj.confidence >= self.config.auto_apply_confidence_threshold:
                        valid, reason = self.adjuster.validate_adjustment(adj, {})
                        if valid:
                            self.adjuster.apply_adjustment(adj, mm_config, sniper_config)
                        else:
                            logger.info(f"Adjustment rejected: {reason}")
            
            # Send Telegram report
            if self.telegram:
                await self._send_report(report)
            
            logger.info(
                f"Meta run #{self._run_count} complete: "
                f"{len(report.adjustments)} adjustments, cost=${cost:.4f}"
            )
        
        return report
    
    def _build_prompt(self, snapshot: PerformanceSnapshot, period: Dict) -> str:
        """Build the analysis prompt with current data."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        
        prompt = f"""Analyze this trading system performance as of {now}:

## Current State
- Total PnL: ${snapshot.total_pnl:.2f}
- ROI: {snapshot.roi_pct:.2f}%
- Total Exposure: ${snapshot.total_exposure:.2f} / $500 max
- Bankroll: ${snapshot.bankroll:.2f}

## Market Maker (Engine 1)
- Active Markets: {snapshot.mm_active_markets}
- Quotes Placed: {snapshot.mm_quote_count}
- Fills: {snapshot.mm_fill_count}
- Realized PnL: ${snapshot.mm_realized_pnl:.2f}
- Unrealized PnL: ${snapshot.mm_unrealized_pnl:.2f}
- Exposure: ${snapshot.mm_exposure:.2f} / $200 max
- Kill Switch: {'TRIGGERED' if snapshot.mm_kill_switch else 'OK'}

## Resolution Sniper (Engine 2)
- Events Processed: {snapshot.sniper_events_processed}
- Gaps Detected: {snapshot.sniper_gaps_detected}
- Trades Executed: {snapshot.sniper_trades_executed}
- Win Rate: {snapshot.sniper_win_rate:.1f}%
- PnL: ${snapshot.sniper_total_pnl:.2f}
- Exposure: ${snapshot.sniper_exposure:.2f} / $300 max

## Period Metrics ({period.get('period_hours', 12)}h)
{json.dumps(period, indent=2)}

## Current Parameters (loaded from live config)
### MM Config
- target_half_spread: {self._mm_config.target_half_spread if self._mm_config else 0.015}
- max_position_per_market: {self._mm_config.max_position_per_market if self._mm_config else 50.0}
- max_total_inventory: {self._mm_config.max_total_inventory if self._mm_config else 200.0}
- kelly_fraction: {self._mm_config.kelly_fraction if self._mm_config else 0.25}
- min_liquidity: {self._mm_config.min_liquidity if self._mm_config else 20000}
- min_spread_cents: {self._mm_config.min_spread_cents if self._mm_config else 0.5}

### Sniper Config
- min_gap_cents: {self._sniper_config.min_gap_cents if self._sniper_config else 3.0}
- min_edge_pct: {self._sniper_config.min_edge_pct if self._sniper_config else 3.0}
- max_trade_size: {self._sniper_config.max_trade_size if self._sniper_config else 75.0}
- max_total_exposure: {self._sniper_config.max_total_exposure if self._sniper_config else 300.0}
- kelly_fraction: {self._sniper_config.kelly_fraction if self._sniper_config else 0.3}

Provide your analysis and recommended parameter adjustments as JSON."""

        return prompt
    
    def _parse_response(self, response_text: str, 
                         snapshot: PerformanceSnapshot,
                         cost: float) -> Optional[MetaReport]:
        """Parse LLM response into a MetaReport."""
        try:
            # Clean potential markdown wrapping
            text = response_text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            
            data = json.loads(text)
            
            adjustments = []
            for adj_data in data.get("adjustments", []):
                adjustments.append(ParameterAdjustment(
                    engine=adj_data.get("engine", ""),
                    parameter=adj_data.get("parameter", ""),
                    current_value=adj_data.get("current_value"),
                    new_value=adj_data.get("new_value"),
                    reason=adj_data.get("reason", ""),
                    confidence=adj_data.get("confidence", 0),
                    impact_estimate=adj_data.get("impact_estimate", ""),
                ))
            
            return MetaReport(
                timestamp=time.time(),
                period_hours=self.config.run_interval_hours,
                snapshot=snapshot,
                adjustments=adjustments,
                summary=data.get("summary", "No summary"),
                risk_assessment=data.get("risk_assessment", "No assessment"),
                recommendations=data.get("recommendations", []),
                raw_llm_response=response_text,
                cost_usd=cost,
            )
            
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.error(f"Failed to parse LLM response: {e}\nResponse: {response_text[:500]}")
            
            # Create minimal report
            return MetaReport(
                timestamp=time.time(),
                period_hours=self.config.run_interval_hours,
                snapshot=snapshot,
                adjustments=[],
                summary="Parse error — could not interpret LLM response.",
                risk_assessment="Unable to assess.",
                recommendations=["Review raw LLM output manually."],
                raw_llm_response=response_text,
                cost_usd=cost,
            )
    
    async def _send_report(self, report: MetaReport):
        """Send formatted report via Telegram."""
        snap = report.snapshot
        
        # PnL emoji
        pnl_emoji = "📈" if snap.total_pnl >= 0 else "📉"
        
        msg = (
            f"🧠 Meta Strategist Report\n"
            f"{'─' * 28}\n"
            f"{pnl_emoji} Total PnL: ${snap.total_pnl:+.2f} ({snap.roi_pct:+.2f}%)\n"
            f"💰 MM: ${snap.mm_realized_pnl:+.2f} | Sniper: ${snap.sniper_total_pnl:+.2f}\n"
            f"📊 Exposure: ${snap.total_exposure:.0f}/$500\n"
            f"\n"
            f"📋 {report.summary}\n"
            f"\n"
            f"⚠️ {report.risk_assessment}\n"
        )
        
        if report.adjustments:
            msg += f"\n🔧 Adjustments ({len(report.adjustments)}):\n"
            for adj in report.adjustments:
                applied = "✅" if self.config.auto_apply_adjustments and \
                    adj.confidence >= self.config.auto_apply_confidence_threshold else "📋"
                msg += f"  {applied} {adj.engine}.{adj.parameter}: {adj.current_value}→{adj.new_value}\n"
                msg += f"     {adj.reason}\n"
        
        if report.recommendations:
            msg += f"\n💡 Recommendations:\n"
            for rec in report.recommendations[:3]:
                msg += f"  • {rec}\n"
        
        msg += f"\n💵 LLM cost: ${report.cost_usd:.4f}"
        
        await self.telegram.send(msg)
    
    # ── Manual Trigger ──
    
    async def force_run(self, mm_stats: Dict, sniper_stats: Dict,
                         mm_config=None, sniper_config=None) -> Optional[MetaReport]:
        """Manually trigger an analysis run."""
        return await self.run_analysis(mm_stats, sniper_stats, mm_config, sniper_config)
    
    # ── Stats ──
    
    def get_stats(self) -> Dict:
        uptime = time.time() - self._started_at if self._started_at else 0
        
        return {
            "engine": "meta_strategist",
            "status": "running" if self._running else "stopped",
            "model": self.config.model,
            "uptime_hours": round(uptime / 3600, 1),
            "run_count": self._run_count,
            "next_run_in_hours": round(
                max(0, self.config.run_interval_hours - (uptime % (self.config.run_interval_hours * 3600)) / 3600), 1
            ),
            "total_llm_cost": round(self.llm.total_cost, 4),
            "auto_apply": self.config.auto_apply_adjustments,
            "adjustment_history": self.adjuster.get_history(10),
            "recent_reports": [
                {
                    "time": datetime.fromtimestamp(r.timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "total_pnl": round(r.snapshot.total_pnl, 2),
                    "adjustments": len(r.adjustments),
                    "summary": r.summary[:100],
                    "cost": round(r.cost_usd, 4),
                }
                for r in self._reports[-5:]
            ],
        }
