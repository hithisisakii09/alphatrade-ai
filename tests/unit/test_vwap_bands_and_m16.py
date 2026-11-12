"""Unit and strategy tests for Milestone M16: VWAP Volatility Bands & Breakout Exhaustion Filter."""

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from app.core.clock import ist_datetime
from app.core.config import load_app_config
from app.data.instruments import InstrumentRegistry
from app.data.models import CandleBar
from app.market.indicators import (
    OpeningRangeState,
    check_vwap_breakout_exhaustion,
    compute_session_vwap,
    compute_session_vwap_std,
)
from app.strategy.orb.engine import ORBStrategyEngine


def _make_sample_bars() -> list[CandleBar]:
  bars = []
  prices = [Decimal("1000.00"), Decimal("1010.00"), Decimal("1020.00"), Decimal("1015.00")]
  for i, p in enumerate(prices):
    bars.append(
        CandleBar(
            symbol="RELIANCE",
            interval_minutes=1,
            timestamp_utc=ist_datetime(2026, 11, 12, 9, 15 + i),
            open=p,
            high=p + Decimal("2.0"),
            low=p - Decimal("2.0"),
            close=p,
            volume=1000,
            source="TEST",
        )
    )
  return bars


def test_compute_session_vwap_std_calculation():
  """Verifies volume-weighted standard deviation calculation around VWAP using Decimal math."""
  bars = _make_sample_bars()
  vwap = compute_session_vwap(bars)
  std = compute_session_vwap_std(bars, vwap)

  assert isinstance(std, Decimal)
  assert std > Decimal("0.00")
  # Standard deviation for this dispersion is roughly 6 to 9 points
  assert Decimal("5.00") < std < Decimal("12.00")

  # Empty bars fallback
  assert compute_session_vwap_std([], Decimal("1000.00")) == Decimal("0.00")


def test_check_vwap_breakout_exhaustion_filter():
  """Verifies rejection of overextended breakouts beyond VWAP standard deviation envelope."""
  vwap = Decimal("1000.00")
  std = Decimal("10.00")
  # Upper 2.5σ band = 1000 + (2.5 * 10) = 1025.00

  # Case 1: Healthy breakout within band (1020 <= 1025)
  ok, msg = check_vwap_breakout_exhaustion(Decimal("1020.00"), vwap, std, max_multiplier=Decimal("2.5"))
  assert ok is True
  assert "VWAP_BAND_OK" in msg

  # Case 2: Overextended exhaustion spike (1030 > 1025)
  ok, msg = check_vwap_breakout_exhaustion(Decimal("1030.00"), vwap, std, max_multiplier=Decimal("2.5"))
  assert ok is False
  assert "EXHAUSTION_BAND_EXCEEDED" in msg

  # Case 3: Permissive fallback when std is None
  ok, msg = check_vwap_breakout_exhaustion(Decimal("1030.00"), vwap, None)
  assert ok is True
  assert "VWAP_STD_UNAVAILABLE_PERMISSIVE" in msg


def test_m16_end_to_end_strategy_rejection_of_exhausted_breakout():
  """Verifies that ORBStrategyEngine rejects breakouts exceeding max VWAP standard deviation."""
  cfg = load_app_config()
  cfg.strategy.require_vwap_volatility_cap = True
  cfg.strategy.max_vwap_std_multiplier = Decimal("2.5")
  registry = InstrumentRegistry()
  engine = ORBStrategyEngine(cfg.strategy, registry)

  vwap = Decimal("1000.00")
  vwap_std = Decimal("10.00")  # Upper 2.5σ = 1025.00

  orb = OpeningRangeState(
      symbol="RELIANCE",
      session_date="2026-11-12",
      orb_high=Decimal("1015.00"),
      orb_low=Decimal("990.00"),
      orb_volume=100000,
      is_complete=True,
  )

  # Scenario A: Exhausted breakout bar closing at 1030.00 (> 1025.00)
  bar_exhausted = CandleBar(
      symbol="RELIANCE",
      interval_minutes=5,
      timestamp_utc=ist_datetime(2026, 11, 12, 9, 35),
      open=Decimal("1018.00"),
      high=Decimal("1032.00"),
      low=Decimal("1016.00"),
      close=Decimal("1030.00"),
      volume=50000,
      source="TEST",
  )
  res_exhausted = engine.evaluate_5m_bar(
      bar_5m=bar_exhausted,
      orb_state=orb,
      vwap=vwap,
      rvol=Decimal("2.0"),
      estimated_spread_pct=Decimal("0.0005"),
      prev_daily_close=Decimal("1000.00"),
      nifty_strongly_bearish=False,
      vwap_std=vwap_std,
  )
  assert res_exhausted.triggered is False
  assert res_exhausted.rejection_reason == "VWAP_EXHAUSTION_BAND_EXCEEDED"

  # Scenario B: Healthy breakout bar closing at 1020.00 (<= 1025.00)
  bar_healthy = CandleBar(
      symbol="RELIANCE",
      interval_minutes=5,
      timestamp_utc=ist_datetime(2026, 11, 12, 9, 35),
      open=Decimal("1014.00"),
      high=Decimal("1022.00"),
      low=Decimal("1012.00"),
      close=Decimal("1020.00"),
      volume=50000,
      source="TEST",
  )
  res_healthy = engine.evaluate_5m_bar(
      bar_5m=bar_healthy,
      orb_state=orb,
      vwap=vwap,
      rvol=Decimal("2.0"),
      estimated_spread_pct=Decimal("0.0005"),
      prev_daily_close=Decimal("1000.00"),
      nifty_strongly_bearish=False,
      vwap_std=vwap_std,
  )
  assert res_healthy.triggered is True
  assert res_healthy.signal_id is not None

