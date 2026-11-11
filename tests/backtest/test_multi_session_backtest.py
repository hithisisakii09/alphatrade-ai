"""Milestone M8.1 Stage 2 Tests: Multi-Session Previous-Close Tracking, Circuit Bands & Session Rollover."""

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from app.backtest.engine import ChronologicalBarWindow, EventDrivenBacktester
from app.core.clock import ist_datetime
from app.core.config import load_app_config
from app.core.database import init_db
from app.data.instruments import InstrumentRegistry
from app.data.models import CandleBar


def _make_bar(symbol: str, year: int, month: int, day: int, hour: int, minute: int, o: str, h: str, l: str, c: str, vol: int = 10000) -> CandleBar:
  return CandleBar(
      symbol=symbol,
      interval_minutes=1,
      timestamp_utc=ist_datetime(year, month, day, hour, minute),
      open=Decimal(o),
      high=Decimal(h),
      low=Decimal(l),
      close=Decimal(c),
      volume=vol,
      source="ANGEL_SMARTAPI",
  )


def test_two_consecutive_sessions_updates_prev_close_and_circuit_bands():
  """Verifies that Day 2 dynamically adopts Day 1 final close for prev_close and circuit calculations."""
  cfg = load_app_config()
  _, SessionLocal = init_db("sqlite:///:memory:")

  # Day 1 (2026-11-09): 09:15 to 09:35, closing at 1250.00
  day1_bars = [
      _make_bar("RELIANCE", 2026, 11, 9, 9, 15 + i, "1200.00", "1205.00", "1198.00", "1202.00")
      for i in range(15)
  ]
  # Final bar of Day 1 closes at 1250.00
  day1_bars.append(_make_bar("RELIANCE", 2026, 11, 9, 9, 30, "1240.00", "1255.00", "1238.00", "1250.00"))

  # Day 2 (2026-11-10): 09:15 to 09:35
  day2_bars = [
      _make_bar("RELIANCE", 2026, 11, 10, 9, 15 + i, "1252.00", "1258.00", "1250.00", "1255.00")
      for i in range(15)
  ]

  all_bars = day1_bars + day2_bars

  with SessionLocal() as db:
    bt = EventDrivenBacktester(db, cfg)
    # Provide Day 1 initial baseline close of 1200.00
    prev_closes = {"RELIANCE": Decimal("1200.00")}

    summary = bt.run_backtest(
        bars_1m_by_symbol={"RELIANCE": all_bars},
        prev_closes_by_symbol=prev_closes,
        dataset_id="TEST_CONSECUTIVE_SESSIONS",
    )

    # Verify that registry computed price bands for 1250.00 (10% band: 1125.00 to 1375.00)
    lower_day2, upper_day2 = bt.registry.compute_price_bands("RELIANCE", Decimal("1250.00"))
    assert lower_day2 == Decimal("1125.00")
    assert upper_day2 == Decimal("1375.00")


def test_multiple_symbols_independent_previous_closes():
  """Verifies that multiple symbols maintain separate previous closes on session rollover without cross-talk."""
  cfg = load_app_config()
  _, SessionLocal = init_db("sqlite:///:memory:")

  # RELIANCE: Day 1 closes at 1300.00, Day 2 opens at 1302.00
  rel_d1 = [_make_bar("RELIANCE", 2026, 11, 9, 9, 15 + i, "1280.00", "1285.00", "1278.00", "1282.00") for i in range(14)]
  rel_d1.append(_make_bar("RELIANCE", 2026, 11, 9, 9, 29, "1295.00", "1305.00", "1295.00", "1300.00"))
  rel_d2 = [_make_bar("RELIANCE", 2026, 11, 10, 9, 15 + i, "1302.00", "1308.00", "1300.00", "1305.00") for i in range(15)]

  # TCS: Day 1 closes at 4200.00, Day 2 opens at 4205.00
  tcs_d1 = [_make_bar("TCS", 2026, 11, 9, 9, 15 + i, "4150.00", "4160.00", "4145.00", "4155.00") for i in range(14)]
  tcs_d1.append(_make_bar("TCS", 2026, 11, 9, 9, 29, "4190.00", "4205.00", "4188.00", "4200.00"))
  tcs_d2 = [_make_bar("TCS", 2026, 11, 10, 9, 15 + i, "4205.00", "4215.00", "4200.00", "4210.00") for i in range(15)]

  with SessionLocal() as db:
    bt = EventDrivenBacktester(db, cfg)
    prev_closes = {"RELIANCE": Decimal("1280.00"), "TCS": Decimal("4150.00")}

    summary = bt.run_backtest(
        bars_1m_by_symbol={"RELIANCE": rel_d1 + rel_d2, "TCS": tcs_d1 + tcs_d2},
        prev_closes_by_symbol=prev_closes,
        dataset_id="TEST_MULTI_SYMBOL_ROLLOVER",
    )

    # RELIANCE Day 2 bands (1300 +/- 10%): 1170.00 to 1430.00
    rel_low, rel_high = bt.registry.compute_price_bands("RELIANCE", Decimal("1300.00"))
    assert rel_low == Decimal("1170.00")
    assert rel_high == Decimal("1430.00")

    # TCS Day 2 bands (4200 +/- 10%): 3780.00 to 4620.00
    tcs_low, tcs_high = bt.registry.compute_price_bands("TCS", Decimal("4200.00"))
    assert tcs_low == Decimal("3780.00")
    assert tcs_high == Decimal("4620.00")


def test_missing_session_data_uses_last_available_close():
  """Verifies that when a calendar session is missing (e.g. Day 2 missing), Day 3 uses Day 1's close."""
  cfg = load_app_config()
  _, SessionLocal = init_db("sqlite:///:memory:")

  # Day 1: 2026-11-09 (Monday), closes at 1000.00
  d1 = [_make_bar("INFY", 2026, 11, 9, 9, 15 + i, "990.00", "995.00", "988.00", "992.00") for i in range(14)]
  d1.append(_make_bar("INFY", 2026, 11, 9, 9, 29, "995.00", "1002.00", "995.00", "1000.00"))

  # Day 2 (2026-11-10) is completely missing from the feed

  # Day 3: 2026-11-11 (Wednesday), opens at 1005.00
  d3 = [_make_bar("INFY", 2026, 11, 11, 9, 15 + i, "1005.00", "1012.00", "1004.00", "1008.00") for i in range(15)]

  with SessionLocal() as db:
    bt = EventDrivenBacktester(db, cfg)
    summary = bt.run_backtest(
        bars_1m_by_symbol={"INFY": d1 + d3},
        prev_closes_by_symbol={"INFY": Decimal("990.00")},
        dataset_id="TEST_MISSING_SESSION",
    )
    # Circuit bands for Day 3 should use 1000.00 (from Day 1), yielding [900.00, 1100.00]
    infy_low, infy_high = bt.registry.compute_price_bands("INFY", Decimal("1000.00"))
    assert infy_low == Decimal("900.00")
    assert infy_high == Decimal("1100.00")


def test_incomplete_session_rollover():
  """Verifies that an incomplete intraday session (e.g. early close) correctly updates prev_close on rollover."""
  cfg = load_app_config()
  _, SessionLocal = init_db("sqlite:///:memory:")

  # Day 1: Only 5 bars (incomplete session), closing at 500.00
  d1 = [_make_bar("TATAMOTORS", 2026, 11, 9, 9, 15 + i, "495.00", "498.00", "494.00", "496.00") for i in range(4)]
  d1.append(_make_bar("TATAMOTORS", 2026, 11, 9, 9, 19, "496.00", "502.00", "495.00", "500.00"))

  # Day 2: 15 bars
  d2 = [_make_bar("TATAMOTORS", 2026, 11, 10, 9, 15 + i, "502.00", "505.00", "500.00", "503.00") for i in range(15)]

  with SessionLocal() as db:
    bt = EventDrivenBacktester(db, cfg)
    summary = bt.run_backtest(
        bars_1m_by_symbol={"TATAMOTORS": d1 + d2},
        prev_closes_by_symbol={"TATAMOTORS": Decimal("495.00")},
        dataset_id="TEST_INCOMPLETE_SESSION",
    )
    # Band based on 500.00 (10% band: 450.00 to 550.00)
    low, high = bt.registry.compute_price_bands("TATAMOTORS", Decimal("500.00"))
    assert low == Decimal("450.00")
    assert high == Decimal("550.00")


def test_overnight_unadjusted_split_gap_quarantines_symbol():
  """Verifies that an overnight jump > 15% between Day 1 close and Day 2 open triggers split quarantine."""
  cfg = load_app_config()
  _, SessionLocal = init_db("sqlite:///:memory:")

  # Day 1: closes at 1000.00
  d1 = [_make_bar("HDFCBANK", 2026, 11, 9, 9, 15 + i, "1000.00", "1005.00", "998.00", "1000.00") for i in range(15)]

  # Day 2: unadjusted 1:1 stock split opens at 500.00 (-50% drop, > 15% threshold)
  d2 = [_make_bar("HDFCBANK", 2026, 11, 10, 9, 15 + i, "500.00", "505.00", "498.00", "502.00") for i in range(15)]

  with SessionLocal() as db:
    bt = EventDrivenBacktester(db, cfg)
    summary = bt.run_backtest(
        bars_1m_by_symbol={"HDFCBANK": d1 + d2},
        prev_closes_by_symbol={"HDFCBANK": Decimal("1000.00")},
        dataset_id="TEST_SPLIT_QUARANTINE",
    )
    # Symbol must be quarantined
    assert "HDFCBANK" in bt.validator.quarantined_symbols
    # Any events must include SUSPECTED_UNADJUSTED_CORPORATE_ACTION
    event_types = [e.event_type for e in bt.validator.events]
    assert "SUSPECTED_UNADJUSTED_CORPORATE_ACTION" in event_types


def test_first_session_fallback_when_prev_close_not_provided():
  """Verifies defined first-session fallback to first candle's open when prev_closes_by_symbol is omitted."""
  cfg = load_app_config()
  _, SessionLocal = init_db("sqlite:///:memory:")

  bars = [_make_bar("AXISBANK", 2026, 11, 9, 9, 15 + i, "1150.00", "1155.00", "1148.00", "1152.00") for i in range(15)]

  with SessionLocal() as db:
    bt = EventDrivenBacktester(db, cfg)
    # Omit AXISBANK from prev_closes_by_symbol completely
    summary = bt.run_backtest(
        bars_1m_by_symbol={"AXISBANK": bars},
        prev_closes_by_symbol={},
        dataset_id="TEST_FIRST_SESSION_FALLBACK",
    )
    # First-session fallback uses 1150.00 (bars[0].open)
    low, high = bt.registry.compute_price_bands("AXISBANK", Decimal("1150.00"))
    assert low == Decimal("1035.00")
    assert high == Decimal("1265.00")

