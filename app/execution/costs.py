"""NSE India Intraday Cash Equity Statutory & Brokerage Cost Calculator."""

from dataclasses import dataclass
from decimal import Decimal
from app.core.config import CostProfileConfig
from app.core.money import round_paise, round_price, to_decimal


@dataclass(frozen=True)
class LegCostBreakdown:
  side: str
  quantity: int
  raw_price: Decimal
  effective_fill_price: Decimal
  turnover_inr: Decimal
  spread_cost_inr: Decimal
  slippage_cost_inr: Decimal
  brokerage_inr: Decimal
  stt_inr: Decimal
  exchange_txn_inr: Decimal
  sebi_and_ipft_inr: Decimal
  stamp_duty_inr: Decimal
  gst_inr: Decimal
  statutory_taxes_inr: Decimal
  total_fees_inr: Decimal


class NSECostEngine:
  """Calculates buy-side and sell-side statutory fees, brokerage, spread, and slippage."""

  def __init__(self, profile: CostProfileConfig):
    self.profile = profile

  def compute_leg_costs(
      self,
      side: str,
      quantity: int,
      raw_price: Decimal,
      custom_slippage_pct: Decimal | None = None,
      bar_volume: int | None = None,
      daily_volatility: Decimal | None = None,
  ) -> LegCostBreakdown:
    side_u = side.upper()
    if side_u not in ("BUY", "SELL"):
      raise ValueError(f"Invalid order side: {side}")
    if quantity <= 0:
      raise ValueError("Quantity must be > 0")

    raw_p = to_decimal(raw_price)
    qty_d = Decimal(quantity)
    half_spread = self.profile.half_spread_pct

    if getattr(self.profile, "enable_market_impact", False) and bar_volume is not None and bar_volume > 0:
      sig = daily_volatility or Decimal("0.015")
      eta = getattr(self.profile, "impact_coefficient_eta", Decimal("0.5"))
      gamma = getattr(self.profile, "impact_coefficient_gamma", Decimal("0.25"))
      sqrt_part = (qty_d / Decimal(bar_volume)).sqrt()
      slip_pct = (eta + gamma) * sig * sqrt_part
    else:
      slip_pct = to_decimal(custom_slippage_pct) if custom_slippage_pct is not None else self.profile.slippage_pct

    friction_pct = half_spread + slip_pct

    if side_u == "BUY":
      eff_price = round_price(raw_p * (Decimal("1") + friction_pct))
    else:
      eff_price = round_price(raw_p * (Decimal("1") - friction_pct))

    turnover = round_paise(eff_price * qty_d)
    spread_cost = round_paise(raw_p * half_spread * qty_d)
    slippage_cost = round_paise(raw_p * slip_pct * qty_d)

    # Brokerage: min(flat_cap, turnover * brokerage_pct)
    raw_brok = turnover * self.profile.brokerage_pct
    if self.profile.brokerage_flat_cap_inr > Decimal("0"):
      brokerage = round_paise(min(self.profile.brokerage_flat_cap_inr, raw_brok))
    else:
      brokerage = round_paise(raw_brok)

    # STT: 0.025% on sell side only for NSE intraday cash equity
    stt_rate = self.profile.stt_buy_pct if side_u == "BUY" else self.profile.stt_sell_pct
    stt = round_paise(turnover * stt_rate)

    # Exchange txn charges + SEBI + IPFT
    exch_txn = round_paise(turnover * self.profile.exchange_txn_pct)
    sebi_ipft = round_paise(turnover * (self.profile.sebi_turnover_pct + self.profile.ipft_pct))

    # Stamp duty: 0.003% on buy side only
    stamp_rate = self.profile.stamp_duty_buy_pct if side_u == "BUY" else self.profile.stamp_duty_sell_pct
    stamp = round_paise(turnover * stamp_rate)

    # GST: 18% on (brokerage + exchange_txn + sebi_ipft)
    gst = round_paise((brokerage + exch_txn + sebi_ipft) * self.profile.gst_pct)

    statutory_taxes = round_paise(stt + exch_txn + sebi_ipft + stamp + gst)
    total_fees = round_paise(brokerage + statutory_taxes)

    return LegCostBreakdown(
        side=side_u,
        quantity=quantity,
        raw_price=raw_p,
        effective_fill_price=eff_price,
        turnover_inr=turnover,
        spread_cost_inr=spread_cost,
        slippage_cost_inr=slippage_cost,
        brokerage_inr=brokerage,
        stt_inr=stt,
        exchange_txn_inr=exch_txn,
        sebi_and_ipft_inr=sebi_ipft,
        stamp_duty_inr=stamp,
        gst_inr=gst,
        statutory_taxes_inr=statutory_taxes,
        total_fees_inr=total_fees,
    )

  def estimate_roundtrip_cost(
      self,
      quantity: int,
      entry_price: Decimal,
      exit_price: Decimal | None = None,
      bar_volume: int | None = None,
      daily_volatility: Decimal | None = None,
  ) -> Decimal:
    """Returns total round-trip statutory + brokerage + spread + slippage cost in INR."""
    ep = to_decimal(entry_price)
    xp = to_decimal(exit_price) if exit_price is not None else ep
    buy_leg = self.compute_leg_costs(
        "BUY", quantity, ep, bar_volume=bar_volume, daily_volatility=daily_volatility
    )
    sell_leg = self.compute_leg_costs(
        "SELL", quantity, xp, bar_volume=bar_volume, daily_volatility=daily_volatility
    )
    return round_paise(buy_leg.total_fees_inr + sell_leg.total_fees_inr)
