"""Pre-Trade Intraday Margin Stress Testing Engine (Milestone M23).

Conforms strictly to institutional risk management and SEBI/NSE peak margin regulations:
1. Pure Decimal financial arithmetic with zero binary float drift.
2. Models SEBI Cash Intraday (MIS) peak margin (VaR + ELM base rate = 20%).
3. Simulates deterministic instantaneous market shocks:
   a. Adverse Market Gap (-2.5% instantaneous drop on long equity book).
   b. Volatility Margin Spike (+50% increase in clearing house margin requirements).
   c. Liquidity / Bid-Ask Haircut (0.20% emergency liquidation spread penalty).
   d. Composite Joint Shock combining all three stress vectors.
4. Enforces hard pre-trade gate: Stressed Margin Utilization <= 85% of stressed cash.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from typing import Any

from app.core.execution_guard import assert_paper_mode
from app.core.money import round_paise, round_price, to_decimal


@dataclass(frozen=True)
class StressScenarioResult:
  """Results of evaluating an individual margin stress scenario."""
  scenario_id: str
  scenario_name: str
  description: str
  stressed_notional_inr: Decimal
  stressed_margin_required_inr: Decimal
  mark_to_market_loss_inr: Decimal
  stressed_cash_available_inr: Decimal
  margin_utilization_pct: Decimal
  margin_headroom_inr: Decimal
  breached: bool


@dataclass(frozen=True)
class IntradayMarginStressReport:
  """Institutional multi-scenario margin stress report."""
  timestamp_utc: datetime
  total_equity_inr: Decimal
  available_cash_inr: Decimal
  base_margin_required_inr: Decimal
  base_utilization_pct: Decimal
  composite_stressed_margin_inr: Decimal
  composite_stressed_cash_inr: Decimal
  composite_utilization_pct: Decimal
  composite_headroom_inr: Decimal
  max_allowed_utilization_pct: Decimal
  passed: bool
  rejection_reason: str | None
  scenarios: list[StressScenarioResult]

  def to_dict(self) -> dict[str, Any]:
    return {
        "timestamp_utc": self.timestamp_utc.isoformat(),
        "total_equity_inr": str(self.total_equity_inr),
        "available_cash_inr": str(self.available_cash_inr),
        "base_margin_required_inr": str(self.base_margin_required_inr),
        "base_utilization_pct": str(self.base_utilization_pct),
        "composite_stressed_margin_inr": str(self.composite_stressed_margin_inr),
        "composite_stressed_cash_inr": str(self.composite_stressed_cash_inr),
        "composite_utilization_pct": str(self.composite_utilization_pct),
        "composite_headroom_inr": str(self.composite_headroom_inr),
        "max_allowed_utilization_pct": str(self.max_allowed_utilization_pct),
        "passed": self.passed,
        "rejection_reason": self.rejection_reason,
        "scenarios": [
            {
                "scenario_id": s.scenario_id,
                "scenario_name": s.scenario_name,
                "description": s.description,
                "stressed_notional_inr": str(s.stressed_notional_inr),
                "stressed_margin_required_inr": str(s.stressed_margin_required_inr),
                "mark_to_market_loss_inr": str(s.mark_to_market_loss_inr),
                "stressed_cash_available_inr": str(s.stressed_cash_available_inr),
                "margin_utilization_pct": str(s.margin_utilization_pct),
                "margin_headroom_inr": str(s.margin_headroom_inr),
                "breached": s.breached,
            }
            for s in self.scenarios
        ],
    }


class IntradayMarginStressEngine:
  """Institutional Pre-Trade Margin Stress & Headroom Surveillance Engine."""

  def __init__(
      self,
      base_margin_rate: Decimal = Decimal("0.20"),
      max_stressed_utilization_pct: Decimal = Decimal("0.85"),
      gap_shock_pct: Decimal = Decimal("0.025"),
      vol_expansion_multiplier: Decimal = Decimal("1.50"),
      liquidity_haircut_pct: Decimal = Decimal("0.0020"),
  ):
    assert_paper_mode()
    self.base_margin_rate = to_decimal(base_margin_rate)
    self.max_stressed_utilization_pct = to_decimal(max_stressed_utilization_pct)
    self.gap_shock_pct = to_decimal(gap_shock_pct)
    self.vol_expansion_multiplier = to_decimal(vol_expansion_multiplier)
    self.liquidity_haircut_pct = to_decimal(liquidity_haircut_pct)

  def evaluate_stress(
      self,
      open_positions: dict[str, tuple[int, Decimal]],  # symbol -> (qty, current_price)
      candidate_symbol: str | None = None,
      candidate_qty: int = 0,
      candidate_price: Decimal = Decimal("0.00"),
      total_equity_inr: Decimal = Decimal("100000.00"),
      available_cash_inr: Decimal = Decimal("100000.00"),
  ) -> IntradayMarginStressReport:
    """Evaluates aggregate margin utilization under deterministic stress scenarios.

    Args:
        open_positions: existing active open positions (symbol -> (quantity, price)).
        candidate_symbol: prospective symbol being evaluated for entry.
        candidate_qty: prospective entry quantity.
        candidate_price: prospective entry price in INR.
        total_equity_inr: current total equity balance in INR.
        available_cash_inr: available deployable cash balance in INR.

    Returns:
        IntradayMarginStressReport
    """
    assert_paper_mode()
    equity = to_decimal(total_equity_inr)
    cash = to_decimal(available_cash_inr)

    # Build combined prospective portfolio
    simulated_positions: dict[str, tuple[int, Decimal]] = dict(open_positions)
    if candidate_symbol and candidate_qty > 0 and candidate_price > Decimal("0"):
      existing_qty, _ = simulated_positions.get(candidate_symbol, (0, candidate_price))
      simulated_positions[candidate_symbol] = (
          existing_qty + candidate_qty,
          to_decimal(candidate_price),
      )

    # 1. Base Scenario
    base_notional = Decimal("0.00")
    for s, (qty, p) in simulated_positions.items():
      base_notional += round_paise(Decimal(qty) * p)

    base_margin = round_paise(base_notional * self.base_margin_rate)
    base_utilization = (
        (base_margin / cash * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if cash > Decimal("0")
        else Decimal("100.00")
    )
    base_headroom = round_paise(cash - base_margin)

    base_scenario = StressScenarioResult(
        scenario_id="SCENARIO_BASE",
        scenario_name="Nominal Base Margin",
        description="Current/projected book under standard SEBI MIS intraday margin (20%).",
        stressed_notional_inr=base_notional,
        stressed_margin_required_inr=base_margin,
        mark_to_market_loss_inr=Decimal("0.00"),
        stressed_cash_available_inr=cash,
        margin_utilization_pct=base_utilization,
        margin_headroom_inr=base_headroom,
        breached=base_utilization > (self.max_stressed_utilization_pct * Decimal("100")),
    )

    # 2. Market Gap Shock (-2.5% drop on long book)
    gap_loss = round_paise(base_notional * self.gap_shock_pct)
    gap_notional = round_paise(base_notional * (Decimal("1.000") - self.gap_shock_pct))
    gap_margin = round_paise(gap_notional * self.base_margin_rate)
    gap_cash = max(Decimal("0.00"), cash - gap_loss)
    gap_utilization = (
        (gap_margin / gap_cash * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if gap_cash > Decimal("0")
        else Decimal("999.99")
    )
    gap_headroom = round_paise(gap_cash - gap_margin)

    gap_scenario = StressScenarioResult(
        scenario_id="SCENARIO_GAP_SHOCK",
        scenario_name="Market Gap Shock (-2.5%)",
        description="Instantaneous 250 bps adverse market drop across entire long exposure.",
        stressed_notional_inr=gap_notional,
        stressed_margin_required_inr=gap_margin,
        mark_to_market_loss_inr=gap_loss,
        stressed_cash_available_inr=gap_cash,
        margin_utilization_pct=gap_utilization,
        margin_headroom_inr=gap_headroom,
        breached=gap_utilization > (self.max_stressed_utilization_pct * Decimal("100")),
    )

    # 3. Volatility Margin Spike (+50% margin requirement)
    vol_margin_rate = self.base_margin_rate * self.vol_expansion_multiplier
    vol_margin = round_paise(base_notional * vol_margin_rate)
    vol_utilization = (
        (vol_margin / cash * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if cash > Decimal("0")
        else Decimal("999.99")
    )
    vol_headroom = round_paise(cash - vol_margin)

    vol_scenario = StressScenarioResult(
        scenario_id="SCENARIO_VOLATILITY_EXPANSION",
        scenario_name="Clearing House Margin Spike (+50%)",
        description="Exchange increases VaR+ELM intraday margin from 20% to 30% due to volatility.",
        stressed_notional_inr=base_notional,
        stressed_margin_required_inr=vol_margin,
        mark_to_market_loss_inr=Decimal("0.00"),
        stressed_cash_available_inr=cash,
        margin_utilization_pct=vol_utilization,
        margin_headroom_inr=vol_headroom,
        breached=vol_utilization > (self.max_stressed_utilization_pct * Decimal("100")),
    )

    # 4. Bid-Ask Liquidity / Slippage Haircut (20 bps)
    haircut_loss = round_paise(base_notional * self.liquidity_haircut_pct)
    haircut_cash = max(Decimal("0.00"), cash - haircut_loss)
    haircut_margin = base_margin
    haircut_utilization = (
        (haircut_margin / haircut_cash * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if haircut_cash > Decimal("0")
        else Decimal("999.99")
    )
    haircut_headroom = round_paise(haircut_cash - haircut_margin)

    haircut_scenario = StressScenarioResult(
        scenario_id="SCENARIO_LIQUIDITY_HAIRCUT",
        scenario_name="Emergency Liquidity Haircut (0.20%)",
        description="20 bps bid-ask widening and execution slippage penalty during fire-sale liquidation.",
        stressed_notional_inr=base_notional,
        stressed_margin_required_inr=haircut_margin,
        mark_to_market_loss_inr=haircut_loss,
        stressed_cash_available_inr=haircut_cash,
        margin_utilization_pct=haircut_utilization,
        margin_headroom_inr=haircut_headroom,
        breached=haircut_utilization > (self.max_stressed_utilization_pct * Decimal("100")),
    )

    # 5. Composite Joint Shock (Gap -2.5% + Vol Spike +50% + Haircut 0.20%)
    composite_loss = gap_loss + haircut_loss
    composite_cash = max(Decimal("0.00"), cash - composite_loss)
    composite_margin = round_paise(gap_notional * vol_margin_rate)
    composite_utilization = (
        (composite_margin / composite_cash * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if composite_cash > Decimal("0")
        else Decimal("999.99")
    )
    composite_headroom = round_paise(composite_cash - composite_margin)
    max_allowed_util = (self.max_stressed_utilization_pct * Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    composite_breached = composite_utilization > max_allowed_util or composite_cash <= Decimal("0")

    composite_scenario = StressScenarioResult(
        scenario_id="SCENARIO_COMPOSITE_STRESS",
        scenario_name="Composite Worst-Case Stress",
        description="Joint stress: -2.5% price gap, +50% margin multiplier, and 20 bps liquidity haircut.",
        stressed_notional_inr=gap_notional,
        stressed_margin_required_inr=composite_margin,
        mark_to_market_loss_inr=composite_loss,
        stressed_cash_available_inr=composite_cash,
        margin_utilization_pct=composite_utilization,
        margin_headroom_inr=composite_headroom,
        breached=composite_breached,
    )

    scenarios = [
        base_scenario,
        gap_scenario,
        vol_scenario,
        haircut_scenario,
        composite_scenario,
    ]

    passed = not composite_breached
    rejection_reason = (
        None
        if passed
        else (
            f"Stressed margin utilization {composite_utilization}% exceeds safety threshold "
            f"{max_allowed_util}% under composite shock (Headroom: ₹{composite_headroom})."
        )
    )

    return IntradayMarginStressReport(
        timestamp_utc=datetime.now(timezone.utc),
        total_equity_inr=equity,
        available_cash_inr=cash,
        base_margin_required_inr=base_margin,
        base_utilization_pct=base_utilization,
        composite_stressed_margin_inr=composite_margin,
        composite_stressed_cash_inr=composite_cash,
        composite_utilization_pct=composite_utilization,
        composite_headroom_inr=composite_headroom,
        max_allowed_utilization_pct=max_allowed_util,
        passed=passed,
        rejection_reason=rejection_reason,
        scenarios=scenarios,
    )
