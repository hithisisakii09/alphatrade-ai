"""Paper Broker & Order Lifecycle State Machine with Idempotency and Restart Recovery."""

from datetime import datetime
from decimal import Decimal
import uuid
from app.core.database import Session
from app.core.database import FillRecord, OrderRecord, PositionRecord
from app.core.execution_guard import assert_paper_mode
from app.core.money import round_paise, to_decimal
from app.execution.costs import NSECostEngine
from app.portfolio.ledger import PortfolioLedger
from app.risk.engine import RiskEngine

LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "CREATED": {"VALIDATED", "REJECTED", "CANCELLED"},
    "VALIDATED": {"ACKNOWLEDGED", "REJECTED", "CANCELLED"},
    "ACKNOWLEDGED": {"PARTIALLY_FILLED", "FILLED", "CANCELLED", "EXPIRED", "REJECTED"},
    "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "FILLED", "CANCELLED", "EXPIRED"},
    "FILLED": set(),
    "REJECTED": set(),
    "CANCELLED": set(),
    "EXPIRED": set(),
}


class InvalidOrderTransitionError(RuntimeError):
  """Raised when an invalid order state transition is attempted."""


class PaperBroker:
  """Simulates realistic order lifecycle, partial/full fills, circuit freezes, and ledger updates."""

  def __init__(
      self,
      db: Session,
      ledger: PortfolioLedger,
      cost_engine: NSECostEngine,
      risk_engine: RiskEngine,
  ):
    assert_paper_mode()
    self.db = db
    self.ledger = ledger
    self.cost_engine = cost_engine
    self.risk_engine = risk_engine

  def transition_order(self, order: OrderRecord, new_status: str, timestamp_utc: datetime, reason: str | None = None) -> None:
    allowed = LEGAL_TRANSITIONS.get(order.status, set())
    if new_status not in allowed:
      raise InvalidOrderTransitionError(
          f"Illegal order state transition from {order.status} to {new_status} for {order.order_id}"
      )
    order.status = new_status
    order.updated_at_utc = timestamp_utc
    if reason:
      order.reason = reason
    self.db.flush()

  def submit_order(
      self,
      idempotency_key: str,
      symbol: str,
      session_date: str,
      side: str,
      quantity: int,
      reference_price: Decimal,
      timestamp_utc: datetime,
      signal_id: str | None = None,
  ) -> tuple[OrderRecord, bool]:
    """Submits an order idempotently. Returns (OrderRecord, is_new)."""
    existing = (
        self.db.query(OrderRecord).filter(OrderRecord.idempotency_key == idempotency_key).first()
    )
    if existing is not None:
      return existing, False

    order = OrderRecord(
        order_id=f"ORD_{uuid.uuid4().hex[:12]}",
        idempotency_key=idempotency_key,
        signal_id=signal_id,
        symbol=symbol,
        session_date=session_date,
        side=side.upper(),
        order_type="MARKET_NEXT_BAR",
        requested_qty=quantity,
        filled_qty=0,
        reference_price=to_decimal(reference_price),
        status="CREATED",
        reason=None,
        created_at_utc=timestamp_utc,
        updated_at_utc=timestamp_utc,
    )
    self.db.add(order)
    self.db.flush()

    if quantity <= 0:
      self.transition_order(order, "REJECTED", timestamp_utc, reason="INVALID_ZERO_OR_NEGATIVE_QTY")
      self.db.commit()
      return order, True

    self.transition_order(order, "VALIDATED", timestamp_utc)
    self.transition_order(order, "ACKNOWLEDGED", timestamp_utc)
    self.db.commit()
    return order, True

  def execute_fill_on_bar(
      self,
      order: OrderRecord,
      bar_open_price: Decimal,
      timestamp_utc: datetime,
      fill_qty: int | None = None,
      orb_low: Decimal | None = None,
      upper_circuit: Decimal | None = None,
      lower_circuit: Decimal | None = None,
      custom_slippage_pct: Decimal | None = None,
  ) -> FillRecord | None:
    """Executes a full or partial fill at the next-bar open price, respecting circuit limits."""
    if order.status not in ("ACKNOWLEDGED", "PARTIALLY_FILLED"):
      raise InvalidOrderTransitionError(f"Cannot execute fill on order in state {order.status}")

    raw_p = to_decimal(bar_open_price)
    # Circuit freeze check: buyers cannot fill at upper circuit; sellers cannot fill at lower circuit
    if order.side == "BUY" and upper_circuit is not None and raw_p >= to_decimal(upper_circuit):
      self.transition_order(order, "REJECTED" if order.filled_qty == 0 else "EXPIRED", timestamp_utc, "UPPER_CIRCUIT_FREEZE")
      self.db.commit()
      return None
    if order.side == "SELL" and lower_circuit is not None and raw_p <= to_decimal(lower_circuit):
      # Lower circuit freeze: order stays pending/unfilled until liquidity returns
      order.reason = "LOWER_CIRCUIT_LIQUIDITY_FREEZE"
      self.db.commit()
      return None

    remaining_qty = order.requested_qty - order.filled_qty
    exec_qty = min(remaining_qty, fill_qty if fill_qty is not None else remaining_qty)
    if exec_qty <= 0:
      return None

    cost_bd = self.cost_engine.compute_leg_costs(
        order.side, exec_qty, raw_p, custom_slippage_pct=custom_slippage_pct
    )

    fill = FillRecord(
        fill_id=f"FIL_{uuid.uuid4().hex[:12]}",
        order_id=order.order_id,
        symbol=order.symbol,
        side=order.side,
        quantity=exec_qty,
        raw_bar_price=raw_p,
        fill_price=cost_bd.effective_fill_price,
        spread_cost_inr=cost_bd.spread_cost_inr,
        slippage_cost_inr=cost_bd.slippage_cost_inr,
        brokerage_inr=cost_bd.brokerage_inr,
        statutory_taxes_inr=cost_bd.statutory_taxes_inr,
        total_fees_inr=cost_bd.total_fees_inr,
        timestamp_utc=timestamp_utc,
    )
    self.db.add(fill)
    order.filled_qty += exec_qty

    if order.filled_qty < order.requested_qty:
      self.transition_order(order, "PARTIALLY_FILLED", timestamp_utc)
    else:
      self.transition_order(order, "FILLED", timestamp_utc)

    if order.side == "BUY":
      # Debit turnover and fees from cash ledger
      self.ledger.record_ledger_movement(
          timestamp_utc=timestamp_utc,
          entry_type="BUY_DEBIT",
          amount_inr=-cost_bd.turnover_inr,
          symbol=order.symbol,
          reference_id=fill.fill_id,
          description=f"BUY {exec_qty} {order.symbol} @ {cost_bd.effective_fill_price}",
      )
      self.ledger.record_ledger_movement(
          timestamp_utc=timestamp_utc,
          entry_type="FEE_DEBIT",
          amount_inr=-cost_bd.total_fees_inr,
          symbol=order.symbol,
          reference_id=fill.fill_id,
          description=f"BUY fees for {order.symbol}",
      )
      self._open_or_augment_position_from_fill(order, fill, orb_low=orb_low)
      self.risk_engine.record_order_accepted(order.session_date, order.symbol)
    else:
      # Credit sell turnover and debit fees
      self.ledger.record_ledger_movement(
          timestamp_utc=timestamp_utc,
          entry_type="SELL_CREDIT",
          amount_inr=cost_bd.turnover_inr,
          symbol=order.symbol,
          reference_id=fill.fill_id,
          description=f"SELL {exec_qty} {order.symbol} @ {cost_bd.effective_fill_price}",
      )
      self.ledger.record_ledger_movement(
          timestamp_utc=timestamp_utc,
          entry_type="FEE_DEBIT",
          amount_inr=-cost_bd.total_fees_inr,
          symbol=order.symbol,
          reference_id=fill.fill_id,
          description=f"SELL fees for {order.symbol}",
      )
      self._close_position_from_fill(order, fill)

    self.db.commit()
    return fill

  def _open_or_augment_position_from_fill(
      self, order: OrderRecord, fill: FillRecord, orb_low: Decimal | None = None
  ) -> PositionRecord:
    pos = self.ledger.get_open_position_by_symbol(order.symbol)
    levels = self.risk_engine.compute_exit_levels(fill.fill_price, orb_low=orb_low)
    if pos is None:
      pos = PositionRecord(
          position_id=f"POS_{uuid.uuid4().hex[:12]}",
          symbol=order.symbol,
          session_date=order.session_date,
          status="OPEN",
          quantity=fill.quantity,
          entry_price=fill.fill_price,
          exit_price=None,
          initial_stop_price=levels.initial_stop_price,
          current_stop_price=levels.initial_stop_price,
          trailing_activation_price=levels.trailing_activation_price,
          trailing_activated=False,
          highest_price_since_entry=fill.fill_price,
          trailing_drop_pct=levels.trailing_drop_pct,
          entry_fees_inr=fill.total_fees_inr,
          exit_fees_inr=Decimal("0.00"),
          realized_pnl_inr=Decimal("0.00"),
          unrealized_pnl_inr=-fill.total_fees_inr,
          exit_reason=None,
          opened_at_utc=fill.timestamp_utc,
          closed_at_utc=None,
      )
      self.db.add(pos)
      self.ledger.register_open_position(pos)
    else:
      # Partial fill completion on the same entry order
      total_qty = pos.quantity + fill.quantity
      weighted_entry = (
          (to_decimal(pos.entry_price) * Decimal(pos.quantity))
          + (to_decimal(fill.fill_price) * Decimal(fill.quantity))
      ) / Decimal(total_qty)
      new_levels = self.risk_engine.compute_exit_levels(weighted_entry, orb_low=orb_low)
      pos.quantity = total_qty
      pos.entry_price = weighted_entry
      pos.initial_stop_price = new_levels.initial_stop_price
      pos.current_stop_price = new_levels.initial_stop_price
      pos.trailing_activation_price = new_levels.trailing_activation_price
      pos.entry_fees_inr = round_paise(to_decimal(pos.entry_fees_inr) + to_decimal(fill.total_fees_inr))
    self.db.flush()
    return pos

  def _close_position_from_fill(self, order: OrderRecord, fill: FillRecord) -> PositionRecord:
    pos = self.ledger.get_open_position_by_symbol(order.symbol)
    if pos is None:
      raise RuntimeError(f"Cannot process SELL fill for {order.symbol}: no open position found")
    gross_pnl = (to_decimal(fill.fill_price) - to_decimal(pos.entry_price)) * Decimal(fill.quantity)
    total_fees = to_decimal(pos.entry_fees_inr) + to_decimal(fill.total_fees_inr)
    net_pnl = round_paise(gross_pnl - total_fees)

    pos.exit_price = fill.fill_price
    pos.exit_fees_inr = fill.total_fees_inr
    pos.realized_pnl_inr = net_pnl
    pos.unrealized_pnl_inr = Decimal("0.00")
    pos.status = "CLOSED"
    pos.exit_reason = order.reason or "STRATEGY_EXIT"
    pos.closed_at_utc = fill.timestamp_utc
    self.db.flush()
    self.ledger.register_closed_position(pos)
    return pos

  def recover_state_after_restart(self) -> dict[str, PositionRecord]:
    """Recovers open positions and their persisted trailing stop state from the database."""
    open_positions = self.ledger.get_open_positions()
    for pos in open_positions:
      if pos.session_date:
        self.risk_engine.record_order_accepted(pos.session_date, pos.symbol)
    return {p.symbol: p for p in open_positions}
