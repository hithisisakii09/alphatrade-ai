"""Dynamic Execution Cost, Square-Root Market Impact & Implementation Shortfall Analytics.

Conforms strictly to quantitative institutional execution standards (Almgren-Chriss / Square-Root Law).
Implements:
1. Pure Decimal arithmetic with zero binary float drift.
2. Square-Root Law of Market Impact (Barone-Adesi, Engle, Ferstenberg & Russell 2008).
3. Temporary & Permanent Price Impact Decomposition.
4. Participation Rate Liquidity Guard (Q / V <= max_participation_rate).
5. Implementation Shortfall Benchmark vs Arrival Price (Perold 1988).
6. Real-Time Slippage Analytics across historical order fills.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
import sqlite3
from typing import Any

from app.core.money import round_paise, round_price, to_decimal


@dataclass(frozen=True)
class MarketImpactBreakdown:
  """Institutional transaction cost analysis (TCA) and market impact metrics."""
  symbol: str
  side: str
  quantity: int
  raw_arrival_price: Decimal
  reference_volume: int
  participation_rate_pct: Decimal
  daily_volatility_pct: Decimal
  temporary_impact_pct: Decimal
  temporary_impact_inr: Decimal
  permanent_impact_pct: Decimal
  permanent_impact_inr: Decimal
  total_impact_pct: Decimal
  total_impact_bps: Decimal
  effective_execution_price: Decimal
  implementation_shortfall_inr: Decimal
  is_participation_compliant: bool
  max_allowed_participation_pct: Decimal


@dataclass(frozen=True)
class ExecutionQualitySummary:
  """Summary metrics evaluating institutional order execution quality."""
  total_orders_analyzed: int
  avg_slippage_bps: Decimal
  total_implementation_shortfall_inr: Decimal
  avg_participation_rate_pct: Decimal
  total_fees_and_impact_inr: Decimal
  by_symbol: dict[str, dict[str, Any]]


def compute_square_root_market_impact(
    side: str,
    quantity: int,
    raw_price: Decimal,
    reference_volume: int,
    daily_volatility: Decimal = Decimal("0.015"),  # 1.5% daily vol
    eta: Decimal = Decimal("0.5"),  # Temporary impact coefficient
    gamma: Decimal = Decimal("0.25"),  # Permanent impact coefficient
    half_spread_pct: Decimal = Decimal("0.00025"),  # 2.5 bps
    max_participation_pct: Decimal = Decimal("0.05"),  # 5% max bar volume
) -> MarketImpactBreakdown:
  """Computes dynamic market impact using the Square-Root Law under Almgren-Chriss framework.

  Temporary Impact = eta * sigma * sqrt(Q / V)
  Permanent Impact = gamma * sigma * sqrt(Q / V)
  Total Impact = (eta + gamma) * sigma * sqrt(Q / V)
  """
  side_u = side.upper()
  if side_u not in ("BUY", "SELL"):
    raise ValueError(f"Invalid order side: {side}")
  if quantity <= 0:
    raise ValueError("Quantity must be > 0")

  q = Decimal(quantity)
  p = to_decimal(raw_price)
  v = Decimal(max(1, reference_volume))
  sig = to_decimal(daily_volatility)

  # Participation rate = Q / V
  participation_ratio = q / v
  participation_pct = participation_ratio * Decimal("100")
  is_compliant = participation_ratio <= max_participation_pct

  # Square-root participation factor: sqrt(Q / V)
  sqrt_part = participation_ratio.sqrt()

  temp_impact_pct = eta * sig * sqrt_part
  perm_impact_pct = gamma * sig * sqrt_part
  total_impact_pct = temp_impact_pct + perm_impact_pct
  total_impact_bps = total_impact_pct * Decimal("10000")

  temp_impact_inr = round_paise(p * temp_impact_pct * q)
  perm_impact_inr = round_paise(p * perm_impact_pct * q)

  friction_pct = half_spread_pct + total_impact_pct
  if side_u == "BUY":
    eff_price = round_price(p * (Decimal("1") + friction_pct))
    # Implementation shortfall = (Fill - Arrival) * Q
    shortfall = round_paise((eff_price - p) * q)
  else:
    eff_price = round_price(p * (Decimal("1") - friction_pct))
    # Implementation shortfall = (Arrival - Fill) * Q
    shortfall = round_paise((p - eff_price) * q)

  return MarketImpactBreakdown(
      symbol="",
      side=side_u,
      quantity=quantity,
      raw_arrival_price=p,
      reference_volume=reference_volume,
      participation_rate_pct=round_price(participation_pct),
      daily_volatility_pct=round_price(sig * Decimal("100")),
      temporary_impact_pct=round_price(temp_impact_pct),
      temporary_impact_inr=temp_impact_inr,
      permanent_impact_pct=round_price(perm_impact_pct),
      permanent_impact_inr=perm_impact_inr,
      total_impact_pct=round_price(total_impact_pct),
      total_impact_bps=round_price(total_impact_bps),
      effective_execution_price=eff_price,
      implementation_shortfall_inr=shortfall,
      is_participation_compliant=is_compliant,
      max_allowed_participation_pct=round_price(max_participation_pct * Decimal("100")),
  )


class ExecutionAnalyticsEngine:
  """Aggregates execution quality and market impact from SQLite orders and fills."""

  def __init__(self, db_path: str = "alphatrade_v2.db"):
    self.db_path = db_path

  def get_average_bar_volume(self, symbol: str, lookback_bars: int = 1500) -> int:
    """Queries average 1m bar volume from SQLite."""
    conn = sqlite3.connect(self.db_path)
    c = conn.cursor()
    c.execute(
        """
        SELECT AVG(volume) FROM (
            SELECT volume FROM market_candles 
            WHERE symbol = ? AND interval_minutes = 1
            ORDER BY timestamp_utc DESC LIMIT ?
        )
        """,
        (symbol, lookback_bars),
    )
    res = c.fetchone()
    conn.close()
    if res and res[0] is not None:
      return max(1000, int(res[0]))
    return 10000  # Default 10k shares fallback

  def compute_order_market_impact(
      self,
      symbol: str,
      side: str,
      quantity: int,
      raw_price: Decimal,
      daily_volatility: Decimal | None = None,
      eta: Decimal = Decimal("0.5"),
      gamma: Decimal = Decimal("0.25"),
      max_participation_pct: Decimal = Decimal("0.05"),
  ) -> MarketImpactBreakdown:
    """Computes dynamic market impact using actual database bar volume."""
    avg_vol = self.get_average_bar_volume(symbol)
    vol_dec = daily_volatility or Decimal("0.015")
    breakdown = compute_square_root_market_impact(
        side=side,
        quantity=quantity,
        raw_price=raw_price,
        reference_volume=avg_vol,
        daily_volatility=vol_dec,
        eta=eta,
        gamma=gamma,
        max_participation_pct=max_participation_pct,
    )
    # Return with symbol populated
    return MarketImpactBreakdown(
        symbol=symbol,
        side=breakdown.side,
        quantity=breakdown.quantity,
        raw_arrival_price=breakdown.raw_arrival_price,
        reference_volume=breakdown.reference_volume,
        participation_rate_pct=breakdown.participation_rate_pct,
        daily_volatility_pct=breakdown.daily_volatility_pct,
        temporary_impact_pct=breakdown.temporary_impact_pct,
        temporary_impact_inr=breakdown.temporary_impact_inr,
        permanent_impact_pct=breakdown.permanent_impact_pct,
        permanent_impact_inr=breakdown.permanent_impact_inr,
        total_impact_pct=breakdown.total_impact_pct,
        total_impact_bps=breakdown.total_impact_bps,
        effective_execution_price=breakdown.effective_execution_price,
        implementation_shortfall_inr=breakdown.implementation_shortfall_inr,
        is_participation_compliant=breakdown.is_participation_compliant,
        max_allowed_participation_pct=breakdown.max_allowed_participation_pct,
    )

  def analyze_historical_executions(self) -> ExecutionQualitySummary:
    """Analyzes execution quality across all completed orders in SQLite."""
    conn = sqlite3.connect(self.db_path)
    c = conn.cursor()
    c.execute(
        """
        SELECT o.symbol, o.side, o.requested_qty, o.filled_qty, o.reference_price, 
               f.fill_price, f.total_fees_inr
        FROM orders o
        JOIN fills f ON o.order_id = f.order_id
        WHERE o.status = 'FILLED'
        """
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
      zero = Decimal("0.0")
      return ExecutionQualitySummary(
          total_orders_analyzed=0,
          avg_slippage_bps=zero,
          total_implementation_shortfall_inr=zero,
          avg_participation_rate_pct=zero,
          total_fees_and_impact_inr=zero,
          by_symbol={},
      )

    total_orders = len(rows)
    total_shortfall = Decimal("0.0")
    total_fees = Decimal("0.0")
    slippage_bps_list: list[Decimal] = []
    participation_list: list[Decimal] = []
    by_symbol_agg: dict[str, dict[str, Any]] = {}

    for sym, side, req_q, fill_q, ref_p, fill_p, fees in rows:
      p_ref = Decimal(str(ref_p))
      p_fill = Decimal(str(fill_p))
      qty = int(fill_q)
      fees_inr = Decimal(str(fees))
      turnover = p_fill * Decimal(qty)

      # Slippage = |fill - ref| / ref
      if p_ref > Decimal("0"):
        slip_ratio = abs(p_fill - p_ref) / p_ref
        slip_bps = slip_ratio * Decimal("10000")
        slippage_bps_list.append(slip_bps)
      else:
        slip_bps = Decimal("0.0")

      # Implementation Shortfall
      if side.upper() == "BUY":
        shortfall = (p_fill - p_ref) * Decimal(qty)
      else:
        shortfall = (p_ref - p_fill) * Decimal(qty)

      total_shortfall += shortfall
      total_fees += fees_inr

      # Estimated participation against average bar volume
      avg_bar_v = self.get_average_bar_volume(sym)
      part_pct = (Decimal(qty) / Decimal(avg_bar_v)) * Decimal("100")
      participation_list.append(part_pct)

      # Per-symbol aggregation
      sym_entry = by_symbol_agg.setdefault(
          sym,
          {
              "orders_count": 0,
              "total_shares": 0,
              "total_turnover_inr": Decimal("0.0"),
              "total_fees_inr": Decimal("0.0"),
              "total_shortfall_inr": Decimal("0.0"),
              "slippage_bps_sum": Decimal("0.0"),
          },
      )
      sym_entry["orders_count"] += 1
      sym_entry["total_shares"] += qty
      sym_entry["total_turnover_inr"] += Decimal(str(turnover))
      sym_entry["total_fees_inr"] += fees_inr
      sym_entry["total_shortfall_inr"] += shortfall
      sym_entry["slippage_bps_sum"] += slip_bps

    avg_slip = (
        sum(slippage_bps_list) / Decimal(len(slippage_bps_list))
        if slippage_bps_list
        else Decimal("0.0")
    )
    avg_part = (
        sum(participation_list) / Decimal(len(participation_list))
        if participation_list
        else Decimal("0.0")
    )

    formatted_by_symbol = {}
    for s, data in by_symbol_agg.items():
      cnt = data["orders_count"]
      formatted_by_symbol[s] = {
          "orders_count": cnt,
          "total_shares": data["total_shares"],
          "total_turnover_inr": str(round_paise(data["total_turnover_inr"])),
          "total_fees_inr": str(round_paise(data["total_fees_inr"])),
          "implementation_shortfall_inr": str(round_paise(data["total_shortfall_inr"])),
          "avg_slippage_bps": str(round_price(data["slippage_bps_sum"] / Decimal(cnt))),
      }

    return ExecutionQualitySummary(
        total_orders_analyzed=total_orders,
        avg_slippage_bps=round_price(avg_slip),
        total_implementation_shortfall_inr=round_paise(total_shortfall),
        avg_participation_rate_pct=round_price(avg_part),
        total_fees_and_impact_inr=round_paise(total_fees + total_shortfall),
        by_symbol=formatted_by_symbol,
    )
