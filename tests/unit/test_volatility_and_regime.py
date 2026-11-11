"""Unit tests for Milestone M11: Volatility Adaptability, RVOL Filtering & Sector Concentration."""

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from app.core.config import CostProfileConfig, RiskProfileConfig, StrategyConfigModel
from app.data.instruments import InstrumentRegistry
from app.data.models import CandleBar
from app.execution.costs import NSECostEngine
from app.market.indicators import OpeningRangeState
from app.risk.engine import RiskEngine
from app.strategy.orb.engine import ORBStrategyEngine


def _get_registry() -> InstrumentRegistry:
  return InstrumentRegistry()


def _get_risk_profile() -> RiskProfileConfig:
  return RiskProfileConfig(
      reference_capital=Decimal("100000.00"),
      max_risk_per_trade_pct=Decimal("0.005"),  # 0.5% = ₹500
      max_daily_loss_pct=Decimal("0.015"),
      max_capital_deployed_pct=Decimal("0.80"),
      max_single_position_pct=Decimal("0.40"),
      max_open_positions=3,
      exit_mode="INTRADAY_V2",
      initial_stop_pct=Decimal("0.0125"),
      min_stop_pct=Decimal("0.0050"),
      max_stop_pct=Decimal("0.0150"),
      trailing_activation_pct=Decimal("0.0250"),
      trailing_drop_pct=Decimal("0.0075"),
      max_cost_to_risk_ratio=Decimal("0.15"),
      max_positions_per_sector=1,
      atr_volatility_multiplier=Decimal("1.5"),
  )


def _get_cost_engine() -> NSECostEngine:
  cost_cfg = CostProfileConfig(
      brokerage_pct=Decimal("0.0003"),
      brokerage_flat_cap_inr=Decimal("20.00"),
      stt_buy_pct=Decimal("0.0000"),
      stt_sell_pct=Decimal("0.00025"),
      exchange_txn_pct=Decimal("0.0000325"),
      sebi_turnover_pct=Decimal("0.000001"),
      ipft_pct=Decimal("0.000001"),
      stamp_duty_buy_pct=Decimal("0.00003"),
      stamp_duty_sell_pct=Decimal("0.0000"),
      gst_pct=Decimal("0.18"),
      half_spread_pct=Decimal("0.0005"),
      slippage_pct=Decimal("0.00025"),
  )
  return NSECostEngine(cost_cfg)


@pytest.fixture
def mock_registry():
  return _get_registry()


@pytest.fixture
def risk_profile():
  return _get_risk_profile()


@pytest.fixture
def cost_engine():
  return _get_cost_engine()


def test_sector_concentration_guard(mock_registry=None, risk_profile=None, cost_engine=None):
  """Verifies that candidate entry is rejected if an open position already exists in the same sector."""
  reg = mock_registry or _get_registry()
  prof = risk_profile or _get_risk_profile()
  cost = cost_engine or _get_cost_engine()

  risk_eng = RiskEngine(prof, cost, registry=reg)
  ts = datetime(2026, 11, 10, 4, 30, tzinfo=timezone.utc)  # 10:00 IST

  # Open position already exists in HDFCBANK (Sector: BANKING)
  open_syms = {"HDFCBANK"}

  # Candidate signal in ICICIBANK (Sector: BANKING) -> Must be rejected
  dec_icici = risk_eng.evaluate_entry_signal(
      symbol="ICICIBANK",
      session_date="2026-11-10",
      signal_timestamp_utc=ts,
      candidate_entry_price=Decimal("1100.00"),
      orb_low=Decimal("1090.00"),
      current_capital_inr=Decimal("100000.00"),
      available_cash_inr=Decimal("80000.00"),
      deployed_exposure_inr=Decimal("20000.00"),
      open_positions_Symbols=open_syms,
      daily_realized_pnl_inr=Decimal("0.00"),
      daily_unrealized_pnl_inr=Decimal("0.00"),
  )
  assert dec_icici.approved is False
  assert dec_icici.reason_code == "SECTOR_CONCENTRATION_LIMIT_EXCEEDED"
  assert "BANKING" in dec_icici.explanation

  # Candidate signal in INFY (Sector: IT) -> Must be approved (different sector)
  dec_infy = risk_eng.evaluate_entry_signal(
      symbol="INFY",
      session_date="2026-11-10",
      signal_timestamp_utc=ts,
      candidate_entry_price=Decimal("1500.00"),
      orb_low=Decimal("1485.00"),
      current_capital_inr=Decimal("100000.00"),
      available_cash_inr=Decimal("80000.00"),
      deployed_exposure_inr=Decimal("20000.00"),
      open_positions_Symbols=open_syms,
      daily_realized_pnl_inr=Decimal("0.00"),
      daily_unrealized_pnl_inr=Decimal("0.00"),
  )
  assert dec_infy.approved is True
  assert dec_infy.approved_quantity >= 1


def test_atr_adjusted_volatility_sizing(mock_registry=None, risk_profile=None, cost_engine=None):
  """Verifies that high ATR dynamically widens the effective stop distance and scales down position size."""
  reg = mock_registry or _get_registry()
  prof = risk_profile or _get_risk_profile()
  cost = cost_engine or _get_cost_engine()

  risk_eng = RiskEngine(prof, cost, registry=reg)
  ts = datetime(2026, 11, 10, 4, 30, tzinfo=timezone.utc)
  entry_p = Decimal("1000.00")

  # 1. Normal sizing without ATR (standard 1.25% stop = ₹12.50 distance)
  dec_normal = risk_eng.evaluate_entry_signal(
      symbol="RELIANCE",
      session_date="2026-11-10",
      signal_timestamp_utc=ts,
      candidate_entry_price=entry_p,
      orb_low=Decimal("990.00"),  # 1.0% distance, clamped to 1.0%
      current_capital_inr=Decimal("100000.00"),
      available_cash_inr=Decimal("100000.00"),
      deployed_exposure_inr=Decimal("0.00"),
      open_positions_Symbols=set(),
      daily_realized_pnl_inr=Decimal("0.00"),
      daily_unrealized_pnl_inr=Decimal("0.00"),
      atr_value=None,
  )
  assert dec_normal.approved is True
  qty_normal = dec_normal.approved_quantity

  # 2. Sizing under high market volatility (ATR = 15.00 -> 1.5 * 15 = ₹22.50 distance)
  dec_volatile = risk_eng.evaluate_entry_signal(
      symbol="RELIANCE",
      session_date="2026-11-10",
      signal_timestamp_utc=ts,
      candidate_entry_price=entry_p,
      orb_low=Decimal("990.00"),
      current_capital_inr=Decimal("100000.00"),
      available_cash_inr=Decimal("100000.00"),
      deployed_exposure_inr=Decimal("0.00"),
      open_positions_Symbols=set(),
      daily_realized_pnl_inr=Decimal("0.00"),
      daily_unrealized_pnl_inr=Decimal("0.00"),
      atr_value=Decimal("15.00"),
  )
  assert dec_volatile.approved is True
  qty_volatile = dec_volatile.approved_quantity

  # Volatile regime must result in strictly smaller position size to preserve the ₹500 risk limit
  assert qty_volatile < qty_normal
  assert qty_volatile == int(Decimal("500.00") / Decimal("22.50"))


def test_rvol_threshold_filtering(mock_registry=None, risk_profile=None):
  """Verifies that evaluate_5m_bar rejects breakouts with insufficient Relative Volume (< 1.50x)."""
  reg = mock_registry or _get_registry()
  prof = risk_profile or _get_risk_profile()

  strat_cfg = StrategyConfigModel(
      strategy_id="ORB_INTRADAY_V2",
      version="2.0.0",
      direction="LONG_ONLY",
      base_interval_minutes=1,
      signal_interval_minutes=5,
      opening_range_minutes=15,
      min_rvol_threshold=Decimal("1.50"),
      min_daily_turnover_cr=Decimal("25.0"),
      max_spread_pct=Decimal("0.0015"),
      min_upper_circuit_headroom_pct=Decimal("0.0300"),
      signal_expiry_bars_5m=2,
      entry_cutoff_ist="14:30",
      liquidation_start_ist="15:00",
      liquidation_deadline_ist="15:15",
      risk_profiles={"STANDARD_RESEARCH_100K": prof},
  )
  strat_eng = ORBStrategyEngine(strat_cfg, reg)

  orb_state = OpeningRangeState(
      symbol="RELIANCE",
      session_date="2026-11-10",
      orb_high=Decimal("1250.00"),
      orb_low=Decimal("1240.00"),
      orb_volume=25000,
      is_complete=True,
  )
  ts = datetime(2026, 11, 10, 4, 15, tzinfo=timezone.utc)  # 09:45 IST
  bar_5m = CandleBar(
      symbol="RELIANCE",
      interval_minutes=5,
      timestamp_utc=ts,
      open=Decimal("1251.00"),
      high=Decimal("1256.00"),
      low=Decimal("1250.50"),
      close=Decimal("1255.00"),  # > ORH 1250.00
      volume=8000,
  )

  # 1. Breakout with weak volume (RVOL = 1.10x < 1.50x) -> Must be rejected
  sig_weak = strat_eng.evaluate_5m_bar(
      bar_5m=bar_5m,
      orb_state=orb_state,
      vwap=Decimal("1248.00"),
      rvol=Decimal("1.10"),
      estimated_spread_pct=Decimal("0.0005"),
      prev_daily_close=Decimal("1245.00"),
  )
  assert sig_weak.triggered is False
  assert sig_weak.rejection_reason == "INSUFFICIENT_RVOL"

  # 2. Breakout with strong institutional volume (RVOL = 1.85x >= 1.50x) -> Must trigger
  sig_strong = strat_eng.evaluate_5m_bar(
      bar_5m=bar_5m,
      orb_state=orb_state,
      vwap=Decimal("1248.00"),
      rvol=Decimal("1.85"),
      estimated_spread_pct=Decimal("0.0005"),
      prev_daily_close=Decimal("1245.00"),
  )
  assert sig_strong.triggered is True
  assert sig_strong.signal_id is not None
  assert sig_strong.rejection_reason is None
