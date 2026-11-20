"""Unit tests for NIFTY Index Options Momentum Strategy and Option Chain Resolver."""

from datetime import datetime, timezone, time
from decimal import Decimal
import pytest

from app.data.option_chain import AngelOptionChainResolver, OptionContract
from app.strategy.options_momentum import (
    OptionPositionTracker,
    NiftyOptionsMomentumEngine,
)


def test_option_position_tracker_initial_bounds():
  """Verifies stop loss, target, and trailing activation calculations."""
  pos = OptionPositionTracker(
      position_id="TEST_POS_1",
      symbol="NIFTY23NOV2624500CE",
      token="64413",
      option_type="CE",
      strike=Decimal("24500.00"),
      expiry="23NOV2026",
      quantity=65,
      entry_price=Decimal("16.00"),
      entry_time=datetime.now(timezone.utc),
  )

  # Hard Stop = -25% of 16.00 = 12.00
  assert pos.initial_stop_price == Decimal("12.00")
  assert pos.current_stop_price == Decimal("12.00")

  # Target 1 = +50% of 16.00 = 24.00
  assert pos.target_price_1 == Decimal("24.00")

  # Trailing activation = +30% of 16.00 = 20.80
  assert pos.trailing_activation_price == Decimal("20.80")
  assert pos.trailing_activated is False


def test_option_position_tracker_trailing_ratchet():
  """Verifies that trailing activates at +30% profit and moves stop to breakeven then trails."""
  pos = OptionPositionTracker(
      position_id="TEST_POS_2",
      symbol="NIFTY23NOV2624500CE",
      token="64413",
      option_type="CE",
      strike=Decimal("24500.00"),
      expiry="23NOV2026",
      quantity=65,
      entry_price=Decimal("16.00"),
      entry_time=datetime.now(timezone.utc),
  )

  # Price moves to 18.00 (below 20.80 activation)
  pos.update_mark_price(Decimal("18.00"))
  assert pos.trailing_activated is False
  assert pos.current_stop_price == Decimal("12.00")

  # Price surges to 22.00 (> 20.80 activation)
  pos.update_mark_price(Decimal("22.00"))
  assert pos.trailing_activated is True
  # Stop moves to breakeven (16.00) or 90% of 22 (19.80) -> whichever is higher: 19.80!
  assert pos.current_stop_price == Decimal("19.80")

  # Price surges to 30.00
  pos.update_mark_price(Decimal("30.00"))
  # 90% of 30.00 = 27.00
  assert pos.current_stop_price == Decimal("27.00")

  # Price retraces to 27.00 -> hits trailing stop
  exit_reason = pos.check_exit_trigger(Decimal("27.00"), time(11, 30))
  assert exit_reason == "TRAILING_PROFIT_STOP_HIT"


def test_option_position_tracker_eod_exit():
  """Verifies that 15:10 IST mandates an immediate EOD exit."""
  pos = OptionPositionTracker(
      position_id="TEST_POS_3",
      symbol="NIFTY23NOV2624500CE",
      token="64413",
      option_type="CE",
      strike=Decimal("24500.00"),
      expiry="23NOV2026",
      quantity=65,
      entry_price=Decimal("16.00"),
      entry_time=datetime.now(timezone.utc),
  )

  exit_reason = pos.check_exit_trigger(Decimal("16.50"), time(15, 12))
  assert exit_reason == "EOD_FORCED_LIQUIDATION_1510"


def test_nifty_options_momentum_engine_orb_breakout():
  """Verifies ORB formation and breakout order dispatch for NIFTY options."""
  engine = NiftyOptionsMomentumEngine()

  # Create mock 09:15-09:30 candles
  candles = []
  base_time = datetime(2026, 11, 20, 3, 45, tzinfo=timezone.utc)  # 09:15 IST
  for i in range(15):
    candles.append({
        "timestamp_utc": datetime(2026, 11, 20, 3, 45 + i, tzinfo=timezone.utc),
        "open": 24300.0,
        "high": 24320.0 if i == 5 else 24310.0,
        "low": 24280.0 if i == 2 else 24295.0,
        "close": 24305.0,
        "volume": 5000,
    })

  assert engine.update_opening_range(candles) is True
  assert engine.orb_high == Decimal("24320.0")
  assert engine.orb_low == Decimal("24280.0")

  # Test Bullish Breakout above ORB High
  order = engine.evaluate_breakout(
      current_spot_price=Decimal("24335.0"),
      current_candle_close=Decimal("24335.0"),
      current_rvol=1.5,
      current_time_utc=datetime(2026, 11, 20, 4, 15, tzinfo=timezone.utc),  # 09:45 IST
  )

  assert order is not None
  assert order.side == "BUY"
  assert order.exchange == "NFO"
  assert "CE" in order.symbol
  assert order.quantity == 65
  assert order.symbol_token is not None


def test_nifty_options_trade_lifecycle():
  """Simulates a complete profitable option trade lifecycle from entry to exit."""
  engine = NiftyOptionsMomentumEngine()
  contract = OptionContract(
      symbol="NIFTY23NOV2624400CE",
      token="64413",
      name="NIFTY",
      strike=Decimal("24400.00"),
      option_type="CE",
      expiry="23NOV2026",
      lotsize=65,
      tick_size=Decimal("0.05"),
  )

  # 1. Fill Entry @ 16.00
  pos = engine.on_order_filled(
      contract=contract,
      fill_price=Decimal("16.00"),
      fill_quantity=65,
      fill_time_utc=datetime(2026, 11, 20, 4, 16, tzinfo=timezone.utc),
  )
  assert engine.active_position is not None
  assert engine.session_trades_count == 1

  # 2. Price rallies to 28.00 (Target 1 achieved)
  pos.update_mark_price(Decimal("28.00"))
  assert pos.trailing_activated is True

  # 3. Close position at 28.00
  closed = engine.on_position_closed(
      exit_price=Decimal("28.00"),
      exit_reason="TARGET_1_PROFIT_LOCK",
      exit_time_utc=datetime(2026, 11, 20, 4, 45, tzinfo=timezone.utc),
  )

  assert closed is not None
  assert closed.status == "CLOSED"
  # Profit = (28.00 - 16.00) * 65 = +780.00
  assert closed.realized_pnl == Decimal("780.00")
  assert engine.session_realized_pnl == Decimal("780.00")
  assert engine.active_position is None


def test_hybrid_execution_policy_fno_paper_equity_live(monkeypatch):
  """Verifies that F&O orders are intercepted into paper trading sandbox while Angel One live wallet is guarded."""
  monkeypatch.setenv("ALPHATRADE_EXECUTION_MODE", "LIVE")
  monkeypatch.setenv("ALPHATRADE_LIVE_CONFIRMATION", "I_ACCEPT_FINANCIAL_RISK")
  monkeypatch.setenv("ALPHATRADE_FNO_PAPER_ONLY", "true")

  from app.execution.gateway import LiveAngelSmartAPIGateway, BrokerOrderRequest

  gw = LiveAngelSmartAPIGateway()
  assert gw.fno_paper_mode_only is True

  # 1. F&O Call order must be routed to paper execution
  fno_req = BrokerOrderRequest(
      symbol="NIFTY23NOV2624400CE",
      side="BUY",
      quantity=65,
      price=Decimal("9.50"),
      order_type="LIMIT",
      product_type="MIS",
      exchange="NFO",
      idempotency_key="TEST_FNO_HYBRID_BUY_01",
  )
  resp = gw.place_order(fno_req)
  assert resp.status == "FILLED"
  assert resp.broker_order_id.startswith("PAPER_FNO_")
  assert resp.filled_quantity == 65
  assert resp.average_price == Decimal("9.50")

  # 2. Status inquiry must return paper order
  status = gw.get_order_status(resp.broker_order_id)
  assert status is not None
  assert status.status == "FILLED"

  # 3. Cancellation must work on paper order
  cancelled = gw.cancel_order(resp.broker_order_id)
  assert cancelled is True
  updated_status = gw.get_order_status(resp.broker_order_id)
  assert updated_status.status == "CANCELLED"


def test_live_fno_execution_authorized_when_paper_disabled(monkeypatch):
  """Verifies that F&O orders target real Angel One gateway when paper mode is disabled."""
  monkeypatch.setenv("ALPHATRADE_EXECUTION_MODE", "LIVE")
  monkeypatch.setenv("ALPHATRADE_LIVE_CONFIRMATION", "I_ACCEPT_FINANCIAL_RISK")
  monkeypatch.setenv("ALPHATRADE_FNO_PAPER_ONLY", "false")

  from app.execution.gateway import LiveAngelSmartAPIGateway

  gw = LiveAngelSmartAPIGateway()
  assert gw.fno_paper_mode_only is False


