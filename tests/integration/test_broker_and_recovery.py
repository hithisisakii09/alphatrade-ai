"""Integration tests for PaperBroker order lifecycle, partial fills, idempotency, circuit freeze, and restart recovery."""

from decimal import Decimal
import subprocess
import sys

class _RaisesContext:
  def __init__(self, exc_type, match=None):
    self.exc_type = exc_type
    self.match = match

  def __enter__(self):
    return self

  def __exit__(self, exc_type, exc_val, exc_tb):
    if exc_type is None:
      raise AssertionError(f"Expected {self.exc_type.__name__} to be raised, but no exception was raised.")
    if not issubclass(exc_type, self.exc_type):
      return False
    if self.match and self.match not in str(exc_val):
      raise AssertionError(f"Expected exception message matching {self.match!r}, got {str(exc_val)!r}")
    return True

from app.core.clock import ist_datetime
from app.core.config import load_app_config
from app.core.database import init_db
from app.execution.costs import NSECostEngine
from app.execution.paper_broker import InvalidOrderTransitionError, PaperBroker
from app.portfolio.ledger import PortfolioLedger
from app.risk.engine import RiskEngine


def test_idempotency_partial_fills_and_restart_recovery():
  cfg = load_app_config()
  prof = cfg.strategy.risk_profiles["STANDARD_RESEARCH_100K"]
  cost_eng = NSECostEngine(cfg.costs["PERCENTAGE_CAPPED_DEFAULT"])
  risk_eng = RiskEngine(prof, cost_eng)
  _, SessionLocal = init_db("sqlite:///:memory:")

  with SessionLocal() as db:
    ledger = PortfolioLedger(db, prof.reference_capital)
    broker = PaperBroker(db, ledger, cost_eng, risk_eng)
    ts = ist_datetime(2026, 11, 10, 9, 35)

    # 1. Idempotent order submission
    ord1, is_new1 = broker.submit_order(
        idempotency_key="IDEMP_RELIANCE_20261110_0935",
        symbol="RELIANCE",
        session_date="2026-11-10",
        side="BUY",
        quantity=20,
        reference_price=Decimal("1250.00"),
        timestamp_utc=ts,
    )
    ord_dup, is_new2 = broker.submit_order(
        idempotency_key="IDEMP_RELIANCE_20261110_0935",
        symbol="RELIANCE",
        session_date="2026-11-10",
        side="BUY",
        quantity=20,
        reference_price=Decimal("1250.00"),
        timestamp_utc=ts,
    )
    assert is_new1 and not is_new2
    assert ord1.order_id == ord_dup.order_id

    # 2. Partial fill followed by second fill
    f1 = broker.execute_fill_on_bar(ord1, Decimal("1250.00"), ts, fill_qty=12)
    assert f1 is not None and ord1.status == "PARTIALLY_FILLED" and ord1.filled_qty == 12
    f2 = broker.execute_fill_on_bar(ord1, Decimal("1251.00"), ts, fill_qty=8)
    assert f2 is not None and ord1.status == "FILLED" and ord1.filled_qty == 20

    # 3. Update trailing state and simulate midday application restart
    pos = ledger.get_open_position_by_symbol("RELIANCE")
    assert pos is not None and pos.quantity == 20
    pos.trailing_activated = True
    pos.highest_price_since_entry = Decimal("1295.00")
    pos.current_stop_price = Decimal("1285.29")
    db.commit()

    # Simulate new broker & risk engine instance recovering from same DB
    risk_eng_restarted = RiskEngine(prof, cost_eng)
    broker_restarted = PaperBroker(db, ledger, cost_eng, risk_eng_restarted)
    recovered = broker_restarted.recover_state_after_restart()
    assert "RELIANCE" in recovered
    assert recovered["RELIANCE"].trailing_activated is True
    assert recovered["RELIANCE"].highest_price_since_entry == Decimal("1295.0000")

    # Symbol already had an accepted entry today -> new entry blocked after restart
    dec_after_restart = risk_eng_restarted.evaluate_entry_signal(
        symbol="RELIANCE",
        session_date="2026-11-10",
        signal_timestamp_utc=ist_datetime(2026, 11, 10, 11, 0),
        candidate_entry_price=Decimal("1260.00"),
        orb_low=Decimal("1245.00"),
        current_capital_inr=prof.reference_capital,
        available_cash_inr=ledger.get_cash_balance(),
        deployed_exposure_inr=ledger.get_deployed_exposure(),
        open_positions_Symbols=set(),
        daily_realized_pnl_inr=Decimal("0"),
        daily_unrealized_pnl_inr=Decimal("0"),
    )
    assert not dec_after_restart.approved
    assert dec_after_restart.reason_code == "MAX_ONE_ENTRY_PER_SYMBOL_PER_DAY"

    # Illegal transition raises InvalidOrderTransitionError
    with _RaisesContext(InvalidOrderTransitionError):
      broker.transition_order(ord1, "ACKNOWLEDGED", ts)


def test_cross_process_restart_persistence(tmp_path):
  db_path = str(tmp_path / "proc_restart.db").replace("\\", "/")

  script_p1 = f"""
import sys
from decimal import Decimal
from app.core.clock import ist_datetime
from app.core.config import load_app_config
from app.core.database import init_db
from app.execution.costs import NSECostEngine
from app.execution.paper_broker import PaperBroker
from app.portfolio.ledger import PortfolioLedger
from app.risk.engine import RiskEngine

cfg = load_app_config()
prof = cfg.strategy.risk_profiles["STANDARD_RESEARCH_100K"]
cost_eng = NSECostEngine(cfg.costs["PERCENTAGE_CAPPED_DEFAULT"])
risk_eng = RiskEngine(prof, cost_eng)
_, SessionLocal = init_db("sqlite:///{db_path}")

with SessionLocal() as db:
  ledger = PortfolioLedger(db, prof.reference_capital)
  broker = PaperBroker(db, ledger, cost_eng, risk_eng)
  ts = ist_datetime(2026, 11, 10, 9, 35)

  ord1, is_new = broker.submit_order(
      idempotency_key="IDEMP_CROSS_PROC_RELIANCE",
      symbol="RELIANCE",
      session_date="2026-11-10",
      side="BUY",
      quantity=20,
      reference_price=Decimal("1250.00"),
      timestamp_utc=ts,
  )
  assert is_new
  broker.execute_fill_on_bar(ord1, Decimal("1250.00"), ts, fill_qty=12)
  broker.execute_fill_on_bar(ord1, Decimal("1251.00"), ts, fill_qty=8)

  pos = ledger.get_open_position_by_symbol("RELIANCE")
  assert pos is not None and pos.quantity == 20
  pos.trailing_activated = True
  pos.highest_price_since_entry = Decimal("1295.00")
  pos.current_stop_price = Decimal("1285.29")
  db.commit()

print("PROCESS_1_SUCCESS")
"""

  script_p2 = f"""
import sys, sqlite3
from decimal import Decimal
from app.core.clock import ist_datetime
from app.core.config import load_app_config
from app.core.database import init_db
from app.execution.costs import NSECostEngine
from app.execution.paper_broker import PaperBroker
from app.portfolio.ledger import PortfolioLedger
from app.risk.engine import RiskEngine

# 1. Direct SQLite table verification from disk
raw_conn = sqlite3.connect("{db_path}")
cur = raw_conn.cursor()
orders_cnt = cur.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
fills_cnt = cur.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
positions_cnt = cur.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
ledger_cnt = cur.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0]
assert orders_cnt == 1, f"Expected 1 order on disk, got {{orders_cnt}}"
assert fills_cnt == 2, f"Expected 2 fills on disk, got {{fills_cnt}}"
assert positions_cnt == 1, f"Expected 1 position on disk, got {{positions_cnt}}"
assert ledger_cnt == 5, f"Expected 5 ledger entries on disk, got {{ledger_cnt}}"
raw_conn.close()

# 2. Re-open via application Session and verify domain state recovery
cfg = load_app_config()
prof = cfg.strategy.risk_profiles["STANDARD_RESEARCH_100K"]
cost_eng = NSECostEngine(cfg.costs["PERCENTAGE_CAPPED_DEFAULT"])
risk_eng = RiskEngine(prof, cost_eng)
_, SessionLocal = init_db("sqlite:///{db_path}")

with SessionLocal() as db:
  ledger = PortfolioLedger(db, prof.reference_capital)
  broker = PaperBroker(db, ledger, cost_eng, risk_eng)
  recovered = broker.recover_state_after_restart()
  assert "RELIANCE" in recovered
  pos = recovered["RELIANCE"]
  assert pos.quantity == 20
  assert pos.trailing_activated is True
  assert pos.highest_price_since_entry == Decimal("1295.0000")
  assert pos.current_stop_price == Decimal("1285.2900")

  reconciled, msg = ledger.reconcile()
  assert reconciled is True and msg == "RECONCILED_OK"

  dec = risk_eng.evaluate_entry_signal(
      symbol="RELIANCE",
      session_date="2026-11-10",
      signal_timestamp_utc=ist_datetime(2026, 11, 10, 11, 0),
      candidate_entry_price=Decimal("1260.00"),
      orb_low=Decimal("1245.00"),
      current_capital_inr=prof.reference_capital,
      available_cash_inr=ledger.get_cash_balance(),
      deployed_exposure_inr=ledger.get_deployed_exposure(),
      open_positions_Symbols=set(),
      daily_realized_pnl_inr=Decimal("0"),
      daily_unrealized_pnl_inr=Decimal("0"),
  )
  assert not dec.approved
  assert dec.reason_code == "MAX_ONE_ENTRY_PER_SYMBOL_PER_DAY"

print("PROCESS_2_SUCCESS")
"""

  # Run Process 1
  p1 = subprocess.run(
      [sys.executable, "-c", script_p1],
      capture_output=True,
      text=True,
      check=True,
  )
  assert "PROCESS_1_SUCCESS" in p1.stdout

  # Run Process 2
  p2 = subprocess.run(
      [sys.executable, "-c", script_p2],
      capture_output=True,
      text=True,
      check=True,
  )
  assert "PROCESS_2_SUCCESS" in p2.stdout

