"""Institutional Algorithmic Order Execution Slicer (TWAP / Dynamic VWAP) (Milestone M25).

Conforms strictly to quantitative institutional execution standards:
1. Pure Decimal arithmetic with zero binary float drift.
2. Abstract ExecutionAlgorithm interface with concrete TWAP and Dynamic VWAP engines.
3. Strict conservation of parent order quantity (integer rounding with exact remainder reconciliation).
4. Deterministic timing jitter to mitigate adverse selection and pattern sniffing.
5. Max slippage abort boundary monitoring against parent arrival price.
6. Seamless integration with BrokerGateway and MicrostructureLiquidityGate.
7. Strict fail-closed paper mode guard (assert_paper_mode()).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
import math
from typing import Any
import uuid

from app.core.execution_guard import assert_paper_mode
from app.core.money import round_paise, round_price, to_decimal
from app.execution.gateway import BrokerGateway, BrokerOrderRequest, BrokerOrderResponse
from app.execution.tca import (
    ExecutionSliceFill,
    ImplementationShortfallBreakdown,
    compute_implementation_shortfall,
)
from app.market.microstructure import (
    MicrostructureLiquidityGate,
    MicrostructureSnapshot,
    OrderBookDepth,
    analyze_order_book,
)


@dataclass
class ChildOrderSlice:
  """Single child slice of a sliced parent algorithmic order."""
  slice_id: str
  slice_index: int
  quantity: int
  scheduled_time_utc: datetime
  status: str = "PENDING"  # "PENDING", "DEFERRED", "SUBMITTED", "FILLED", "ABORTED"
  target_price: Decimal | None = None
  executed_price: Decimal = Decimal("0.00")
  filled_quantity: int = 0
  executed_time_utc: datetime | None = None
  broker_order_id: str | None = None
  rejection_reason: str | None = None

  def to_dict(self) -> dict[str, Any]:
    return {
        "slice_id": self.slice_id,
        "slice_index": self.slice_index,
        "quantity": self.quantity,
        "scheduled_time_utc": self.scheduled_time_utc.isoformat(),
        "status": self.status,
        "target_price": str(self.target_price) if self.target_price else None,
        "executed_price": str(self.executed_price),
        "filled_quantity": self.filled_quantity,
        "executed_time_utc": self.executed_time_utc.isoformat() if self.executed_time_utc else None,
        "broker_order_id": self.broker_order_id,
        "rejection_reason": self.rejection_reason,
    }


@dataclass(frozen=True)
class ParentOrderRequest:
  """Institutional parent algorithmic order specification."""
  parent_order_id: str
  symbol: str
  side: str  # "BUY" or "SELL"
  total_quantity: int
  start_time_utc: datetime
  end_time_utc: datetime
  algo_type: str = "TWAP"  # "TWAP" or "VWAP"
  num_slices: int = 5
  arrival_price: Decimal = Decimal("0.00")
  decision_price: Decimal = Decimal("0.00")
  max_slippage_bps: Decimal = Decimal("50.0")  # Abort if price moves >50 bps adversely
  jitter_pct: Decimal = Decimal("0.10")  # Timing jitter up to 10% of interval


class ExecutionAlgorithm(ABC):
  """Abstract Execution Algorithm managing child order schedules and fills."""

  def __init__(self, parent_request: ParentOrderRequest):
    assert_paper_mode()
    self.parent = parent_request
    self.status: str = "ACTIVE"  # "ACTIVE", "COMPLETED", "ABORTED"
    self.abort_reason: str | None = None
    self.slices: list[ChildOrderSlice] = []
    self.fills: list[ExecutionSliceFill] = []
    self._init_schedule()

  @abstractmethod
  def _init_schedule(self) -> None:
    """Subclass generates the child slice schedule."""
    pass

  def get_next_pending_slice(self) -> ChildOrderSlice | None:
    """Returns next child slice awaiting execution."""
    if self.status != "ACTIVE":
      return None
    for s in self.slices:
      if s.status in ("PENDING", "DEFERRED"):
        return s
    return None

  def record_slice_fill(
      self,
      slice_id: str,
      filled_quantity: int,
      average_price: Decimal,
      broker_order_id: str,
      timestamp_utc: datetime | None = None,
      fee_inr: Decimal = Decimal("0.00"),
  ) -> None:
    """Updates slice lifecycle on fill and logs fill record for TCA."""
    now = timestamp_utc or datetime.now(timezone.utc)
    for s in self.slices:
      if s.slice_id == slice_id:
        s.filled_quantity = filled_quantity
        s.executed_price = average_price
        s.executed_time_utc = now
        s.broker_order_id = broker_order_id
        s.status = "FILLED" if filled_quantity >= s.quantity else "PARTIALLY_FILLED"

        self.fills.append(
            ExecutionSliceFill(
                slice_id=slice_id,
                quantity=filled_quantity,
                price=average_price,
                timestamp_utc=now,
                fee_inr=fee_inr,
            )
        )
        break

    # Check if all completed
    if all(s.status == "FILLED" for s in self.slices):
      self.status = "COMPLETED"

  def check_abort_condition(self, current_price: Decimal) -> tuple[bool, str]:
    """Evaluates whether adverse price movement exceeds max slippage tolerance."""
    assert_paper_mode()
    curr_p = to_decimal(current_price)
    arr_p = to_decimal(self.parent.arrival_price)
    if arr_p <= Decimal("0.00"):
      return False, "NO_ARRIVAL_PRICE"

    max_slip = self.parent.max_slippage_bps
    if self.parent.side.upper() == "BUY":
      adverse_drift_bps = ((curr_p - arr_p) / arr_p * Decimal("10000")).quantize(
          Decimal("0.01"), rounding=ROUND_HALF_UP
      )
      if adverse_drift_bps > max_slip:
        self.status = "ABORTED"
        self.abort_reason = f"MAX_SLIPPAGE_EXCEEDED:{adverse_drift_bps}bps>{max_slip}bps"
        for s in self.slices:
          if s.status in ("PENDING", "DEFERRED"):
            s.status = "ABORTED"
            s.rejection_reason = self.abort_reason
        return True, self.abort_reason
    else:  # SELL
      adverse_drift_bps = ((arr_p - curr_p) / arr_p * Decimal("10000")).quantize(
          Decimal("0.01"), rounding=ROUND_HALF_UP
      )
      if adverse_drift_bps > max_slip:
        self.status = "ABORTED"
        self.abort_reason = f"MAX_SLIPPAGE_EXCEEDED:{adverse_drift_bps}bps>{max_slip}bps"
        for s in self.slices:
          if s.status in ("PENDING", "DEFERRED"):
            s.status = "ABORTED"
            s.rejection_reason = self.abort_reason
        return True, self.abort_reason

    return False, "WITHIN_SLIPPAGE_TOLERANCE"

  def is_complete(self) -> bool:
    return self.status in ("COMPLETED", "ABORTED")

  def compute_tca(
      self,
      final_market_price: Decimal | None = None,
      interval_vwap: Decimal | None = None,
      interval_twap: Decimal | None = None,
  ) -> ImplementationShortfallBreakdown:
    """Computes comprehensive Post-Trade Implementation Shortfall."""
    assert_paper_mode()
    dec_p = self.parent.decision_price if self.parent.decision_price > Decimal("0") else self.parent.arrival_price
    return compute_implementation_shortfall(
        symbol=self.parent.symbol,
        side=self.parent.side,
        total_ordered_quantity=self.parent.total_quantity,
        decision_price=dec_p,
        arrival_price=self.parent.arrival_price,
        fills=self.fills,
        final_market_price=final_market_price,
        interval_vwap=interval_vwap,
        interval_twap=interval_twap,
    )

  def to_dict(self) -> dict[str, Any]:
    total_filled = sum(s.filled_quantity for s in self.slices)
    return {
        "parent_order_id": self.parent.parent_order_id,
        "symbol": self.parent.symbol,
        "side": self.parent.side,
        "algo_type": self.parent.algo_type,
        "total_quantity": self.parent.total_quantity,
        "filled_quantity": total_filled,
        "status": self.status,
        "abort_reason": self.abort_reason,
        "num_slices": len(self.slices),
        "slices": [s.to_dict() for s in self.slices],
    }


class TWAPExecutionEngine(ExecutionAlgorithm):
  """Time-Weighted Average Price (TWAP) Execution Engine."""

  def _init_schedule(self) -> None:
    N = max(1, self.parent.num_slices)
    total_qty = self.parent.total_quantity
    base_qty = total_qty // N
    remainder = total_qty % N

    total_seconds = max(1.0, (self.parent.end_time_utc - self.parent.start_time_utc).total_seconds())
    interval_seconds = total_seconds / float(N)

    # Timing jitter: bounded pseudorandom shift based on slice index
    jitter_fraction = float(self.parent.jitter_pct)

    self.slices = []
    for i in range(N):
      slice_qty = base_qty + (1 if i < remainder else 0)
      slice_id = f"{self.parent.parent_order_id}_S{i+1:02d}"

      # Base time offset
      base_offset = i * interval_seconds
      # Deterministic pseudo-jitter: sine function bounded by jitter_fraction
      jitter_offset = math.sin(i + 1.0) * jitter_fraction * interval_seconds
      sched_seconds = max(0.0, min(total_seconds, base_offset + jitter_offset))
      sched_time = self.parent.start_time_utc + timedelta(seconds=sched_seconds)

      self.slices.append(
          ChildOrderSlice(
              slice_id=slice_id,
              slice_index=i + 1,
              quantity=slice_qty,
              scheduled_time_utc=sched_time,
              status="PENDING",
              target_price=self.parent.arrival_price,
          )
      )

    # Safety invariant: Total scheduled quantity MUST equal parent quantity exactly
    assert sum(s.quantity for s in self.slices) == total_qty, "TWAP slice quantity sum mismatch"


class DynamicVWAPExecutionEngine(ExecutionAlgorithm):
  """Dynamic Volume-Weighted Average Price (VWAP) Execution Engine."""

  # Canonical NSE intraday U-shaped volume weights for N slices
  CANONICAL_INTRADAY_VOLUME_WEIGHTS = [
      Decimal("0.24"),  # 09:15-10:00 Opening breakout rush
      Decimal("0.18"),  # 10:00-11:15 Morning trend continuation
      Decimal("0.14"),  # 11:15-12:30 Midday consolidation
      Decimal("0.16"),  # 12:30-13:45 European session open
      Decimal("0.28"),  # 13:45-15:30 Power hour & MTM close
  ]

  def _init_schedule(self) -> None:
    N = max(1, self.parent.num_slices)
    total_qty = self.parent.total_quantity

    # Interpolate or match volume weights
    if N == len(self.CANONICAL_INTRADAY_VOLUME_WEIGHTS):
      weights = self.CANONICAL_INTRADAY_VOLUME_WEIGHTS
    else:
      # Uniformly sample from canonical profile
      step = len(self.CANONICAL_INTRADAY_VOLUME_WEIGHTS) / float(N)
      raw_weights = [
          self.CANONICAL_INTRADAY_VOLUME_WEIGHTS[min(int(i * step), len(self.CANONICAL_INTRADAY_VOLUME_WEIGHTS) - 1)]
          for i in range(N)
      ]
      tot_w = sum(raw_weights)
      weights = [w / tot_w for w in raw_weights]

    # Compute raw quantities
    slice_quantities = [int((w * Decimal(total_qty)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) for w in weights]
    # Enforce exact integer sum conservation
    current_sum = sum(slice_quantities)
    diff = total_qty - current_sum
    if diff != 0:
      # Adjust the slice with largest quantity to absorb rounding residual
      max_idx = slice_quantities.index(max(slice_quantities))
      slice_quantities[max_idx] += diff

    total_seconds = max(1.0, (self.parent.end_time_utc - self.parent.start_time_utc).total_seconds())
    interval_seconds = total_seconds / float(N)

    self.slices = []
    for i in range(N):
      slice_qty = slice_quantities[i]
      slice_id = f"{self.parent.parent_order_id}_VWAP_S{i+1:02d}"
      sched_time = self.parent.start_time_utc + timedelta(seconds=i * interval_seconds)

      self.slices.append(
          ChildOrderSlice(
              slice_id=slice_id,
              slice_index=i + 1,
              quantity=slice_qty,
              scheduled_time_utc=sched_time,
              status="PENDING",
              target_price=self.parent.arrival_price,
          )
      )

    # Safety invariant: Total scheduled quantity MUST equal parent quantity exactly
    assert sum(s.quantity for s in self.slices) == total_qty, "VWAP slice quantity sum mismatch"


class AlgoExecutionCoordinator:
  """Coordinates execution of algorithmic orders against BrokerGateway and Market Microstructure."""

  def __init__(
      self,
      liquidity_gate: MicrostructureLiquidityGate | None = None,
  ):
    assert_paper_mode()
    self.liquidity_gate = liquidity_gate or MicrostructureLiquidityGate()
    self.active_algorithms: dict[str, ExecutionAlgorithm] = {}
    self.completed_tca_reports: list[ImplementationShortfallBreakdown] = []

  def register_algorithm(self, algo: ExecutionAlgorithm) -> None:
    self.active_algorithms[algo.parent.parent_order_id] = algo

  def get_algorithm(self, parent_order_id: str) -> ExecutionAlgorithm | None:
    return self.active_algorithms.get(parent_order_id)

  def execute_slice(
      self,
      parent_order_id: str,
      gateway: BrokerGateway,
      book_depth: OrderBookDepth | None = None,
      current_price: Decimal | None = None,
  ) -> BrokerOrderResponse | None:
    """Evaluates pre-trade microstructure and executes the next pending child slice."""
    assert_paper_mode()
    algo = self.active_algorithms.get(parent_order_id)
    if not algo or algo.status != "ACTIVE":
      return None

    # 1. Check max slippage abort condition
    if current_price is not None:
      aborted, reason = algo.check_abort_condition(current_price)
      if aborted:
        return None

    # 2. Get next pending child slice
    slice_item = algo.get_next_pending_slice()
    if not slice_item or slice_item.quantity <= 0:
      return None

    # 3. Microstructure Liquidity Gate evaluation
    if book_depth is not None:
      snapshot = analyze_order_book(book_depth)
      is_approved, reason, details = self.liquidity_gate.evaluate_slice(
          snapshot=snapshot,
          slice_quantity=slice_item.quantity,
          side=algo.parent.side,
      )
      if not is_approved:
        slice_item.status = "DEFERRED"
        slice_item.rejection_reason = reason
        return None

    # 4. Route child order to BrokerGateway
    slice_price = current_price or slice_item.target_price or Decimal("1000.00")
    order_req = BrokerOrderRequest(
        symbol=algo.parent.symbol,
        side=algo.parent.side,
        quantity=slice_item.quantity,
        order_type="MARKET",
        price=slice_price,
        idempotency_key=slice_item.slice_id,
        tag=f"ALGO_{algo.parent.algo_type}",
    )
    slice_item.status = "SUBMITTED"
    resp = gateway.place_order(order_req)

    # 5. Record fill state
    if resp.status == "FILLED":
      algo.record_slice_fill(
          slice_id=slice_item.slice_id,
          filled_quantity=resp.filled_quantity,
          average_price=resp.average_price,
          broker_order_id=resp.broker_order_id,
          timestamp_utc=resp.timestamp_utc,
      )

    return resp
