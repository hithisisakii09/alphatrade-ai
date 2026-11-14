"""Transaction Cost Analysis (TCA) & Implementation Shortfall Engine (Milestone M25).

Conforms strictly to institutional execution benchmarking standards (Perold 1988):
1. Pure Decimal financial arithmetic with zero binary float drift.
2. Full Implementation Shortfall (IS) decomposition:
   - Execution / Market Impact Cost (Arrival vs Executed Price)
   - Delay / Latency Cost (Decision vs First Fill Price)
   - Opportunity Cost (Decision vs Final Price on Unfilled Quantity)
   - Statutory Fees & Brokerage Impact
3. Execution Benchmark Attribution:
   - Arrival Price Benchmark
   - Interval Volume-Weighted Average Price (VWAP)
   - Interval Time-Weighted Average Price (TWAP)
4. Strict fail-closed paper trading guard (assert_paper_mode()).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from typing import Any

from app.core.execution_guard import assert_paper_mode
from app.core.money import round_paise, round_price, to_decimal


@dataclass(frozen=True)
class ExecutionSliceFill:
  """Single fill record for an execution slice."""
  slice_id: str
  quantity: int
  price: Decimal
  timestamp_utc: datetime
  fee_inr: Decimal = Decimal("0.00")


@dataclass(frozen=True)
class ImplementationShortfallBreakdown:
  """Detailed institutional Perold Implementation Shortfall decomposition."""
  symbol: str
  side: str
  total_ordered_quantity: int
  total_filled_quantity: int
  unfilled_quantity: int
  decision_price: Decimal
  arrival_price: Decimal
  average_execution_price: Decimal
  first_fill_price: Decimal
  final_market_price: Decimal
  interval_vwap: Decimal
  interval_twap: Decimal

  # Monetary metrics (INR)
  market_impact_inr: Decimal
  delay_cost_inr: Decimal
  opportunity_cost_inr: Decimal
  total_fees_inr: Decimal
  total_shortfall_inr: Decimal

  # Normalized metrics (basis points relative to decision notional)
  market_impact_bps: Decimal
  delay_cost_bps: Decimal
  opportunity_cost_bps: Decimal
  fees_bps: Decimal
  total_shortfall_bps: Decimal

  # Benchmark Outperformance / Underperformance (bps)
  # Negative means favorable (bought cheaper / sold higher than benchmark)
  vwap_shortfall_bps: Decimal
  twap_shortfall_bps: Decimal

  timestamp_utc: datetime

  def to_dict(self) -> dict[str, Any]:
    return {
        "symbol": self.symbol,
        "side": self.side,
        "total_ordered_quantity": self.total_ordered_quantity,
        "total_filled_quantity": self.total_filled_quantity,
        "unfilled_quantity": self.unfilled_quantity,
        "decision_price": str(self.decision_price),
        "arrival_price": str(self.arrival_price),
        "average_execution_price": str(self.average_execution_price),
        "first_fill_price": str(self.first_fill_price),
        "final_market_price": str(self.final_market_price),
        "interval_vwap": str(self.interval_vwap),
        "interval_twap": str(self.interval_twap),
        "market_impact_inr": str(self.market_impact_inr),
        "delay_cost_inr": str(self.delay_cost_inr),
        "opportunity_cost_inr": str(self.opportunity_cost_inr),
        "total_fees_inr": str(self.total_fees_inr),
        "total_shortfall_inr": str(self.total_shortfall_inr),
        "market_impact_bps": str(self.market_impact_bps),
        "delay_cost_bps": str(self.delay_cost_bps),
        "opportunity_cost_bps": str(self.opportunity_cost_bps),
        "fees_bps": str(self.fees_bps),
        "total_shortfall_bps": str(self.total_shortfall_bps),
        "vwap_shortfall_bps": str(self.vwap_shortfall_bps),
        "twap_shortfall_bps": str(self.twap_shortfall_bps),
        "timestamp_utc": self.timestamp_utc.isoformat(),
    }


def compute_implementation_shortfall(
    symbol: str,
    side: str,
    total_ordered_quantity: int,
    decision_price: Decimal,
    arrival_price: Decimal,
    fills: list[ExecutionSliceFill],
    final_market_price: Decimal | None = None,
    interval_vwap: Decimal | None = None,
    interval_twap: Decimal | None = None,
    timestamp_utc: datetime | None = None,
) -> ImplementationShortfallBreakdown:
  """Computes institutional Perold (1988) Implementation Shortfall attribution."""
  assert_paper_mode()
  now = timestamp_utc or datetime.now(timezone.utc)
  side_clean = side.upper()
  side_mult = Decimal("1") if side_clean == "BUY" else Decimal("-1")

  dec_p = to_decimal(decision_price)
  arr_p = to_decimal(arrival_price)
  fin_p = to_decimal(final_market_price or arr_p)
  vwap_p = to_decimal(interval_vwap or arr_p)
  twap_p = to_decimal(interval_twap or arr_p)

  total_filled = sum(f.quantity for f in fills)
  unfilled_qty = max(0, total_ordered_quantity - total_filled)
  total_fees = sum((f.fee_inr for f in fills), Decimal("0.00"))

  if total_filled > 0:
    total_notional = sum(Decimal(f.quantity) * f.price for f in fills)
    avg_exec_p = round_price(total_notional / Decimal(total_filled))
    first_p = fills[0].price
  else:
    avg_exec_p = arr_p
    first_p = arr_p

  # Monetary breakdown (INR)
  # 1. Market Impact / Execution Cost: (AvgExec - Arrival) * FilledQty * side_mult
  market_impact_inr = round_paise((avg_exec_p - arr_p) * Decimal(total_filled) * side_mult)

  # 2. Delay Cost: (FirstFill - Decision) * FilledQty * side_mult
  delay_cost_inr = round_paise((first_p - dec_p) * Decimal(total_filled) * side_mult)

  # 3. Opportunity Cost: (Final - Decision) * UnfilledQty * side_mult
  opportunity_cost_inr = round_paise((fin_p - dec_p) * Decimal(unfilled_qty) * side_mult)

  # 4. Total Implementation Shortfall
  total_shortfall_inr = round_paise(
      ((avg_exec_p - dec_p) * Decimal(total_filled) * side_mult)
      + opportunity_cost_inr
      + total_fees
  )

  # Basis points relative to Decision Notional
  decision_notional = dec_p * Decimal(total_ordered_quantity)
  if decision_notional > Decimal("0"):
    bps_factor = Decimal("10000") / decision_notional
    impact_bps = (market_impact_inr * bps_factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    delay_bps = (delay_cost_inr * bps_factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    opp_bps = (opportunity_cost_inr * bps_factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    fees_bps = (total_fees * bps_factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    total_bps = (total_shortfall_inr * bps_factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
  else:
    impact_bps = delay_bps = opp_bps = fees_bps = total_bps = Decimal("0.00")

  # Benchmark Outperformance/Underperformance (bps)
  # (AvgExec - Benchmark) / Benchmark * 10,000 * side_mult
  def _calc_bench_bps(bench: Decimal) -> Decimal:
    if bench > Decimal("0") and total_filled > 0:
      return (((avg_exec_p - bench) / bench) * Decimal("10000") * side_mult).quantize(
          Decimal("0.01"), rounding=ROUND_HALF_UP
      )
    return Decimal("0.00")

  vwap_shortfall_bps = _calc_bench_bps(vwap_p)
  twap_shortfall_bps = _calc_bench_bps(twap_p)

  return ImplementationShortfallBreakdown(
      symbol=symbol,
      side=side_clean,
      total_ordered_quantity=total_ordered_quantity,
      total_filled_quantity=total_filled,
      unfilled_quantity=unfilled_qty,
      decision_price=dec_p,
      arrival_price=arr_p,
      average_execution_price=avg_exec_p,
      first_fill_price=first_p,
      final_market_price=fin_p,
      interval_vwap=vwap_p,
      interval_twap=twap_p,
      market_impact_inr=market_impact_inr,
      delay_cost_inr=delay_cost_inr,
      opportunity_cost_inr=opportunity_cost_inr,
      total_fees_inr=total_fees,
      total_shortfall_inr=total_shortfall_inr,
      market_impact_bps=impact_bps,
      delay_cost_bps=delay_bps,
      opportunity_cost_bps=opp_bps,
      fees_bps=fees_bps,
      total_shortfall_bps=total_bps,
      vwap_shortfall_bps=vwap_shortfall_bps,
      twap_shortfall_bps=twap_shortfall_bps,
      timestamp_utc=now,
  )
