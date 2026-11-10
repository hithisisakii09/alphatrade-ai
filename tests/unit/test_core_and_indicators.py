"""Unit tests for Decimal money arithmetic, NSE calendar, data validation, and zero-look-ahead indicators."""

from datetime import date
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

from app.core.clock import NSECalendar, ist_datetime
from app.core.money import round_paise, round_to_tick, to_decimal
from app.data.models import CandleBar
from app.data.validator import CandleIngestionValidator, aggregate_1m_to_nm
from app.market.indicators import (
    compute_atr_series,
    compute_ema_series,
    compute_opening_range,
    compute_session_vwap,
    compute_time_of_day_rvol,
)


def test_decimal_rejects_binary_float():
  with _RaisesContext(TypeError, match="Binary float"):
    to_decimal(100.05)
  assert round_paise("100.055") == Decimal("100.06")
  assert round_to_tick(Decimal("1250.03"), Decimal("0.05")) == Decimal("1250.05")


def test_holiday_and_special_session_calendar():
  cal = NSECalendar()
  # Republic Day 2026-01-26 is holiday
  assert not cal.is_trading_day(date(2026, 1, 26))
  # Sunday 2026-11-08 is Diwali Muhurat special session (trading day, but intraday ORB disabled)
  assert cal.is_trading_day(date(2026, 11, 8))
  assert not cal.is_orb_eligible_session(date(2026, 11, 8))
  assert cal.get_session_state(ist_datetime(2026, 11, 8, 18, 15)) == "SPECIAL_SESSION"
  # Regular Tuesday 2026-11-10 session states
  assert cal.get_session_state(ist_datetime(2026, 11, 10, 9, 20)) == "OPENING_RANGE_FORMATION"
  assert cal.get_session_state(ist_datetime(2026, 11, 10, 10, 0)) == "ACTIVE_TRADING_WINDOW"
  assert cal.get_session_state(ist_datetime(2026, 11, 10, 14, 35)) == "NO_NEW_ENTRIES_MANAGE_ONLY"
  assert cal.get_session_state(ist_datetime(2026, 11, 10, 15, 5)) == "EOD_LIQUIDATION_WINDOW"


def test_validator_catches_duplicates_stale_and_unadjusted_splits():
  val = CandleIngestionValidator()
  b1 = CandleBar(
      symbol="RELIANCE",
      timestamp_utc=ist_datetime(2026, 11, 10, 9, 15),
      open=Decimal("1000"),
      high=Decimal("1005"),
      low=Decimal("998"),
      close=Decimal("1002"),
      volume=10000,
  )
  ok1, ev1 = val.validate_bar(b1)
  assert ok1 and not ev1

  # Duplicate timestamp rejected
  ok_dup, ev_dup = val.validate_bar(b1)
  assert not ok_dup and ev_dup[0].event_type == "DUPLICATE_CANDLE"

  # Stale data (>90s delayed) rejected
  b2 = CandleBar(
      symbol="RELIANCE",
      timestamp_utc=ist_datetime(2026, 11, 10, 9, 16),
      open=Decimal("1002"),
      high=Decimal("1004"),
      low=Decimal("1001"),
      close=Decimal("1003"),
      volume=8000,
  )
  ok_stale, ev_stale = val.validate_bar(b2, reference_clock_utc=ist_datetime(2026, 11, 10, 9, 19))
  assert not ok_stale and ev_stale[0].event_type == "STALE_MARKET_DATA"

  # Next day 1:2 stock split jump (-50% overnight) quarantined
  b_split = CandleBar(
      symbol="RELIANCE",
      timestamp_utc=ist_datetime(2026, 11, 11, 9, 15),
      open=Decimal("501"),
      high=Decimal("505"),
      low=Decimal("499"),
      close=Decimal("502"),
      volume=20000,
  )
  ok_sp, ev_sp = val.validate_bar(b_split)
  assert not ok_sp
  assert "RELIANCE" in val.quarantined_symbols
  assert ev_sp[0].event_type == "SUSPECTED_UNADJUSTED_CORPORATE_ACTION"


def test_indicators_vwap_ema_atr_orb_rvol():
  bars = [
      CandleBar(
          symbol="TCS",
          timestamp_utc=ist_datetime(2026, 11, 10, 9, 15 + i),
          open=Decimal(str(100 + i)),
          high=Decimal(str(102 + i)),
          low=Decimal(str(99 + i)),
          close=Decimal(str(101 + i)),
          volume=1000 * (i + 1),
      )
      for i in range(15)
  ]
  vwap = compute_session_vwap(bars)
  assert vwap > Decimal("100")
  orb = compute_opening_range(bars, 15)
  assert orb is not None and orb.is_complete
  assert orb.orb_high == Decimal("116")
  assert orb.orb_low == Decimal("99")

  emas = compute_ema_series([b.close for b in bars], period=5)
  assert emas[:4] == [None, None, None, None]
  assert emas[4] == Decimal("103.0000")

  atrs = compute_atr_series(bars, period=5)
  assert atrs[4] == Decimal("3.0000")

  rvol = compute_time_of_day_rvol(30000, [10000, 15000, 20000])
  assert rvol == Decimal("2.0000")

  bars_5m = aggregate_1m_to_nm(bars, 5)
  assert len(bars_5m) == 3
