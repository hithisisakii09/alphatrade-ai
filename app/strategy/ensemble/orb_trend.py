"""Trend-Following ORB Alpha Sub-Strategy (Milestone M27).

Wraps the core Opening Range Breakout (ORB v2) alpha model with higher timeframe
trend confirmation and relative volume filtering.
"""

from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Any
import uuid

from app.core.clock import to_ist
from app.core.execution_guard import assert_paper_mode
from app.core.money import round_price, to_decimal
from app.data.models import CandleBar
from app.market.indicators import (
    compute_session_vwap,
    compute_session_vwap_std,
)
from app.market.regime import MarketRegime, RegimeState
from app.strategy.ensemble.base import (
    AbstractAlphaStrategy,
    EnsembleSignal,
    StrategyType,
)


class ORBTrendStrategy(AbstractAlphaStrategy):
  """Momentum & Trend-Following strategy exploiting 15m Opening Range Breakouts."""

  def __init__(
      self,
      strategy_id: str = "ORB_TREND_V2",
      min_rvol: Decimal = Decimal("1.50"),
      entry_start_ist: str = "09:30:00",
      entry_cutoff_ist: str = "14:30:00",
  ):
    super().__init__(strategy_id=strategy_id, strategy_type=StrategyType.TREND_FOLLOWING)
    self.min_rvol = min_rvol
    self.entry_start = time.fromisoformat(entry_start_ist)
    self.entry_cutoff = time.fromisoformat(entry_cutoff_ist)

  def evaluate(
      self,
      symbol: str,
      session_bars: list[CandleBar],
      regime_state: RegimeState | None = None,
      **kwargs: Any,
  ) -> list[EnsembleSignal]:
    assert_paper_mode()
    if not session_bars or len(session_bars) < 3:
      return []

    # Filter adverse BEAR regimes
    if regime_state and regime_state.regime == MarketRegime.BEAR:
      return []

    # Compute 15-minute Opening Range (bars between 09:15 and 09:30 IST)
    or_bars: list[CandleBar] = []
    post_or_bars: list[CandleBar] = []

    for b in session_bars:
      t_ist = to_ist(b.timestamp_utc).time()
      if t_ist < self.entry_start:
        or_bars.append(b)
      else:
        post_or_bars.append(b)

    if not or_bars or not post_or_bars:
      return []

    orh = max(b.high for b in or_bars)
    orl = min(b.low for b in or_bars)
    vwap = compute_session_vwap(session_bars)

    latest_bar = post_or_bars[-1]
    t_latest_ist = to_ist(latest_bar.timestamp_utc).time()

    if t_latest_ist < self.entry_start or t_latest_ist > self.entry_cutoff:
      return []

    # Check breakout: Close > ORH and Close > VWAP
    if latest_bar.close <= orh or latest_bar.close <= vwap:
      return []

    # RVOL filter check
    rvol = to_decimal(kwargs.get("rvol", Decimal("1.80")))
    if rvol < self.min_rvol:
      return []

    # Higher timeframe EMA check
    htf_ema = kwargs.get("htf_ema")
    if htf_ema is not None and latest_bar.close <= to_decimal(htf_ema):
      return []

    # Passed all breakout criteria -> emit BUY signal
    ref_p = latest_bar.close
    initial_stop = max(orl, round_price(ref_p * Decimal("0.9875")))  # 1.25% stop or clamped ORL
    risk = ref_p - initial_stop
    target_p = round_price(ref_p + (risk * Decimal("2.00"))) if risk > Decimal("0") else None

    # Confidence scaling based on RVOL and Bull probability
    base_conf = Decimal("0.70")
    if regime_state:
      base_conf += to_decimal(Decimal(str(regime_state.bull_prob))) * Decimal("0.20")
    confidence = min(Decimal("1.00"), base_conf)

    sig = EnsembleSignal(
        signal_id=f"SIG_ORB_{symbol}_{uuid.uuid4().hex[:8]}",
        strategy_id=self.strategy_id,
        strategy_type=self.strategy_type,
        symbol=symbol,
        side="BUY",
        reference_price=ref_p,
        stop_loss_price=initial_stop,
        target_price=target_p,
        timestamp_utc=latest_bar.timestamp_utc,
        confidence=confidence,
        metadata={
            "orh": str(orh),
            "orl": str(orl),
            "vwap": str(vwap),
            "rvol": str(rvol),
            "regime": regime_state.regime.value if regime_state else "UNKNOWN",
        },
    )
    return [sig]
