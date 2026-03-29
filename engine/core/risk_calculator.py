"""
PolyEdge v4 — Risk Calculator
================================
Polymarket-adapted position sizing and risk/reward calculator.
Inspired by calc.trade but tailored for binary outcome markets.

Key differences from crypto futures:
  - No leverage (shares cost $0.01-$0.99 each)
  - No liquidation (max loss = cost basis)
  - Binary outcomes (resolves to $1.00 or $0.00)
  - Fees: 0% maker, ~1% taker on Polymarket CLOB
  - Profit = (1.00 - entry_price) * shares  (if YES wins)
  - Loss = entry_price * shares  (if YES loses)

Usage:
  calc = RiskCalculator(bankroll=507, risk_pct=2.0)
  result = calc.size_trade(entry_price=0.35, confidence=0.70)
  # Returns: TradeDecision with size, R:R, expected value, Kelly size

The orchestrator calls this BEFORE every trade to ensure:
  1. Position size is proportional to edge (Kelly criterion)
  2. Risk per trade never exceeds risk_pct of bankroll
  3. Total exposure stays within limits
  4. Withdrawable capital is always protected
"""

import math
import logging
from dataclasses import dataclass, field
from typing import Optional, List

logger = logging.getLogger("polyedge.risk")


# ─── Trade Decision Output ──────────────────────────────────

@dataclass
class TradeDecision:
    """Output of the risk calculator — everything the orchestrator needs."""
    # Should we take this trade?
    approved: bool
    reason: str  # Why approved/rejected

    # Position sizing
    shares: float = 0          # Number of shares to buy
    cost: float = 0            # Total cost ($)
    risk_amount: float = 0     # Max loss ($) if market resolves wrong

    # Reward analysis
    reward_if_win: float = 0   # Profit ($) if market resolves right
    risk_reward_ratio: float = 0  # R:R ratio (reward / risk)
    expected_value: float = 0  # EV = (prob * reward) - ((1-prob) * risk)
    ev_per_dollar: float = 0   # EV per dollar risked

    # Kelly criterion
    kelly_size: float = 0      # Full Kelly position ($)
    kelly_fraction_used: float = 0  # Fraction of Kelly we're using

    # Portfolio context
    bankroll: float = 0
    risk_pct_of_bankroll: float = 0  # What % of bankroll we're risking
    current_exposure: float = 0
    exposure_after: float = 0
    withdrawable_after: float = 0  # Capital safe to withdraw after trade

    # Fees
    estimated_fees: float = 0

    def summary(self) -> str:
        if not self.approved:
            return f"REJECTED: {self.reason}"
        return (
            f"BUY {self.shares:.0f} shares @ ${self.cost:.2f} | "
            f"Risk ${self.risk_amount:.2f} → Reward ${self.reward_if_win:.2f} | "
            f"R:R {self.risk_reward_ratio:.1f}:1 | "
            f"EV ${self.expected_value:.2f} ({self.ev_per_dollar:.1f}¢/$) | "
            f"Kelly {self.kelly_fraction_used:.0%} | "
            f"Withdrawable ${self.withdrawable_after:.2f}"
        )


# ─── Risk Calculator Config ─────────────────────────────────

@dataclass
class RiskConfig:
    """Risk management parameters."""
    # Per-trade limits
    risk_pct: float = 2.0           # Max risk per trade (% of bankroll)
    max_trade_size: float = 75.0    # Hard cap on any single trade ($)
    min_trade_size: float = 5.0     # Minimum trade to be worth fees

    # Kelly criterion
    kelly_fraction: float = 0.25    # Use 25% of full Kelly (conservative)
    min_edge_pct: float = 5.0       # Minimum edge to trade (%)

    # Portfolio limits
    max_exposure_pct: float = 60.0  # Max % of bankroll in active positions
    reserve_pct: float = 20.0       # Always keep 20% liquid (withdrawable)
    max_single_market_pct: float = 15.0  # Max 15% of bankroll in one market
    max_correlated_pct: float = 30.0     # Max 30% in correlated markets

    # Fees
    maker_fee: float = 0.0          # Polymarket: 0% maker
    taker_fee: float = 0.01         # Polymarket: ~1% taker
    exit_slippage: float = 0.02     # Estimated 2% slippage on early exit

    # Entry price tiers (adjust risk by price tier)
    # Cheap shares = higher variance, reduce position
    tier_adjustments: dict = field(default_factory=lambda: {
        "penny": {"max_entry": 0.05, "kelly_mult": 0.5, "label": "Penny (<5¢)"},
        "cheap": {"max_entry": 0.15, "kelly_mult": 0.75, "label": "Cheap (5-15¢)"},
        "mid":   {"max_entry": 0.40, "kelly_mult": 1.0, "label": "Mid (15-40¢)"},
        "fair":  {"max_entry": 0.65, "kelly_mult": 1.0, "label": "Fair (40-65¢)"},
        "heavy": {"max_entry": 1.00, "kelly_mult": 0.75, "label": "Heavy (>65¢)"},
    })


# ─── Profile Presets ─────────────────────────────────────────

RISK_PROFILE_CONFIGS = {
    "conservative": RiskConfig(
        risk_pct=2.0, max_trade_size=25.0, min_trade_size=3.0,
        kelly_fraction=0.15, min_edge_pct=5.0,
        max_exposure_pct=90.0, reserve_pct=5.0,
        max_single_market_pct=25.0, max_correlated_pct=40.0,
    ),
    "balanced": RiskConfig(
        risk_pct=3.0, max_trade_size=50.0, min_trade_size=3.0,
        kelly_fraction=0.25, min_edge_pct=5.0,
        max_exposure_pct=85.0, reserve_pct=10.0,
        max_single_market_pct=25.0, max_correlated_pct=40.0,
    ),
    "aggressive": RiskConfig(
        risk_pct=5.0, max_trade_size=75.0, min_trade_size=5.0,
        kelly_fraction=0.40, min_edge_pct=3.0,
        max_exposure_pct=85.0, reserve_pct=5.0,
        max_single_market_pct=30.0, max_correlated_pct=50.0,
    ),
}


# ─── Risk Calculator ────────────────────────────────────────

class RiskCalculator:
    """
    Polymarket-adapted position sizing calculator.

    Called by the orchestrator before every trade to determine:
    1. Whether to take the trade (edge check)
    2. How much to risk (Kelly + portfolio limits)
    3. Risk/Reward analysis
    4. Impact on withdrawable capital
    """

    def __init__(self, bankroll: float, config: RiskConfig = None,
                 current_exposure: float = 0, free_usdc: float = None):
        self.bankroll = bankroll
        self.config = config or RiskConfig()
        self.current_exposure = current_exposure
        self.free_usdc = free_usdc if free_usdc is not None else bankroll

    @classmethod
    def from_profile(cls, profile_name: str, bankroll: float,
                     free_usdc: float = None, current_exposure: float = 0):
        """Create a RiskCalculator from a named profile."""
        config = RISK_PROFILE_CONFIGS.get(profile_name.lower(), RISK_PROFILE_CONFIGS["balanced"])
        return cls(bankroll=bankroll, config=config,
                   current_exposure=current_exposure, free_usdc=free_usdc)

    def size_trade(
        self,
        entry_price: float,
        confidence: float,
        side: str = "YES",
        market_question: str = "",
        correlated_exposure: float = 0,
    ) -> TradeDecision:
        """
        Calculate optimal position size for a Polymarket trade.

        Args:
            entry_price: Price per share ($0.01-$0.99)
            confidence: Our estimated probability of winning (0.0-1.0)
            side: "YES" or "NO"
            market_question: For logging
            correlated_exposure: $ already in correlated markets

        Returns:
            TradeDecision with approved/rejected + sizing details
        """
        decision = TradeDecision(approved=False, reason="", bankroll=self.bankroll)
        decision.current_exposure = self.current_exposure

        # ── Validate inputs ──
        if entry_price <= 0 or entry_price >= 1.0:
            decision.reason = f"Invalid entry price: ${entry_price:.4f}"
            return decision

        if confidence <= 0 or confidence >= 1.0:
            decision.reason = f"Invalid confidence: {confidence:.0%}"
            return decision

        # ── Calculate edge ──
        # For YES shares: fair_value = confidence, edge = confidence - entry_price
        if side == "YES":
            fair_value = confidence
            edge = fair_value - entry_price
            reward_per_share = 1.0 - entry_price  # Profit if YES wins
            risk_per_share = entry_price            # Loss if YES loses
            win_prob = confidence
        else:
            fair_value = 1.0 - confidence
            edge = fair_value - entry_price
            reward_per_share = 1.0 - entry_price
            risk_per_share = entry_price
            win_prob = 1.0 - confidence

        edge_pct = (edge / entry_price) * 100 if entry_price > 0 else 0

        # ── Edge check ──
        if edge_pct < self.config.min_edge_pct:
            decision.reason = f"Edge too small: {edge_pct:.1f}% < {self.config.min_edge_pct}% min"
            return decision

        # ── Kelly criterion ──
        # Kelly formula for binary outcomes: f* = (p * b - q) / b
        # where p = win_prob, q = 1-p, b = reward/risk ratio
        b = reward_per_share / risk_per_share if risk_per_share > 0 else 0
        q = 1.0 - win_prob
        full_kelly = (win_prob * b - q) / b if b > 0 else 0

        if full_kelly <= 0:
            decision.reason = f"Negative Kelly: {full_kelly:.3f} (no edge)"
            return decision

        # Apply Kelly fraction (conservative)
        tier = self._get_tier(entry_price)
        tier_mult = tier.get("kelly_mult", 1.0) if tier else 1.0
        adjusted_kelly = full_kelly * self.config.kelly_fraction * tier_mult

        kelly_size_dollars = adjusted_kelly * self.bankroll
        decision.kelly_size = kelly_size_dollars

        # ── Apply position limits ──
        # Limit 1: Max risk per trade (% of bankroll)
        max_risk = self.bankroll * (self.config.risk_pct / 100)
        max_cost_from_risk = max_risk  # In Polymarket, max loss = cost

        # Limit 2: Hard cap on trade size
        max_cost_from_cap = self.config.max_trade_size

        # Limit 3: Available USDC
        max_cost_from_balance = self.free_usdc * 0.95  # Keep 5% buffer

        # Limit 4: Reserve protection (always keep reserve_pct liquid)
        min_reserve = self.bankroll * (self.config.reserve_pct / 100)
        max_cost_from_reserve = max(0, self.free_usdc - min_reserve)

        # Limit 5: Exposure limits
        max_exposure_total = self.bankroll * (self.config.max_exposure_pct / 100)
        max_cost_from_exposure = max(0, max_exposure_total - self.current_exposure)

        # Limit 6: Single market concentration
        max_single = self.bankroll * (self.config.max_single_market_pct / 100)

        # Limit 7: Correlated market limit
        max_correlated = self.bankroll * (self.config.max_correlated_pct / 100)
        max_cost_from_corr = max(0, max_correlated - correlated_exposure)

        # Take the most restrictive limit
        max_cost = min(
            kelly_size_dollars,
            max_cost_from_risk,
            max_cost_from_cap,
            max_cost_from_balance,
            max_cost_from_reserve,
            max_cost_from_exposure,
            max_single,
            max_cost_from_corr,
        )

        if max_cost < self.config.min_trade_size:
            limiting = self._find_limiting_factor(
                kelly_size_dollars, max_cost_from_risk, max_cost_from_cap,
                max_cost_from_balance, max_cost_from_reserve,
                max_cost_from_exposure, max_single, max_cost_from_corr
            )
            decision.reason = f"Trade too small after limits: ${max_cost:.2f} (limited by {limiting})"
            return decision

        # ── Calculate final position ──
        cost = round(max_cost, 2)
        shares = cost / entry_price
        fees = cost * self.config.taker_fee
        cost_with_fees = cost + fees

        reward_if_win = shares * reward_per_share - fees
        risk_amount = cost_with_fees  # Max loss = cost + fees
        rr_ratio = reward_if_win / risk_amount if risk_amount > 0 else 0

        # Expected value
        ev = (win_prob * reward_if_win) - (q * risk_amount)
        ev_per_dollar = (ev / cost) * 100 if cost > 0 else 0  # cents per dollar

        # Withdrawable after trade
        exposure_after = self.current_exposure + cost
        withdrawable = self.free_usdc - cost - min_reserve

        # ── Build decision ──
        decision.approved = True
        decision.reason = f"Edge {edge_pct:.1f}%, Kelly {adjusted_kelly:.1%}, tier={tier.get('label', '?')}"
        decision.shares = round(shares, 1)
        decision.cost = cost
        decision.risk_amount = round(risk_amount, 2)
        decision.reward_if_win = round(reward_if_win, 2)
        decision.risk_reward_ratio = round(rr_ratio, 2)
        decision.expected_value = round(ev, 2)
        decision.ev_per_dollar = round(ev_per_dollar, 1)
        decision.kelly_size = round(kelly_size_dollars, 2)
        decision.kelly_fraction_used = round(cost / kelly_size_dollars, 2) if kelly_size_dollars > 0 else 0
        decision.risk_pct_of_bankroll = round((cost / self.bankroll) * 100, 1)
        decision.exposure_after = round(exposure_after, 2)
        decision.withdrawable_after = round(max(0, withdrawable), 2)
        decision.estimated_fees = round(fees, 2)

        logger.info(f"RISK CALC: {decision.summary()}")
        return decision

    def batch_evaluate(
        self,
        opportunities: List[dict],
    ) -> List[TradeDecision]:
        """
        Evaluate multiple trade opportunities and rank by EV/dollar.

        Each opportunity: {entry_price, confidence, side, market_question, correlated_exposure}
        Returns sorted by EV per dollar (best first).
        """
        decisions = []
        running_exposure = self.current_exposure
        running_free = self.free_usdc

        for opp in opportunities:
            # Create temporary calculator with updated exposure
            calc = RiskCalculator(
                bankroll=self.bankroll,
                config=self.config,
                current_exposure=running_exposure,
                free_usdc=running_free,
            )
            decision = calc.size_trade(
                entry_price=opp["entry_price"],
                confidence=opp["confidence"],
                side=opp.get("side", "YES"),
                market_question=opp.get("market_question", ""),
                correlated_exposure=opp.get("correlated_exposure", 0),
            )
            decisions.append(decision)

            # Update running totals for sequential sizing
            if decision.approved:
                running_exposure += decision.cost
                running_free -= decision.cost

        # Sort approved trades by EV/dollar (best first)
        approved = [d for d in decisions if d.approved]
        approved.sort(key=lambda d: d.ev_per_dollar, reverse=True)

        return approved

    def portfolio_risk_report(
        self,
        positions: List[dict],
    ) -> dict:
        """
        Generate portfolio-level risk report.

        Each position: {title, cost, value, entry_price, cur_price, size}
        """
        total_cost = sum(p.get("cost", 0) for p in positions)
        total_value = sum(p.get("value", 0) for p in positions)
        total_pnl = total_value - total_cost

        # Max possible loss (all positions go to $0)
        max_loss = total_value

        # Risk concentration
        biggest_position = max(positions, key=lambda p: p.get("value", 0)) if positions else {}
        concentration = (biggest_position.get("value", 0) / total_value * 100) if total_value > 0 else 0

        # Withdrawable capital
        min_reserve = self.bankroll * (self.config.reserve_pct / 100)
        withdrawable = max(0, self.free_usdc - min_reserve)

        return {
            "bankroll": self.bankroll,
            "equity": total_value + self.free_usdc,
            "total_cost": round(total_cost, 2),
            "total_value": round(total_value, 2),
            "total_pnl": round(total_pnl, 2),
            "pnl_pct": round((total_pnl / total_cost * 100), 1) if total_cost > 0 else 0,
            "free_usdc": round(self.free_usdc, 2),
            "exposure_pct": round((total_cost / self.bankroll * 100), 1) if self.bankroll > 0 else 0,
            "max_possible_loss": round(max_loss, 2),
            "biggest_position": biggest_position.get("title", ""),
            "biggest_position_pct": round(concentration, 1),
            "withdrawable": round(withdrawable, 2),
            "reserve_target": round(min_reserve, 2),
            "positions_count": len(positions),
            "risk_score": self._calculate_risk_score(positions, total_value),
        }

    def _calculate_risk_score(self, positions: list, total_value: float) -> str:
        """Calculate overall risk score: LOW / MEDIUM / HIGH / CRITICAL."""
        exposure_pct = (total_value / self.bankroll * 100) if self.bankroll > 0 else 0

        # Check concentration
        if positions:
            biggest = max(p.get("value", 0) for p in positions)
            concentration = (biggest / total_value * 100) if total_value > 0 else 0
        else:
            concentration = 0

        if exposure_pct > 80 or concentration > 40:
            return "CRITICAL"
        elif exposure_pct > 60 or concentration > 30:
            return "HIGH"
        elif exposure_pct > 40 or concentration > 20:
            return "MEDIUM"
        else:
            return "LOW"

    def _get_tier(self, entry_price: float) -> Optional[dict]:
        """Get the price tier for a given entry price."""
        for key in ["penny", "cheap", "mid", "fair", "heavy"]:
            tier = self.config.tier_adjustments.get(key, {})
            if entry_price <= tier.get("max_entry", 1.0):
                return tier
        return self.config.tier_adjustments.get("heavy", {})

    def _find_limiting_factor(self, kelly, risk, cap, balance, reserve, exposure, single, corr):
        """Find which limit is most restrictive."""
        limits = {
            "kelly": kelly, "risk_pct": risk, "cap": cap,
            "balance": balance, "reserve": reserve,
            "exposure": exposure, "single_market": single, "correlated": corr,
        }
        return min(limits, key=limits.get)
