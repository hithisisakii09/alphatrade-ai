"""Unit and integration tests for Milestone M13: Multi-Timeframe Trend Confirmation, 2-Day High Breakout, and Adaptive ATR Trailing Stops."""

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from app.core.clock import ist_datetime
from app.core.config import load_app_config
from app.data.instruments import InstrumentRegistry
from app.data.models import CandleBar
from app.execution.costs import NSECostEngine
from app.market.indicators import (
    check_macro_trend_alignment,
    check_two_day_high_breakout,
    compute_higher_timeframe_ema,
    OpeningRangeState,
)
from app.risk.engine import RiskEngine
from app.strategy.orb.engine import ORBStrategyEngine


def _make_1m_bars(start_hour: int, start_min: int, count: int, base_price: Decimal, price_step: Decimal = Decimal("0")) -> list[CandleBar]:
  bars = []
  for i in range(count):
    m = start_min + i
    h = start_hour + m // 60
    m = m % 60
    p = base_price + Decimal(i) * price_step
    bars.append(
        CandleBar(
            symbol="RELIANCE",
            interval_minutes=1,
            timestamp_utc=ist_datetime(2026, 11, 12, h, m),
            open=p,
            high=p + Decimal("1.0"),
            low=p - Decimal("1.0"),
            close=p,
            volume=5000,
            source="TEST",
        )
    )
  return bars


def test_higher_timeframe_ema_calculation():
  """Verifies 1-minute aggregation to 1-hour bars and EMA-3/EMA-5 calculation."""
  # 180 1-minute bars = 3 60-minute bars
  bars = _make_1m_bars(9, 15, 180, Decimal("1000.00"), price_step=Decimal("0.5"))
  htf_ema = compute_higher_timeframe_ema(bars, htf_minutes=60, period=3)
  assert htf_ema is not None
  assert isinstance(htf_ema, Decimal)
  # Closes are increasing, so EMA must be between min and max close
  assert Decimal("1000.00") < htf_ema < Decimal("1100.00")

  # Insufficient bars (< 3) should return None
  short_bars = _make_1m_bars(9, 15, 60, Decimal("1000.00"))
  assert compute_higher_timeframe_ema(short_bars, htf_minutes=60, period=3) is None


def test_macro_trend_alignment_filter():
  """Verifies that LONG breakouts require Price >= 1h EMA when macro alignment is active."""
  curr_price = Decimal("1350.00")
  ema_bullish = Decimal("1340.00")
  ema_bearish = Decimal("1360.00")

  # Bullish: Price (1350) >= 1h EMA (1340) -> Approved
  aligned, msg = check_macro_trend_alignment(curr_price, ema_bullish, direction="LONG")
  assert aligned is True
  assert "MACRO_UPTREND_ALIGNED" in msg

  # Bearish: Price (1350) < 1h EMA (1360) -> Filtered
  aligned, msg = check_macro_trend_alignment(curr_price, ema_bearish, direction="LONG")
  assert aligned is False
  assert "COUNTER_TREND_FILTERED" in msg

  # Permissive fallback when HTF EMA unavailable
  aligned, msg = check_macro_trend_alignment(curr_price, None, direction="LONG")
  assert aligned is True
  assert "HTF_EMA_UNAVAILABLE_PERMISSIVE" in msg


def test_two_day_high_breakout_filter():
  """Verifies that breakouts confirm against previous day's high."""
  curr_price = Decimal("2500.00")
  prev_high_lower = Decimal("2480.00")
  prev_high_higher = Decimal("2520.00")

  # Breakout above 2-day high
  ok, msg = check_two_day_high_breakout(curr_price, prev_high_lower)
  assert ok is True
  assert "TWO_DAY_HIGH_CONFIRMED" in msg

  # Below 2-day high -> Rejected
  ok, msg = check_two_day_high_breakout(curr_price, prev_high_higher)
  assert ok is False
  assert "TWO_DAY_HIGH_REJECTED" in msg

  # Permissive when prev day high unavailable
  ok, msg = check_two_day_high_breakout(curr_price, None)
  assert ok is True
  assert "PREV_DAY_HIGH_UNAVAILABLE_PERMISSIVE" in msg


def test_adaptive_atr_trailing_stop_activation():
  """Verifies that adaptive ATR trailing dynamically scales trailing activation based on volatility."""
  cfg = load_app_config()
  profile = cfg.strategy.risk_profiles[cfg.active_risk_profile]
  cost_engine = NSECostEngine(cfg.costs[cfg.active_cost_profile])
  risk_eng = RiskEngine(profile, cost_engine)

  entry_price = Decimal("1000.00")
  orb_low = Decimal("990.00")
  atr_val = Decimal("15.00")  # High volatility stock

  # Default: Fixed percentage (+2.50% = 1025.00)
  dec_default = risk_eng.evaluate_entry_signal(
      symbol="RELIANCE",
      session_date="2026-11-12",
      signal_timestamp_utc=ist_datetime(2026, 11, 12, 10, 0),
      candidate_entry_price=entry_price,
      orb_low=orb_low,
      current_capital_inr=Decimal("100000.00"),
      available_cash_inr=Decimal("100000.00"),
      deployed_exposure_inr=Decimal("0.00"),
      open_positions_Symbols=set(),
      daily_realized_pnl_inr=Decimal("0.00"),
      daily_unrealized_pnl_inr=Decimal("0.00"),
      atr_value=atr_val,
  )
  assert dec_default.trailing_activation_price == Decimal("1025.00")

  # Enable adaptive ATR trailing (2.0x ATR = +30.00 -> 1030.00)
  profile.adaptive_atr_trailing = True
  profile.adaptive_atr_trail_multiplier = Decimal("2.0")

  dec_adaptive = risk_eng.evaluate_entry_signal(
      symbol="RELIANCE",
      session_date="2026-11-12",
      signal_timestamp_utc=ist_datetime(2026, 11, 12, 10, 0),
      candidate_entry_price=entry_price,
      orb_low=orb_low,
      current_capital_inr=Decimal("100000.00"),
      available_cash_inr=Decimal("100000.00"),
      deployed_exposure_inr=Decimal("0.00"),
      open_positions_Symbols=set(),
      daily_realized_pnl_inr=Decimal("0.00"),
      daily_unrealized_pnl_inr=Decimal("0.00"),
      atr_value=atr_val,
  )
  assert dec_adaptive.trailing_activation_price == Decimal("1030.00")


def test_m13_end_to_end_strategy_rejection_of_counter_trend():
  """Verifies that ORBStrategyEngine rejects 5m breakout when price is below 1-hour macro EMA."""
  cfg = load_app_config()
  cfg.strategy.require_macro_trend_alignment = True
  registry = InstrumentRegistry()
  engine = ORBStrategyEngine(cfg.strategy, registry)

  bar_5m = CandleBar(
      symbol="RELIANCE",
      interval_minutes=5,
      timestamp_utc=ist_datetime(2026, 11, 12, 9, 35),
      open=Decimal("1250.00"),
      high=Decimal("1260.00"),
      low=Decimal("1248.00"),
      close=Decimal("1258.00"),
      volume=50000,
      source="TEST",
  )
  orb = OpeningRangeState(
      symbol="RELIANCE",
      session_date="2026-11-12",
      orb_high=Decimal("1255.00"),
      orb_low=Decimal("1240.00"),
      orb_volume=100000,
      is_complete=True,
  )

  # Scenario A: Counter-trend (Price 1258 < 1h EMA 1270) -> Rejected
  res_counter = engine.evaluate_5m_bar(
      bar_5m=bar_5m,
      orb_state=orb,
      vwap=Decimal("1252.00"),
      rvol=Decimal("2.0"),
      estimated_spread_pct=Decimal("0.0005"),
      prev_daily_close=Decimal("1250.00"),
      nifty_strongly_bearish=False,
      htf_ema=Decimal("1270.00"),
  )
  assert res_counter.triggered is False
  assert res_counter.rejection_reason == "COUNTER_MACRO_TREND"

  # Scenario B: Trend-aligned (Price 1258 >= 1h EMA 1250) -> Approved
  res_aligned = engine.evaluate_5m_bar(
      bar_5m=bar_5m,
      orb_state=orb,
      vwap=Decimal("1252.00"),
      rvol=Decimal("2.0"),
      estimated_spread_pct=Decimal("0.0005"),
      prev_daily_close=Decimal("1250.00"),
      nifty_strongly_bearish=False,
      htf_ema=Decimal("1250.00"),
  )
  assert res_aligned.triggered is True
  assert res_aligned.signal_id is not None
