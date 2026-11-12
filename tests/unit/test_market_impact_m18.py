"""Unit tests for Milestone M18: Dynamic Market Impact, Square-Root Law & Slippage Analytics."""

from datetime import datetime, timezone
from decimal import Decimal
from fastapi.testclient import TestClient

from app.core.config import CostProfileConfig, RiskProfileConfig, load_app_config
from app.execution.costs import NSECostEngine
from app.execution.impact import (
    ExecutionAnalyticsEngine,
    compute_square_root_market_impact,
)
from app.main import app
from app.risk.engine import RiskEngine


def test_square_root_impact_calculation():
  # BUY 100 shares at 1,000 INR with 40,000 bar volume and 2.0% daily vol
  # Q/V = 100 / 40,000 = 0.0025 -> sqrt(Q/V) = 0.05
  # eta = 0.5 -> temp = 0.5 * 0.02 * 0.05 = 0.0005 (5.0 bps)
  # gamma = 0.25 -> perm = 0.25 * 0.02 * 0.05 = 0.00025 (2.5 bps)
  # total impact = 7.5 bps
  breakdown = compute_square_root_market_impact(
      side="BUY",
      quantity=100,
      raw_price=Decimal("1000.00"),
      reference_volume=40000,
      daily_volatility=Decimal("0.02"),
      eta=Decimal("0.5"),
      gamma=Decimal("0.25"),
      half_spread_pct=Decimal("0.00025"),  # 2.5 bps
      max_participation_pct=Decimal("0.05"),  # 5% max
  )

  assert breakdown.side == "BUY"
  assert breakdown.quantity == 100
  assert breakdown.participation_rate_pct == Decimal("0.2500")  # 0.25%
  assert breakdown.is_participation_compliant is True
  assert breakdown.temporary_impact_pct == Decimal("0.0005")
  assert breakdown.permanent_impact_pct == Decimal("0.0003") or abs(breakdown.permanent_impact_pct - Decimal("0.00025")) < Decimal("0.0001")
  assert breakdown.total_impact_bps == Decimal("7.5000")
  assert breakdown.implementation_shortfall_inr > Decimal("0")


def test_participation_rate_boundary_compliance():
  # Case 1: Compliant (100 shares in 10,000 volume = 1% <= 5%)
  b_ok = compute_square_root_market_impact(
      side="BUY",
      quantity=100,
      raw_price=Decimal("500.00"),
      reference_volume=10000,
      max_participation_pct=Decimal("0.05"),
  )
  assert b_ok.is_participation_compliant is True

  # Case 2: Exceeded (600 shares in 10,000 volume = 6% > 5%)
  b_exceeded = compute_square_root_market_impact(
      side="BUY",
      quantity=600,
      raw_price=Decimal("500.00"),
      reference_volume=10000,
      max_participation_pct=Decimal("0.05"),
  )
  assert b_exceeded.is_participation_compliant is False


def test_nse_cost_engine_dynamic_impact_slippage():
  base_cfg = load_app_config()
  cost_prof = base_cfg.costs["PERCENTAGE_CAPPED_DEFAULT"]

  # Engine with market impact DISABLED
  engine_static = NSECostEngine(cost_prof)
  leg_static = engine_static.compute_leg_costs("BUY", 100, Decimal("1000.00"))
  assert leg_static.slippage_cost_inr == Decimal("25.00")  # 100 * 1000 * 0.00025

  # Engine with market impact ENABLED
  impact_prof = CostProfileConfig(
      brokerage_pct=cost_prof.brokerage_pct,
      brokerage_flat_cap_inr=cost_prof.brokerage_flat_cap_inr,
      stt_buy_pct=cost_prof.stt_buy_pct,
      stt_sell_pct=cost_prof.stt_sell_pct,
      exchange_txn_pct=cost_prof.exchange_txn_pct,
      sebi_turnover_pct=cost_prof.sebi_turnover_pct,
      ipft_pct=cost_prof.ipft_pct,
      stamp_duty_buy_pct=cost_prof.stamp_duty_buy_pct,
      stamp_duty_sell_pct=cost_prof.stamp_duty_sell_pct,
      gst_pct=cost_prof.gst_pct,
      half_spread_pct=cost_prof.half_spread_pct,
      slippage_pct=cost_prof.slippage_pct,
      enable_market_impact=True,
      impact_coefficient_eta=Decimal("0.5"),
      impact_coefficient_gamma=Decimal("0.25"),
  )
  engine_dynamic = NSECostEngine(impact_prof)
  # Q = 100, V = 10,000 -> sqrt(Q/V) = 0.10. sigma = 0.02. slip = 0.75 * 0.02 * 0.10 = 0.0015 (15 bps)
  leg_dynamic = engine_dynamic.compute_leg_costs(
      "BUY", 100, Decimal("1000.00"), bar_volume=10000, daily_volatility=Decimal("0.02")
  )
  assert leg_dynamic.slippage_cost_inr == Decimal("150.00")  # 100,000 * 0.0015 = 150 INR


def test_risk_engine_participation_rate_limit_gate():
  app_cfg = load_app_config()
  cost_eng = NSECostEngine(app_cfg.costs["PERCENTAGE_CAPPED_DEFAULT"])

  # Profile requiring participation rate <= 1.0%
  prof = RiskProfileConfig(
      reference_capital=Decimal("100000.00"),
      max_risk_per_trade_pct=Decimal("0.005"),
      max_daily_loss_pct=Decimal("0.015"),
      max_capital_deployed_pct=Decimal("0.80"),
      max_single_position_pct=Decimal("0.40"),
      max_open_positions=2,
      exit_mode="INTRADAY_V2",
      initial_stop_pct=Decimal("0.0125"),
      min_stop_pct=Decimal("0.0050"),
      max_stop_pct=Decimal("0.0150"),
      trailing_activation_pct=Decimal("0.0250"),
      trailing_drop_pct=Decimal("0.0075"),
      max_cost_to_risk_ratio=Decimal("0.15"),
      require_participation_rate_cap=True,
      max_participation_rate_pct=Decimal("0.01"),  # 1% cap
  )
  risk_eng = RiskEngine(prof, cost_eng)

  # Sizing would yield 16 shares (budget ₹500 / stop dist ₹31.25 = 16 shares)
  # But bar_volume = 1,000 shares -> 1% cap allows max 10 shares!
  decision = risk_eng.evaluate_entry_signal(
      symbol="RELIANCE",
      session_date="2026-11-12",
      signal_timestamp_utc=datetime(2026, 11, 12, 4, 30, tzinfo=timezone.utc),
      candidate_entry_price=Decimal("2500.00"),
      orb_low=Decimal("2468.75"),  # 31.25 distance -> 16 shares
      current_capital_inr=Decimal("100000.00"),
      available_cash_inr=Decimal("50000.00"),
      deployed_exposure_inr=Decimal("0.00"),
      open_positions_Symbols=set(),
      daily_realized_pnl_inr=Decimal("0.00"),
      daily_unrealized_pnl_inr=Decimal("0.00"),
      bar_volume=1000,
  )

  assert decision.approved is False
  assert decision.reason_code == "PARTICIPATION_RATE_LIMIT_EXCEEDED"
  assert "PARTICIPATION_RATE_LIMIT_EXCEEDED" in decision.explanation or "exceeds maximum participation" in decision.explanation


def test_execution_analytics_engine_historical_aggregation():
  engine = ExecutionAnalyticsEngine("alphatrade_v2.db")
  summary = engine.analyze_historical_executions()
  assert summary.total_orders_analyzed >= 0
  assert summary.avg_slippage_bps >= Decimal("0.0")
  assert isinstance(summary.by_symbol, dict)


def test_api_execution_impact_endpoint():
  client = TestClient(app)
  res = client.get("/execution/impact?symbol=RELIANCE&quantity=50&price=1340")
  assert res.status_code == 200
  data = res.json()
  assert "summary" in data
  assert "simulation" in data
  assert data["simulation"]["symbol"] == "RELIANCE"
  assert data["simulation"]["quantity"] == 50
  assert "total_impact_bps" in data["simulation"]
  assert "effective_execution_price" in data["simulation"]

