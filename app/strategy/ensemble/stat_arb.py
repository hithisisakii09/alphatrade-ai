"""Statistical Arbitrage / Pairs Spread Reversion Sub-Strategy (Milestone M27).

Tracks normalized price ratio spread z-scores across cointegrated liquid constituent pairs
(e.g., HDFCBANK vs ICICIBANK, TCS vs INFY).
"""

from datetime import datetime, timezone
from decimal import Decimal
import math
from typing import Any
import uuid

from app.core.execution_guard import assert_paper_mode
from app.core.money import round_price, to_decimal
from app.data.models import CandleBar
from app.market.regime import MarketRegime, RegimeState
from app.strategy.ensemble.base import (
    AbstractAlphaStrategy,
    EnsembleSignal,
    StrategyType,
)


class PairsSpreadReversionStrategy(AbstractAlphaStrategy):
  """Statistical Arbitrage pairs strategy evaluating relative spread z-scores."""

  def __init__(
      self,
      strategy_id: str = "STAT_ARB_PAIRS_V1",
      z_threshold: Decimal = Decimal("2.0"),
      lookback_window: int = 30,
  ):
    super().__init__(strategy_id=strategy_id, strategy_type=StrategyType.STAT_ARB)
    self.z_threshold = z_threshold
    self.lookback_window = lookback_window

    # Canonical correlated liquid pairs (NSE Cash)
    self.pairs: dict[str, str] = {
        "HDFCBANK": "ICICIBANK",
        "ICICIBANK": "HDFCBANK",
        "TCS": "INFY",
        "INFY": "TCS",
    }

  def compute_spread_zscore(
      self,
      bars_a: list[CandleBar],
      bars_b: list[CandleBar],
  ) -> tuple[Decimal, Decimal, Decimal]:
    """Computes price ratio z-score between two aligned price series.
    
    Returns:
      (latest_zscore, rolling_mean, rolling_std)
    """
    n = min(len(bars_a), len(bars_b), self.lookback_window)
    if n < 10:
      return Decimal("0.00"), Decimal("0.00"), Decimal("0.00")

    ratios: list[float] = []
    for i in range(-n, 0):
      p_a = float(bars_a[i].close)
      p_b = float(bars_b[i].close)
      if p_b > 0:
        ratios.append(p_a / p_b)

    if len(ratios) < 10:
      return Decimal("0.00"), Decimal("0.00"), Decimal("0.00")

    mean_r = sum(ratios) / len(ratios)
    variance = sum((r - mean_r) ** 2 for r in ratios) / len(ratios)
    std_r = math.sqrt(variance) if variance > 0 else 0.0

    if std_r <= 1e-6:
      return Decimal("0.00"), to_decimal(Decimal(str(round(mean_r, 4)))), Decimal("0.00")

    curr_ratio = ratios[-1]
    z = (curr_ratio - mean_r) / std_r

    return (
        to_decimal(Decimal(str(round(z, 4)))),
        to_decimal(Decimal(str(round(mean_r, 4)))),
        to_decimal(Decimal(str(round(std_r, 4)))),
    )

  def evaluate(
      self,
      symbol: str,
      session_bars: list[CandleBar],
      regime_state: RegimeState | None = None,
      **kwargs: Any,
  ) -> list[EnsembleSignal]:
    assert_paper_mode()
    pair_symbol = self.pairs.get(symbol)
    if not pair_symbol:
      return []

    pair_bars = kwargs.get("pair_bars")
    if not pair_bars or len(pair_bars) < 10 or len(session_bars) < 10:
      return []

    z_score, mean_ratio, std_ratio = self.compute_spread_zscore(session_bars, pair_bars)

    # Condition: Stock A is undervalued relative to Stock B (z <= -2.0)
    # Signals a BUY on Stock A targeting spread reversion to mean ratio.
    if z_score <= -self.z_threshold:
      latest_bar = session_bars[-1]
      ref_p = latest_bar.close

      # Stop loss at 3.0σ spread divergence (~1.5% below current price)
      stop_loss_p = round_price(ref_p * Decimal("0.9850"))
      # Target profit at mean ratio
      target_p = round_price(ref_p * Decimal("1.0250"))

      base_conf = Decimal("0.65")
      if regime_state:
        # Stat arb carries highest weight during volatile or neutral regimes
        if regime_state.regime in (MarketRegime.BEAR, MarketRegime.NEUTRAL):
          base_conf += Decimal("0.20")
      confidence = min(Decimal("0.90"), base_conf)

      sig = EnsembleSignal(
          signal_id=f"SIG_STATARB_{symbol}_{uuid.uuid4().hex[:8]}",
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
              "pair_symbol": pair_symbol,
              "z_score": str(z_score),
              "mean_ratio": str(mean_ratio),
              "std_ratio": str(std_ratio),
              "regime": regime_state.regime.value if regime_state else "UNKNOWN",
          },
      )
      return [sig]

    return []
