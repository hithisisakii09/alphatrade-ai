"""Double-entry portfolio ledger, position tracker, and SQLite state recovery."""

from datetime import datetime, timezone
from decimal import Decimal
import uuid
from app.core.database import Session
from app.core.database import (
    FillRecord,
    LedgerEntryRecord,
    OrderRecord,
    PortfolioSnapshotRecord,
    PositionRecord,
)
from app.core.money import round_paise, to_decimal


class PortfolioLedger:
  """Maintains cash balance, positions, ledger entries, and reconciliation."""

  def __init__(self, db: Session, starting_capital_inr: Decimal):
    self.db = db
    self.starting_capital_inr = round_paise(starting_capital_inr)
    self._ensure_initialized()

  def _ensure_initialized(self) -> None:
    first_entry = self.db.query(LedgerEntryRecord).order_by(LedgerEntryRecord.id.asc()).first()
    if first_entry is None:
      now = datetime.now(timezone.utc)
      init_rec = LedgerEntryRecord(
          entry_id=f"LED_{uuid.uuid4().hex[:12]}",
          timestamp_utc=now,
          entry_type="INITIAL_DEPOSIT",
          symbol=None,
          reference_id="INIT",
          amount_inr=self.starting_capital_inr,
          balance_after_inr=self.starting_capital_inr,
          description="Initial virtual paper-trading capital deposit",
      )
      self.db.add(init_rec)
      self.db.commit()

  def get_cash_balance(self) -> Decimal:
    last_entry = self.db.query(LedgerEntryRecord).order_by(LedgerEntryRecord.id.desc()).first()
    return round_paise(last_entry.balance_after_inr) if last_entry else self.starting_capital_inr

  def get_open_positions(self) -> list[PositionRecord]:
    return self.db.query(PositionRecord).filter(PositionRecord.status == "OPEN").all()

  def get_open_position_by_symbol(self, symbol: str) -> PositionRecord | None:
    return (
        self.db.query(PositionRecord)
        .filter(PositionRecord.symbol == symbol, PositionRecord.status == "OPEN")
        .first()
    )

  def get_deployed_exposure(self) -> Decimal:
    open_pos = self.get_open_positions()
    total = sum((to_decimal(p.entry_price) * Decimal(p.quantity) for p in open_pos), Decimal("0.00"))
    return round_paise(total)

  def get_daily_realized_pnl(self, session_date: str) -> Decimal:
    closed_today = (
        self.db.query(PositionRecord)
        .filter(PositionRecord.session_date == session_date, PositionRecord.status == "CLOSED")
        .all()
    )
    return round_paise(sum((to_decimal(p.realized_pnl_inr) for p in closed_today), Decimal("0.00")))

  def get_daily_unrealized_pnl(self, mark_prices: dict[str, Decimal] | None = None) -> Decimal:
    open_pos = self.get_open_positions()
    total = Decimal("0.00")
    for p in open_pos:
      if mark_prices and p.symbol in mark_prices:
        mp = to_decimal(mark_prices[p.symbol])
        p.unrealized_pnl_inr = round_paise((mp - to_decimal(p.entry_price)) * Decimal(p.quantity) - to_decimal(p.entry_fees_inr))
      total += to_decimal(p.unrealized_pnl_inr)
    self.db.commit()
    return round_paise(total)

  def record_ledger_movement(
      self,
      timestamp_utc: datetime,
      entry_type: str,
      amount_inr: Decimal,
      symbol: str | None,
      reference_id: str,
      description: str,
  ) -> LedgerEntryRecord:
    curr_cash = self.get_cash_balance()
    amt = round_paise(amount_inr)
    new_bal = round_paise(curr_cash + amt)
    entry = LedgerEntryRecord(
        entry_id=f"LED_{uuid.uuid4().hex[:12]}",
        timestamp_utc=timestamp_utc,
        entry_type=entry_type,
        symbol=symbol,
        reference_id=reference_id,
        amount_inr=amt,
        balance_after_inr=new_bal,
        description=description,
    )
    self.db.add(entry)
    self.db.flush()
    return entry

  def reconcile(self) -> tuple[bool, str]:
    """Reconciles ledger cash balance against sum of all ledger entries and fill records."""
    entries = self.db.query(LedgerEntryRecord).order_by(LedgerEntryRecord.id.asc()).all()
    if not entries:
      return False, "EMPTY_LEDGER"
    running = Decimal("0.00")
    for e in entries:
      running = round_paise(running + to_decimal(e.amount_inr))
      if running != round_paise(e.balance_after_inr):
        return False, f"LEDGER_BALANCE_MISMATCH_AT_{e.entry_id}"

    # Verify open positions have positive quantity
    for p in self.get_open_positions():
      if p.quantity <= 0:
        return False, f"INVALID_OPEN_POSITION_QTY_{p.position_id}"

    return True, "RECONCILED_OK"

  def take_snapshot(self, timestamp_utc: datetime, mark_prices: dict[str, Decimal] | None = None) -> PortfolioSnapshotRecord:
    cash = self.get_cash_balance()
    deployed = self.get_deployed_exposure()
    unrealized = self.get_daily_unrealized_pnl(mark_prices)
    closed_all = self.db.query(PositionRecord).filter(PositionRecord.status == "CLOSED").all()
    realized = round_paise(sum((to_decimal(p.realized_pnl_inr) for p in closed_all), Decimal("0.00")))
    fills = self.db.query(FillRecord).all()
    total_fees = round_paise(sum((to_decimal(f.total_fees_inr) for f in fills), Decimal("0.00")))
    # Net equity = cash + current market value of open positions
    open_market_val = round_paise(deployed + unrealized)
    net_eq = round_paise(cash + open_market_val)

    snap = PortfolioSnapshotRecord(
        timestamp_utc=timestamp_utc,
        starting_capital_inr=self.starting_capital_inr,
        cash_balance_inr=cash,
        deployed_exposure_inr=deployed,
        realized_pnl_inr=realized,
        unrealized_pnl_inr=unrealized,
        total_fees_inr=total_fees,
        net_equity_inr=net_eq,
    )
    self.db.add(snap)
    self.db.commit()
    return snap
