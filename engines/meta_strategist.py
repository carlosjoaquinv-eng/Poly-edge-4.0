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


@dataclass
class AdjustmentOutcome:
    """Tracks whether a past adjustment helped or hurt performance."""
    run_timestamp: float
    pnl_at_adjustment: float
    adjustments: List[Dict] = field(default_factory=list)
    pnl_at_review: float = 0.0
    pnl_delta: float = 0.0
    outcome: str = "pending"   # "good", "bad", "neutral", "rolled_back", "pending"
    reviewed: bool = False


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
    auto_apply_confidence_threshold: float = 80.0  # Legacy: kept for reference
    auto_apply_min_confidence: float = 60.0  # Below this: reject entirely

    # Auto-rollback
    auto_rollback_enabled: bool = True
    auto_rollback_loss_usd: float = 25.0     # Rollback if PnL drops > $25
    auto_rollback_loss_pct: float = 5.0      # Or drops > 5% of bankroll


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

    Pipeline: scale by confidence → clamp to bounds → validate % change → apply.
    """

    # Absolute min/max bounds per parameter — prevents drift to nonsensical values
    PARAM_BOUNDS: Dict[str, tuple] = {
        # MM params
        "kelly_fraction": (0.05, 0.5),
        "target_half_spread": (0.005, 0.05),
        "max_position_per_market": (10.0, 200.0),
        "max_total_inventory": (50.0, 500.0),
        "min_quote_size": (5.0, 50.0),
        "max_quote_size": (10.0, 100.0),
        "min_spread_cents": (0.1, 5.0),
        "max_spread_cents": (2.0, 20.0),
        "inventory_skew_factor": (0.1, 1.0),
        "max_loss_per_day": (10.0, 200.0),
        # Sniper params
        "min_gap_cents": (1.0, 10.0),
        "min_edge_pct": (1.0, 15.0),
        "max_trade_size": (10.0, 200.0),
        "max_total_exposure": (50.0, 500.0),
        "min_confidence": (30.0, 90.0),
        "max_concurrent_trades": (1, 12),
        "max_loss_per_trade": (5.0, 100.0),
    }

    def __init__(self, config: MetaConfig):
        self.config = config
        self._adjustment_history: List[Dict] = []

    def scale_adjustment(self, adj: ParameterAdjustment,
                          min_conf: float) -> ParameterAdjustment:
        """
        Scale adjustment magnitude by confidence.

        Formula: scale = (confidence - min_conf) / (100 - min_conf)
                 effective = current + (recommended - current) * scale

        conf=min_conf → 0% of change, conf=100 → 100% of change.
        """
        if not isinstance(adj.current_value, (int, float)) or \
           not isinstance(adj.new_value, (int, float)):
            return adj  # Non-numeric: can't scale

        conf = max(min_conf, min(adj.confidence, 100.0))
        scale = (conf - min_conf) / max(100.0 - min_conf, 1.0)
        effective = adj.current_value + (adj.new_value - adj.current_value) * scale

        if isinstance(adj.current_value, int):
            effective = round(effective)
        else:
            effective = round(effective, 6)

        return ParameterAdjustment(
            engine=adj.engine, parameter=adj.parameter,
            current_value=adj.current_value, new_value=effective,
            reason=adj.reason, confidence=adj.confidence,
            impact_estimate=adj.impact_estimate,
        )

    def clamp_to_bounds(self, adj: ParameterAdjustment) -> ParameterAdjustment:
        """Clamp new_value to hard bounds if defined for this parameter."""
        bounds = self.PARAM_BOUNDS.get(adj.parameter)
        if bounds and isinstance(adj.new_value, (int, float)):
            lo, hi = bounds
            clamped = max(lo, min(adj.new_value, hi))
            if clamped != adj.new_value:
                logger.info(f"Clamped {adj.parameter}: {adj.new_value} → {clamped} (bounds [{lo}, {hi}])")
                return ParameterAdjustment(
                    engine=adj.engine, parameter=adj.parameter,
                    current_value=adj.current_value, new_value=clamped,
                    reason=adj.reason, confidence=adj.confidence,
                    impact_estimate=adj.impact_estimate,
                )
        return adj

    def validate_adjustment(self, adj: ParameterAdjustment,
                             current_config: Dict) -> Tuple[bool, str]:
        """Validate a proposed adjustment against safety rails."""
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
                limit = 30.0

            if change_pct > limit:
                return False, f"Change too large: {change_pct:.0f}% > {limit:.0f}% limit"

            # Check hard bounds
            bounds = self.PARAM_BOUNDS.get(adj.parameter)
            if bounds:
                lo, hi = bounds
                if adj.new_value < lo or adj.new_value > hi:
                    return False, f"Out of bounds: {adj.new_value} not in [{lo}, {hi}]"

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

        # Outcome tracking
        self._pending_outcomes: List[AdjustmentOutcome] = []
        self._completed_outcomes: List[AdjustmentOutcome] = []
        self._last_adj_statuses: List[Dict] = []  # For Telegram report
    
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
        Uses self._mm_config / self._sniper_config for auto-apply (set via set_configs).
        """
        self._run_count += 1
        logger.info(f"Meta Strategist run #{self._run_count}")

        try:
            mm_s = mm_stats or {}
            sniper_s = sniper_stats or {}
            snapshot = self.analyzer.take_snapshot(mm_s, sniper_s)

            # Review outcomes from previous adjustments
            await self._review_outcomes(snapshot)

            period = self.analyzer.compute_period_metrics(self.config.run_interval_hours)
            user_prompt = self._build_prompt(snapshot, period)
            response_text, cost = await self.llm.analyze(self.SYSTEM_PROMPT, user_prompt)
        except Exception as e:
            logger.error(f"Meta run #{self._run_count} failed during data/LLM phase: {e}", exc_info=True)
            return None

        try:
            report = self._parse_response(response_text, snapshot, cost)
        except Exception as e:
            logger.error(f"Meta run #{self._run_count} failed parsing LLM response: {e}")
            return None

        if report:
            self._reports.append(report)

            # Resolve live configs: prefer method args (manual/test), fall back to stored refs
            live_mm = mm_config or self._mm_config
            live_sniper = sniper_config or self._sniper_config

            # Auto-apply pipeline: reject → scale → clamp → validate → apply
            self._last_adj_statuses = []
            if self.config.auto_apply_adjustments and live_mm and live_sniper:
                applied = []
                for adj in report.adjustments:
                    status = self._apply_single_adjustment(adj, live_mm, live_sniper)
                    self._last_adj_statuses.append(status)
                    if status["status"] in ("applied", "partial"):
                        applied.append(status["scaled_adj"])

                if applied:
                    self._record_adjustment_snapshot(snapshot, applied)
            else:
                # Auto-apply off — mark all as recommended-only
                for adj in report.adjustments:
                    self._last_adj_statuses.append({"adj": adj, "status": "recommend"})

            if self.telegram:
                await self._send_report(report)

            logger.info(
                f"Meta run #{self._run_count} complete: "
                f"{len(report.adjustments)} adjustments, cost=${cost:.4f}"
            )

        return report

    def _apply_single_adjustment(self, adj: ParameterAdjustment,
                                  mm_config, sniper_config) -> Dict:
        """Run one adjustment through the confidence-weighted pipeline."""
        min_conf = self.config.auto_apply_min_confidence

        # Reject if below minimum confidence
        if adj.confidence < min_conf:
            logger.info(f"Rejected {adj.engine}.{adj.parameter} (conf {adj.confidence:.0f} < {min_conf})")
            return {"adj": adj, "status": "rejected_confidence",
                    "reason": f"conf {adj.confidence:.0f} < {min_conf}"}

        # Scale by confidence
        scaled = self.adjuster.scale_adjustment(adj, min_conf)

        # Skip no-ops (confidence exactly at threshold → scale=0)
        if isinstance(scaled.new_value, (int, float)) and isinstance(scaled.current_value, (int, float)):
            if abs(scaled.new_value - scaled.current_value) < 1e-9:
                return {"adj": adj, "status": "rejected_noop", "reason": "no effective change"}

        # Clamp to hard bounds
        scaled = self.adjuster.clamp_to_bounds(scaled)

        # Validate % change limits
        valid, reason = self.adjuster.validate_adjustment(scaled, {})
        if not valid:
            logger.info(f"Rejected {adj.engine}.{adj.parameter}: {reason}")
            return {"adj": adj, "status": "rejected_validation", "reason": reason}

        # Apply
        self.adjuster.apply_adjustment(scaled, mm_config, sniper_config)

        # Determine if partial or full
        is_partial = adj.confidence < 80
        status = "partial" if is_partial else "applied"

        return {"adj": adj, "status": status, "scaled_adj": scaled,
                "scaled_value": scaled.new_value}

    # ── Outcome Tracking ──

    def _record_adjustment_snapshot(self, snapshot: PerformanceSnapshot,
                                     applied: List[ParameterAdjustment]):
        """Record PnL and adjustments at the time of application."""
        outcome = AdjustmentOutcome(
            run_timestamp=time.time(),
            pnl_at_adjustment=snapshot.total_pnl,
            adjustments=[
                {"engine": a.engine, "parameter": a.parameter,
                 "old_value": a.current_value, "new_value": a.new_value,
                 "confidence": a.confidence}
                for a in applied
            ],
        )
        self._pending_outcomes.append(outcome)
        # Keep only last 20 pending
        if len(self._pending_outcomes) > 20:
            self._pending_outcomes = self._pending_outcomes[-20:]
        logger.info(f"Recorded adjustment snapshot: PnL=${snapshot.total_pnl:.2f}, {len(applied)} adjustments")

    async def _review_outcomes(self, snapshot: PerformanceSnapshot):
        """
        Review pending outcomes from previous adjustments.

        Called at the start of each run. Compares current PnL to PnL at
        adjustment time after 6h+ elapsed. Marks good/bad/neutral.
        Also triggers auto-rollback if enabled and loss exceeds thresholds.
        """
        now = time.time()
        min_elapsed = 6 * 3600  # Wait at least 6 hours before judging

        still_pending = []
        for outcome in self._pending_outcomes:
            if outcome.reviewed:
                continue

            elapsed = now - outcome.run_timestamp
            if elapsed < min_elapsed:
                still_pending.append(outcome)
                continue

            # Enough time has passed — evaluate
            outcome.pnl_at_review = snapshot.total_pnl
            outcome.pnl_delta = snapshot.total_pnl - outcome.pnl_at_adjustment
            outcome.reviewed = True

            if outcome.pnl_delta > 1.0:
                outcome.outcome = "good"
            elif outcome.pnl_delta < -1.0:
                outcome.outcome = "bad"
            else:
                outcome.outcome = "neutral"

            self._completed_outcomes.append(outcome)
            logger.info(
                f"Outcome review: PnL delta=${outcome.pnl_delta:+.2f} → {outcome.outcome} "
                f"(adj from {datetime.fromtimestamp(outcome.run_timestamp, timezone.utc).strftime('%m-%d %H:%M')})"
            )

            # Auto-rollback check
            if outcome.outcome == "bad" and self.config.auto_rollback_enabled:
                await self._check_and_rollback(outcome, snapshot)

        self._pending_outcomes = still_pending
        # Keep only last 50 completed outcomes
        if len(self._completed_outcomes) > 50:
            self._completed_outcomes = self._completed_outcomes[-50:]

    async def _check_and_rollback(self, outcome: AdjustmentOutcome,
                                    snapshot: PerformanceSnapshot):
        """
        Auto-rollback if PnL dropped significantly after adjustments.

        Triggers if loss exceeds auto_rollback_loss_usd OR auto_rollback_loss_pct
        of bankroll. Restores pre-adjustment values via setattr().
        """
        loss = abs(outcome.pnl_delta)
        loss_pct = (loss / max(snapshot.bankroll, 1)) * 100

        should_rollback = (
            loss >= self.config.auto_rollback_loss_usd or
            loss_pct >= self.config.auto_rollback_loss_pct
        )

        if not should_rollback:
            return

        logger.warning(
            f"Auto-rollback triggered: PnL delta=${outcome.pnl_delta:+.2f} "
            f"(loss ${loss:.2f} / {loss_pct:.1f}% of bankroll)"
        )

        rolled_back = []
        for adj_info in outcome.adjustments:
            engine = adj_info.get("engine", "")
            param = adj_info.get("parameter", "")
            old_val = adj_info.get("old_value")

            target = self._mm_config if engine == "mm" else self._sniper_config
            if target and hasattr(target, param) and old_val is not None:
                current = getattr(target, param)
                setattr(target, param, old_val)
                rolled_back.append(f"{engine}.{param}: {current} → {old_val}")
                logger.info(f"Rolled back {engine}.{param}: {current} → {old_val}")

        outcome.outcome = "rolled_back"

        if rolled_back and self.telegram:
            msg = (
                f"🔄 Auto-Rollback Triggered\n"
                f"{'─' * 28}\n"
                f"PnL delta: ${outcome.pnl_delta:+.2f} "
                f"(${loss:.2f} loss / {loss_pct:.1f}% of bankroll)\n\n"
                f"Restored parameters:\n"
            )
            for rb in rolled_back:
                msg += f"  • {rb}\n"
            await self.telegram.send(msg)

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

"""

        # Add outcome history if we have any
        if self._completed_outcomes:
            recent = self._completed_outcomes[-5:]
            prompt += "\n## Past Adjustment Outcomes\n"
            prompt += "These are outcomes of previous adjustments — use them to calibrate confidence.\n"
            for o in recent:
                ts = datetime.fromtimestamp(o.run_timestamp, timezone.utc).strftime("%m-%d %H:%M")
                prompt += (
                    f"- [{ts}] {o.outcome.upper()}: PnL delta=${o.pnl_delta:+.2f} "
                    f"({len(o.adjustments)} params adjusted)\n"
                )
            good = sum(1 for o in self._completed_outcomes if o.outcome == "good")
            bad = sum(1 for o in self._completed_outcomes if o.outcome == "bad")
            rolled = sum(1 for o in self._completed_outcomes if o.outcome == "rolled_back")
            prompt += f"Overall record: {good} good, {bad} bad, {rolled} rolled back\n"

        prompt += "\nProvide your analysis and recommended parameter adjustments as JSON."

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
        
        if self._last_adj_statuses:
            msg += f"\n🔧 Adjustments ({len(self._last_adj_statuses)}):\n"
            for s in self._last_adj_statuses:
                adj = s["adj"]
                status = s["status"]
                icon = {"applied": "✅", "partial": "⚠️", "recommend": "📋",
                        "rejected_confidence": "❌", "rejected_validation": "❌",
                        "rejected_noop": "❌"}.get(status, "❓")
                scaled_val = s.get("scaled_value", adj.new_value)
                msg += f"  {icon} {adj.engine}.{adj.parameter}: {adj.current_value}→{scaled_val}"
                if status == "partial":
                    msg += f" (scaled, conf={adj.confidence:.0f})"
                elif status.startswith("rejected"):
                    msg += f" ({s.get('reason', status)})"
                msg += f"\n     {adj.reason}\n"
        elif report.adjustments:
            msg += f"\n📋 Recommendations ({len(report.adjustments)}):\n"
            for adj in report.adjustments:
                msg += f"  📋 {adj.engine}.{adj.parameter}: {adj.current_value}→{adj.new_value}\n"
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
