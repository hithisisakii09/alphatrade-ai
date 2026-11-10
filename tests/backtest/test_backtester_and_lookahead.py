"""Backtester integrity tests: catches deliberate look-ahead bug and verifies reproducible benchmark."""

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

from app.backtest.engine import ChronologicalBarWindow, LookAheadLeakageError
from app.core.clock import ist_datetime
from app.data.models import CandleBar
from app.services.daily_runner import run_benchmark_simulation


def test_deliberate_lookahead_bug_caught():
  bars = [
      CandleBar(
          symbol="INFY",
          timestamp_utc=ist_datetime(2026, 11, 10, 9, 15 + i),
          open=Decimal("1500"),
          high=Decimal("1505"),
          low=Decimal("1495"),
          close=Decimal("1502"),
          volume=10000,
      )
      for i in range(5)
  ]
  window = ChronologicalBarWindow(bars, current_idx=2)
  # Accessing current or past bar succeeds
  assert window.access_bar_at(2).timestamp_utc == bars[2].timestamp_utc
  # Deliberate look-ahead to future bar (idx=3) raises LookAheadLeakageError
  with _RaisesContext(LookAheadLeakageError, match="Look-ahead violation"):
    window.access_bar_at(3)


def test_reproducible_end_to_end_benchmark_and_api_routes(tmp_path):
  db_file = tmp_path / "test_bt.db"
  res = run_benchmark_simulation(db_url=f"sqlite:///{db_file}")
  summary = res["summary"]
  daily = res["daily_report"]
  assert summary.total_trades == 2
  assert summary.wins == 2
  assert summary.net_expectancy_inr > Decimal("0")
  assert daily["ledger_reconciled"] is True
  assert daily["open_positions_count"] == 0
