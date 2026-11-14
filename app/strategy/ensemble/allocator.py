"""Dynamic Meta-Ensemble Allocator & Signal Fusion Engine (Milestone M27).

Orchestrates multi-strategy capital allocation dynamically conditioned on M19 HMM regime
posterior probabilities, resolves signal conflicts, and applies Bayesian conviction fusion.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
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
from app.strategy.ensemble.orb_trend import ORBTrendStrategy
from app.strategy.ensemble.stat_arb import PairsSpreadReversionStrategy
from app.strategy.ensemble.vwap_reversion import VWAPMeanReversionStrategy


@dataclass(frozen=True)
class SynthesizedEnsembleOrder:
  """Actionable order recommendation emitted by the MetaStrategyEnsemble."""
  ensemble_order_id: str
  symbol: str
  side: str  # "BUY" or "SELL"
  reference_price: Decimal
  stop_loss_price: Decimal
  target_price: Decimal | None
  effective_conviction: Decimal  # 0.00 to 1.00
  participating_strategies: list[str]
  timestamp_utc: datetime
  status: str = "APPROVED"

  def to_dict(self) -> dict[str, Any]:
    return {
        "ensemble_order_id": self.ensemble_order_id,
        "symbol": self.symbol,
        "side": self.side,
        "reference_price": str(self.reference_price),
        "stop_loss_price": str(self.stop_loss_price),
        "target_price": str(self.target_price) if self.target_price is not None else None,
        "effective_conviction": str(self.effective_conviction),
        "participating_strategies": self.participating_strategies,
        "timestamp_utc": self.timestamp_utc.isoformat(),
        "status": self.status,
    }


class MetaStrategyEnsemble:
  """Institutional multi-strategy meta-allocator with dynamic regime weighting."""

  # Default Regime Weight Matrix: W[Regime][Strategy]
  DEFAULT_REGIME_WEIGHTS: dict[MarketRegime, dict[str, Decimal]] = {
      MarketRegime.BULL: {
          "ORB_TREND_V2": Decimal("0.70"),
          "VWAP_REVERSION_V1": Decimal("0.20"),
          "STAT_ARB_PAIRS_V1": Decimal("0.10"),
      },
      MarketRegime.NEUTRAL: {
          "ORB_TREND_V2": Decimal("0.15"),
          "VWAP_REVERSION_V1": Decimal("0.60"),
          "STAT_ARB_PAIRS_V1": Decimal("0.25"),
      },
      MarketRegime.BEAR: {
          "ORB_TREND_V2": Decimal("0.00"),
          "VWAP_REVERSION_V1": Decimal("0.30"),
          "STAT_ARB_PAIRS_V1": Decimal("0.70"),
      },
  }

  def __init__(self, strategies: list[AbstractAlphaStrategy] | None = None):
    assert_paper_mode()
    if strategies is not None:
      self.strategies = {s.strategy_id: s for s in strategies}
    else:
      self.strategies = {
          "ORB_TREND_V2": ORBTrendStrategy(),
          "VWAP_REVERSION_V1": VWAPMeanReversionStrategy(),
          "STAT_ARB_PAIRS_V1": PairsSpreadReversionStrategy(),
      }

  def compute_dynamic_weights(self, regime_state: RegimeState | None) -> dict[str, Decimal]:
    """Computes continuous strategy weights by weighting regime matrices with posterior probabilities."""
    assert_paper_mode()
    if not regime_state:
      # Default equal-ish neutral weights
      return {
          "ORB_TREND_V2": Decimal("0.33"),
          "VWAP_REVERSION_V1": Decimal("0.34"),
          "STAT_ARB_PAIRS_V1": Decimal("0.33"),
      }

    p_bull = Decimal(str(round(regime_state.bull_prob, 4)))
    p_neutral = Decimal(str(round(regime_state.neutral_prob, 4)))
    p_bear = Decimal(str(round(regime_state.bear_prob, 4)))

    w_bull = self.DEFAULT_REGIME_WEIGHTS[MarketRegime.BULL]
    w_neutral = self.DEFAULT_REGIME_WEIGHTS[MarketRegime.NEUTRAL]
    w_bear = self.DEFAULT_REGIME_WEIGHTS[MarketRegime.BEAR]

    raw_weights: dict[str, Decimal] = {}
    total_w = Decimal("0.00")

    for strat_id in self.strategies.keys():
      val = (
          p_bull * w_bull.get(strat_id, Decimal("0"))
          + p_neutral * w_neutral.get(strat_id, Decimal("0"))
          + p_bear * w_bear.get(strat_id, Decimal("0"))
      )
      raw_weights[strat_id] = val
      total_w += val

    # Normalize weights so they sum strictly to 1.0000
    normalized_weights: dict[str, Decimal] = {}
    if total_w > Decimal("0.00"):
      for strat_id, val in raw_weights.items():
        norm_val = (val / total_w).quantize(Decimal("0.0001"))
        normalized_weights[strat_id] = norm_val
    else:
      n = Decimal(len(self.strategies))
      for strat_id in self.strategies.keys():
        normalized_weights[strat_id] = (Decimal("1.00") / n).quantize(Decimal("0.0001"))

    return normalized_weights

  def evaluate_ensemble(
      self,
      symbol: str,
      session_bars: list[CandleBar],
      regime_state: RegimeState | None = None,
      **kwargs: Any,
  ) -> list[SynthesizedEnsembleOrder]:
    """Runs all registered strategies, aggregates signals, resolves conflicts, and produces final orders."""
    assert_paper_mode()
    dynamic_weights = self.compute_dynamic_weights(regime_state)

    raw_signals: list[EnsembleSignal] = []
    for strat_id, strat in self.strategies.items():
      strat_signals = strat.evaluate(
          symbol=symbol,
          session_bars=session_bars,
          regime_state=regime_state,
          **kwargs,
      )
      raw_signals.extend(strat_signals)

    if not raw_signals:
      return []

    # Group signals by side ("BUY" vs "SELL")
    buy_signals = [s for s in raw_signals if s.side == "BUY"]
    sell_signals = [s for s in raw_signals if s.side == "SELL"]

    # Conflict Resolution:
    # If both BUY and SELL exist for the same symbol:
    if buy_signals and sell_signals:
      buy_conviction = sum(s.confidence * dynamic_weights.get(s.strategy_id, Decimal("0")) for s in buy_signals)
      sell_conviction = sum(s.confidence * dynamic_weights.get(s.strategy_id, Decimal("0")) for s in sell_signals)

      if abs(buy_conviction - sell_conviction) < Decimal("0.10"):
        # Neutralized conflict (no clear edge) -> discard both
        return []
      elif buy_conviction > sell_conviction:
        chosen_signals = buy_signals
      else:
        chosen_signals = sell_signals
    elif buy_signals:
      chosen_signals = buy_signals
    else:
      chosen_signals = sell_signals

    # Consensus Synthesis: Combine multiple signals in the same direction
    participating = [s.strategy_id for s in chosen_signals]
    total_weighted_conf = Decimal("0.00")
    for s in chosen_signals:
      w = dynamic_weights.get(s.strategy_id, Decimal("0.33"))
      total_weighted_conf += s.confidence * w

    effective_conviction = min(Decimal("1.00"), total_weighted_conf.quantize(Decimal("0.0001")))

    # Take most conservative stop loss and weighted target
    ref_price = chosen_signals[0].reference_price
    side = chosen_signals[0].side

    if side == "BUY":
      # Highest stop loss gives tightest protection
      stop_loss = max(s.stop_loss_price for s in chosen_signals)
    else:
      # Lowest stop loss gives tightest protection for short
      stop_loss = min(s.stop_loss_price for s in chosen_signals)

    targets = [s.target_price for s in chosen_signals if s.target_price is not None]
    target_price = round_price(sum(targets) / Decimal(len(targets))) if targets else None

    order = SynthesizedEnsembleOrder(
        ensemble_order_id=f"ENS_{symbol}_{uuid.uuid4().hex[:8]}",
        symbol=symbol,
        side=side,
        reference_price=ref_price,
        stop_loss_price=stop_loss,
        target_price=target_price,
        effective_conviction=effective_conviction,
        participating_strategies=participating,
        timestamp_utc=chosen_signals[0].timestamp_utc,
        status="APPROVED",
    )
    return [order]

  def get_telemetry(self, regime_state: RegimeState | None = None) -> dict[str, Any]:
    """Returns ensemble status, registered strategies, and active regime weights."""
    assert_paper_mode()
    weights = self.compute_dynamic_weights(regime_state)
    return {
        "registered_strategies_count": len(self.strategies),
        "strategies": [
            {
                "strategy_id": s.strategy_id,
                "strategy_type": s.strategy_type.value,
                "current_weight": str(weights.get(s.strategy_id, Decimal("0"))),
            }
            for s in self.strategies.values()
        ],
        "active_weights": {k: str(v) for k, v in weights.items()},
        "regime": regime_state.regime.value if regime_state else "UNKNOWN",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
