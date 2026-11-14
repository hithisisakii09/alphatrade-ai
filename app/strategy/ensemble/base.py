"""Base interfaces and signal models for the Multi-Strategy Meta-Ensemble (Milestone M27).

Institutional Invariants:
1. Pure Decimal arithmetic with zero binary float drift.
2. Uniform signal representation across heterogeneous alpha models.
3. Strict fail-closed paper execution enforcement via assert_paper_mode().
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
import uuid

from app.core.execution_guard import assert_paper_mode
from app.core.money import round_price, to_decimal
from app.data.models import CandleBar
from app.market.regime import RegimeState


class StrategyType(str, Enum):
  TREND_FOLLOWING = "TREND_FOLLOWING"
  MEAN_REVERSION = "MEAN_REVERSION"
  STAT_ARB = "STAT_ARB"


@dataclass(frozen=True)
class EnsembleSignal:
  """Standardized prospective trade signal emitted by any ensemble alpha strategy."""
  signal_id: str
  strategy_id: str
  strategy_type: StrategyType
  symbol: str
  side: str  # "BUY" or "SELL"
  reference_price: Decimal
  stop_loss_price: Decimal
  target_price: Decimal | None
  timestamp_utc: datetime
  confidence: Decimal  # 0.00 to 1.00
  metadata: dict[str, Any] = field(default_factory=dict)

  def __post_init__(self):
    assert_paper_mode()

  def to_dict(self) -> dict[str, Any]:
    return {
        "signal_id": self.signal_id,
        "strategy_id": self.strategy_id,
        "strategy_type": self.strategy_type.value,
        "symbol": self.symbol,
        "side": self.side,
        "reference_price": str(self.reference_price),
        "stop_loss_price": str(self.stop_loss_price),
        "target_price": str(self.target_price) if self.target_price is not None else None,
        "timestamp_utc": self.timestamp_utc.isoformat(),
        "confidence": str(self.confidence),
        "metadata": self.metadata,
    }


class AbstractAlphaStrategy(ABC):
  """Abstract base class for all institutional alpha sub-strategies."""

  def __init__(self, strategy_id: str, strategy_type: StrategyType):
    assert_paper_mode()
    self.strategy_id = strategy_id
    self.strategy_type = strategy_type

  @abstractmethod
  def evaluate(
      self,
      symbol: str,
      session_bars: list[CandleBar],
      regime_state: RegimeState | None = None,
      **kwargs: Any,
  ) -> list[EnsembleSignal]:
    """Evaluates chronological session candle bars and emits prospective alpha signals."""
    raise NotImplementedError
