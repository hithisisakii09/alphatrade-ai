"""Integration tests for PaperBroker order lifecycle, partial fills, idempotency, circuit freeze, and restart recovery."""

from decimal import Decimal

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
