"""Safety and mandatory edge-case tests from Section 13.1 (LIVE guard, kill switch, rejected orders, EOD close, FastAPI)."""

from decimal import Decimal
from fastapi.testclient import TestClient

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
from app.core.execution_guard import LiveTradingProhibitedError, assert_paper_mode
from app.execution.costs import NSECostEngine
from app.execution.paper_broker import PaperBroker
from app.main import app
from app.portfolio.ledger import PortfolioLedger
from app.risk.engine import RiskEngine


def test_live_mode_rejected_at_startup_and_by_guard(monkeypatch):
  with _RaisesContext(LiveTradingProhibitedError):
    assert_paper_mode("LIVE")
  with _RaisesContext(LiveTradingProhibitedError):
    load_app_config(execution_mode="LIVE")
  monkeypatch.setenv("ALPHATRADE_EXECUTION_MODE", "LIVE")
  with _RaisesContext(LiveTradingProhibitedError):
    assert_paper_mode()


def test_rejected_orders_do_not_count_as_completed_entries_and_kill_switch_edges():
  cfg = load_app_config()
  prof = cfg.strategy.risk_profiles["STANDARD_RESEARCH_100K"]
  cost_eng = NSECostEngine(cfg.costs["PERCENTAGE_CAPPED_DEFAULT"])
  risk_eng = RiskEngine(prof, cost_eng)
  _, SessionLocal = init_db("sqlite:///:memory:")

  with SessionLocal() as db:
    ledger = PortfolioLedger(db, prof.reference_capital)
    broker = PaperBroker(db, ledger, cost_eng, risk_eng)
    ts = ist_datetime(2026, 11, 10, 10, 0)

    # 1. Submit an order that gets rejected due to upper circuit freeze
    ord_rej, _ = broker.submit_order(
        idempotency_key="REJ_SBIN_1",
        symbol="SBIN",
        session_date="2026-11-10",
        side="BUY",
        quantity=10,
        reference_price=Decimal("850.00"),
        timestamp_utc=ts,
    )
    fill_rej = broker.execute_fill_on_bar(
        ord_rej, bar_open_price=Decimal("880.00"), timestamp_utc=ts, upper_circuit=Decimal("880.00")
    )
    assert fill_rej is None and ord_rej.status == "REJECTED"

    # Because ord_rej was rejected, SBIN is still eligible for an entry that day
    dec_retry = risk_eng.evaluate_entry_signal(
        symbol="SBIN",
        session_date="2026-11-10",
        signal_timestamp_utc=ts,
        candidate_entry_price=Decimal("850.00"),
        orb_low=Decimal("840.00"),
        current_capital_inr=prof.reference_capital,
        available_cash_inr=ledger.get_cash_balance(),
        deployed_exposure_inr=Decimal("0"),
        open_positions_Symbols=set(),
        daily_realized_pnl_inr=Decimal("0"),
        daily_unrealized_pnl_inr=Decimal("0"),
    )
    assert dec_retry.approved is True

    # 2. Quantity below one share rejected when price > available cash
    dec_expensive = risk_eng.evaluate_entry_signal(
        symbol="MRF",
        session_date="2026-11-10",
        signal_timestamp_utc=ts,
        candidate_entry_price=Decimal("125000.00"),
        orb_low=Decimal("124000.00"),
        current_capital_inr=prof.reference_capital,
        available_cash_inr=Decimal("50000.00"),
        deployed_exposure_inr=Decimal("0"),
        open_positions_Symbols=set(),
        daily_realized_pnl_inr=Decimal("0"),
        daily_unrealized_pnl_inr=Decimal("0"),
    )
    assert not dec_expensive.approved
    assert dec_expensive.reason_code == "QUANTITY_BELOW_ONE_SHARE"

    # 3. Daily loss limit reached using realized + unrealized P&L (-1,500 on 1,00,000) triggers kill switch
    dec_loss = risk_eng.evaluate_entry_signal(
        symbol="INFY",
        session_date="2026-11-10",
        signal_timestamp_utc=ts,
        candidate_entry_price=Decimal("1500.00"),
        orb_low=Decimal("1485.00"),
        current_capital_inr=prof.reference_capital,
        available_cash_inr=ledger.get_cash_balance(),
        deployed_exposure_inr=Decimal("0"),
        open_positions_Symbols=set(),
        daily_realized_pnl_inr=Decimal("-900.00"),
        daily_unrealized_pnl_inr=Decimal("-650.00"),
    )
    assert not dec_loss.approved
    assert risk_eng.kill_switch_active is True
    assert "DAILY_LOSS_LIMIT_REACHED" in dec_loss.reason_code

    # 4. EOD liquidation still functions at 15:00 IST even when kill switch is active
    ev_eod = risk_eng.evaluate_position_bar_1m(
        bar_open=Decimal("1505.00"),
        bar_high=Decimal("1506.00"),
        bar_low=Decimal("1503.00"),
        bar_close=Decimal("1504.00"),
        bar_timestamp_utc=ist_datetime(2026, 11, 10, 15, 0),
        initial_stop_price=Decimal("1481.25"),
        trailing_activation_price=Decimal("1537.50"),
        trailing_drop_pct=Decimal("0.0075"),
        trailing_activated=False,
        highest_price_since_entry=Decimal("1505.00"),
    )
    assert ev_eod.should_exit is True
    assert ev_eod.exit_reason == "EOD_FORCED_LIQUIDATION_1500"


def test_all_11_readonly_fastapi_endpoints():
  client = TestClient(app)
  for endpoint in [
      "/health",
      "/market/status",
      "/market/data-quality",
      "/strategy/status",
      "/signals",
      "/risk/status",
      "/orders",
      "/positions",
      "/portfolio",
      "/reports/daily",
      "/backtests",
      "/",
  ]:
    resp = client.get(endpoint)
    assert resp.status_code == 200, f"Endpoint {endpoint} failed with {resp.status_code}"
