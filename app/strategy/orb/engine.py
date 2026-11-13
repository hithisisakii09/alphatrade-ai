"""Deterministic Long-Only Opening Range Breakout (ORB) Strategy Engine."""

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from app.core.clock import to_ist
from app.core.config import StrategyConfigModel
from app.data.instruments import InstrumentRegistry
from app.data.models import CandleBar
from app.market.indicators import (
    OpeningRangeState,
    check_macro_trend_alignment,
    check_two_day_high_breakout,
    check_vwap_breakout_exhaustion,
)
from app.market.regime import MarketRegime, RegimeState


@dataclass(frozen=True)
class ORBSignalEvaluation:
  symbol: str
  session_date: str
  timestamp_utc: datetime
  triggered: bool
  signal_id: str | None
  signal_price: Decimal
  orb_high: Decimal | None
  orb_low: Decimal | None
  vwap: Decimal
  rvol: Decimal
  rejection_reason: str | None
  explanation: str


class ORBStrategyEngine:
  """Evaluates 5-minute closes against the 15-minute Opening Range and market/eligibility filters."""

  def __init__(self, config: StrategyConfigModel, registry: InstrumentRegistry):
    self.config = config
    self.registry = registry
    self.cutoff_time = time.fromisoformat(config.entry_cutoff_ist)

  def is_nifty_strongly_bearish(
      self,
      nifty_close: Decimal,
      nifty_vwap: Decimal,
      nifty_orb_low: Decimal,
  ) -> bool:
    """Formal NIFTY 50 strongly bearish condition: price is strictly below BOTH session VWAP and 15m ORB Low."""
    return nifty_close < nifty_vwap and nifty_close < nifty_orb_low

  def evaluate_5m_bar(
      self,
      bar_5m: CandleBar,
      orb_state: OpeningRangeState | None,
      vwap: Decimal,
      rvol: Decimal,
      estimated_spread_pct: Decimal,
      prev_daily_close: Decimal,
      nifty_strongly_bearish: bool = False,
      htf_ema: Decimal | None = None,
      prev_day_high: Decimal | None = None,
      vwap_std: Decimal | None = None,
      regime_state: RegimeState | None = None,
  ) -> ORBSignalEvaluation:
    dt_ist = to_ist(bar_5m.timestamp_utc)
    session_date = dt_ist.date().isoformat()
    sym = bar_5m.symbol

    # 1. Instrument eligibility & liquidity check
    eligible, elig_reason = self.registry.is_eligible(sym, self.config.min_daily_turnover_cr)
    if not eligible:
      return ORBSignalEvaluation(
          symbol=sym,
          session_date=session_date,
          timestamp_utc=bar_5m.timestamp_utc,
          triggered=False,
          signal_id=None,
          signal_price=bar_5m.close,
          orb_high=orb_state.orb_high if orb_state else None,
          orb_low=orb_state.orb_low if orb_state else None,
          vwap=vwap,
          rvol=rvol,
          rejection_reason=elig_reason,
          explanation=f"Rejected by instrument eligibility: {elig_reason}",
      )

    # 2. Opening range completion check
    if orb_state is None or not orb_state.is_complete:
      return ORBSignalEvaluation(
          symbol=sym,
          session_date=session_date,
          timestamp_utc=bar_5m.timestamp_utc,
          triggered=False,
          signal_id=None,
          signal_price=bar_5m.close,
          orb_high=None,
          orb_low=None,
          vwap=vwap,
          rvol=rvol,
          rejection_reason="ORB_NOT_COMPLETE",
          explanation="15-minute Opening Range (09:15-09:30 IST) is not yet complete.",
      )

    # 3. Entry cutoff window check (no entries at or after 14:30 IST)
    if dt_ist.time() >= self.cutoff_time:
      return ORBSignalEvaluation(
          symbol=sym,
          session_date=session_date,
          timestamp_utc=bar_5m.timestamp_utc,
          triggered=False,
          signal_id=None,
          signal_price=bar_5m.close,
          orb_high=orb_state.orb_high,
          orb_low=orb_state.orb_low,
          vwap=vwap,
          rvol=rvol,
          rejection_reason="PAST_ENTRY_CUTOFF_1430",
          explanation=f"Signal time {dt_ist.time()} is at or after 14:30 IST entry cutoff.",
      )

    # 4. NIFTY 50 strongly bearish market regime filter
    if nifty_strongly_bearish:
      return ORBSignalEvaluation(
          symbol=sym,
          session_date=session_date,
          timestamp_utc=bar_5m.timestamp_utc,
          triggered=False,
          signal_id=None,
          signal_price=bar_5m.close,
          orb_high=orb_state.orb_high,
          orb_low=orb_state.orb_low,
          vwap=vwap,
          rvol=rvol,
          rejection_reason="NIFTY_STRONGLY_BEARISH",
          explanation="NIFTY 50 is below both session VWAP and 15m ORB Low.",
      )

    # 5. Breakout confirmation: 5m candle close strictly above Opening Range High
    if bar_5m.close <= orb_state.orb_high:
      return ORBSignalEvaluation(
          symbol=sym,
          session_date=session_date,
          timestamp_utc=bar_5m.timestamp_utc,
          triggered=False,
          signal_id=None,
          signal_price=bar_5m.close,
          orb_high=orb_state.orb_high,
          orb_low=orb_state.orb_low,
          vwap=vwap,
          rvol=rvol,
          rejection_reason="CLOSE_NOT_ABOVE_ORB_HIGH",
          explanation=f"Close {bar_5m.close} <= ORB High {orb_state.orb_high}.",
      )

    # 6. VWAP confirmation: price strictly above session VWAP
    if bar_5m.close <= vwap:
      return ORBSignalEvaluation(
          symbol=sym,
          session_date=session_date,
          timestamp_utc=bar_5m.timestamp_utc,
          triggered=False,
          signal_id=None,
          signal_price=bar_5m.close,
          orb_high=orb_state.orb_high,
          orb_low=orb_state.orb_low,
          vwap=vwap,
          rvol=rvol,
          rejection_reason="PRICE_BELOW_VWAP",
          explanation=f"Close {bar_5m.close} <= Session VWAP {vwap}.",
      )

    # 7. Relative Volume threshold check
    if rvol < self.config.min_rvol_threshold:
      return ORBSignalEvaluation(
          symbol=sym,
          session_date=session_date,
          timestamp_utc=bar_5m.timestamp_utc,
          triggered=False,
          signal_id=None,
          signal_price=bar_5m.close,
          orb_high=orb_state.orb_high,
          orb_low=orb_state.orb_low,
          vwap=vwap,
          rvol=rvol,
          rejection_reason="INSUFFICIENT_RVOL",
          explanation=f"RVOL {rvol} < threshold {self.config.min_rvol_threshold}.",
      )

    # 8. Maximum spread filter
    if estimated_spread_pct > self.config.max_spread_pct:
      return ORBSignalEvaluation(
          symbol=sym,
          session_date=session_date,
          timestamp_utc=bar_5m.timestamp_utc,
          triggered=False,
          signal_id=None,
          signal_price=bar_5m.close,
          orb_high=orb_state.orb_high,
          orb_low=orb_state.orb_low,
          vwap=vwap,
          rvol=rvol,
          rejection_reason="SPREAD_TOO_WIDE",
          explanation=f"Spread {estimated_spread_pct} > max {self.config.max_spread_pct}.",
      )

    # 9. Price-band upper circuit headroom check (>= 3.0% below upper circuit)
    _, upper_circuit = self.registry.compute_price_bands(sym, prev_daily_close)
    headroom_pct = (upper_circuit - bar_5m.close) / bar_5m.close
    if headroom_pct < self.config.min_upper_circuit_headroom_pct:
      return ORBSignalEvaluation(
          symbol=sym,
          session_date=session_date,
          timestamp_utc=bar_5m.timestamp_utc,
          triggered=False,
          signal_id=None,
          signal_price=bar_5m.close,
          orb_high=orb_state.orb_high,
          orb_low=orb_state.orb_low,
          vwap=vwap,
          rvol=rvol,
          rejection_reason="INSUFFICIENT_UPPER_CIRCUIT_HEADROOM",
          explanation=f"Upper circuit headroom {headroom_pct:.4f} < {self.config.min_upper_circuit_headroom_pct}.",
      )

    # 10. Multi-timeframe macro trend alignment (e.g. 1h EMA-50)
    if self.config.require_macro_trend_alignment:
      aligned, trend_msg = check_macro_trend_alignment(bar_5m.close, htf_ema, direction="LONG")
      if not aligned:
        return ORBSignalEvaluation(
            symbol=sym,
            session_date=session_date,
            timestamp_utc=bar_5m.timestamp_utc,
            triggered=False,
            signal_id=None,
            signal_price=bar_5m.close,
            orb_high=orb_state.orb_high,
            orb_low=orb_state.orb_low,
            vwap=vwap,
            rvol=rvol,
            rejection_reason="COUNTER_MACRO_TREND",
            explanation=trend_msg,
        )

    # 11. Two-day high breakout confirmation
    if self.config.require_two_day_high_breakout:
      confirmed, high_msg = check_two_day_high_breakout(bar_5m.close, prev_day_high)
      if not confirmed:
        return ORBSignalEvaluation(
            symbol=sym,
            session_date=session_date,
            timestamp_utc=bar_5m.timestamp_utc,
            triggered=False,
            signal_id=None,
            signal_price=bar_5m.close,
            orb_high=orb_state.orb_high,
            orb_low=orb_state.orb_low,
            vwap=vwap,
            rvol=rvol,
            rejection_reason="TWO_DAY_HIGH_NOT_BROKEN",
            explanation=high_msg,
        )

    # 12. VWAP breakout exhaustion filter
    if self.config.require_vwap_volatility_cap:
      ok, vwap_msg = check_vwap_breakout_exhaustion(
          current_price=bar_5m.close,
          vwap=vwap,
          vwap_std=vwap_std,
          max_multiplier=self.config.max_vwap_std_multiplier,
      )
      if not ok:
        return ORBSignalEvaluation(
            symbol=sym,
            session_date=session_date,
            timestamp_utc=bar_5m.timestamp_utc,
            triggered=False,
            signal_id=None,
            signal_price=bar_5m.close,
            orb_high=orb_state.orb_high,
            orb_low=orb_state.orb_low,
            vwap=vwap,
            rvol=rvol,
            rejection_reason="VWAP_EXHAUSTION_BAND_EXCEEDED",
            explanation=vwap_msg,
        )

    # 13. HMM Volatility Regime filter
    if getattr(self.config, "require_regime_filter", False) and regime_state is not None:
      allowed = getattr(self.config, "allowed_regimes", ["BULL", "NEUTRAL"])
      if regime_state.regime.value not in allowed:
        return ORBSignalEvaluation(
            symbol=sym,
            session_date=session_date,
            timestamp_utc=bar_5m.timestamp_utc,
            triggered=False,
            signal_id=None,
            signal_price=bar_5m.close,
            orb_high=orb_state.orb_high,
            orb_low=orb_state.orb_low,
            vwap=vwap,
            rvol=rvol,
            rejection_reason="ADVERSE_REGIME",
            explanation=(
                f"Rejected: Current volatility regime is {regime_state.regime.value} "
                f"(confidence={regime_state.confidence:.1%}). "
                f"Allowed regimes: {allowed}."
            ),
        )

    # 13. HMM Volatility Regime filter
    if getattr(self.config, "require_regime_filter", False) and regime_state is not None:
      allowed = getattr(self.config, "allowed_regimes", ["BULL", "NEUTRAL"])
      if regime_state.regime.value not in allowed:
        return ORBSignalEvaluation(
            symbol=sym,
            session_date=session_date,
            timestamp_utc=bar_5m.timestamp_utc,
            triggered=False,
            signal_id=None,
            signal_price=bar_5m.close,
            orb_high=orb_state.orb_high,
            orb_low=orb_state.orb_low,
            vwap=vwap,
            rvol=rvol,
            rejection_reason="ADVERSE_REGIME",
            explanation=(
                f"Rejected: Current volatility regime is {regime_state.regime.value} "
                f"(confidence={regime_state.confidence:.1%}). "
                f"Allowed regimes: {allowed}."
            ),
        )

    sig_id = f"SIG_{sym}_{session_date}_{dt_ist.strftime('%H%M')}"
    return ORBSignalEvaluation(
        symbol=sym,
        session_date=session_date,
        timestamp_utc=bar_5m.timestamp_utc,
        triggered=True,
        signal_id=sig_id,
        signal_price=bar_5m.close,
        orb_high=orb_state.orb_high,
        orb_low=orb_state.orb_low,
        vwap=vwap,
        rvol=rvol,
        rejection_reason=None,
        explanation=(
            f"ORB Long Breakout confirmed: Close {bar_5m.close} > ORH {orb_state.orb_high}, "
            f"VWAP {vwap}, RVOL {rvol}x."
        ),
    )
