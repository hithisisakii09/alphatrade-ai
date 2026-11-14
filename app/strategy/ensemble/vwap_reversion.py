"""VWAP Volatility Band Mean-Reversion Alpha Sub-Strategy (Milestone M27).

Exploits statistical price over-extension relative to session Volume-Weighted Average Price (VWAP)
during range-bound NEUTRAL volatility regimes.
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


class VWAPMeanReversionStrategy(AbstractAlphaStrategy):
  """Mean-reversion strategy trading oversold deviations from intraday VWAP."""

  def __init__(
      self,
      strategy_id: str = "VWAP_REVERSION_V1",
      band_multiplier: Decimal = Decimal("2.0"),
      max_rvol_for_reversion: Decimal = Decimal("1.30"),
      entry_start_ist: str = "10:00:00",
      entry_cutoff_ist: str = "14:45:00",
  ):
    super().__init__(strategy_id=strategy_id, strategy_type=StrategyType.MEAN_REVERSION)
    self.band_multiplier = band_multiplier
    self.max_rvol = max_rvol_for_reversion
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
    # Requires established intraday distribution (>= 15 1m/5m bars)
    if not session_bars or len(session_bars) < 15:
      return []

    # Mean reversion is strictly prioritized during NEUTRAL regimes;
    # in strong trending BULL or catastrophic BEAR, reversion signals carry higher failure risk.
    if regime_state and regime_state.regime == MarketRegime.BEAR:
      return []

    latest_bar = session_bars[-1]
    t_latest_ist = to_ist(latest_bar.timestamp_utc).time()

    if t_latest_ist < self.entry_start or t_latest_ist > self.entry_cutoff:
      return []

    vwap = compute_session_vwap(session_bars)
    vwap_std = compute_session_vwap_std(session_bars, vwap)

    if vwap_std <= Decimal("0.00"):
      return []

    lower_band = round_price(vwap - (self.band_multiplier * vwap_std))
    upper_band = round_price(vwap + (self.band_multiplier * vwap_std))

    rvol = to_decimal(kwargs.get("rvol", Decimal("0.90")))

    # 1. Long Mean-Reversion: Price is oversold below lower band (Price <= VWAP - 2.0σ)
    # Lack of aggressive selling volume (rvol <= 1.30) indicates liquidity exhaustion rather than genuine breakdown.
    if latest_bar.close <= lower_band and rvol <= self.max_rvol:
      ref_p = latest_bar.close
      target_p = vwap  # Mean reversion target back to session VWAP
      # Stop placed outside 2.75σ band
      stop_loss_p = round_price(vwap - ((self.band_multiplier + Decimal("0.75")) * vwap_std))

      # Ensure valid risk/reward
      if stop_loss_p >= ref_p:
        stop_loss_p = round_price(ref_p * Decimal("0.9900"))  # 1% stop fallback

      base_conf = Decimal("0.65")
      if regime_state and regime_state.regime == MarketRegime.NEUTRAL:
        base_conf += to_decimal(Decimal(str(regime_state.neutral_prob))) * Decimal("0.25")
      confidence = min(Decimal("0.95"), base_conf)

      sig = EnsembleSignal(
          signal_id=f"SIG_MR_{symbol}_{uuid.uuid4().hex[:8]}",
          strategy_id=self.strategy_id,
          strategy_type=self.strategy_type,
          symbol=symbol,
          side="BUY",
          reference_price=ref_p,
          stop_loss_price=stop_loss_p,
          target_price=target_p,
          timestamp_utc=latest_bar.timestamp_utc,
          confidence=confidence,
          metadata={
              "vwap": str(vwap),
              "vwap_std": str(vwap_std),
              "lower_band": str(lower_band),
              "upper_band": str(upper_band),
              "distance_to_vwap_pct": str(round_price(((vwap - ref_p) / vwap) * Decimal("100"))),
              "rvol": str(rvol),
              "regime": regime_state.regime.value if regime_state else "UNKNOWN",
          },
      )
      return [sig]

    return []
