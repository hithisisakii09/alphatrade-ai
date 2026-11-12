"""Unit tests for Milestone M17: Portfolio Value-at-Risk (VaR), CVaR, and Correlation Engine."""

from datetime import datetime, timezone
from decimal import Decimal
from fastapi.testclient import TestClient

from app.core.config import RiskProfileConfig, load_app_config
from app.execution.costs import NSECostEngine
from app.main import app
from app.risk.engine import RiskEngine
from app.risk.var import (
    PortfolioVaREngine,
    Z_95,
    Z_99,
    compute_cross_asset_covariance,
    compute_returns_from_prices,
    compute_series_statistics,
    cornish_fisher_quantile,
)


def test_returns_and_series_statistics():
  prices = [Decimal("100"), Decimal("102"), Decimal("101"), Decimal("103")]
  returns = compute_returns_from_prices(prices)
  assert len(returns) == 3
  assert returns[0] == Decimal("0.02")  # (102 - 100) / 100
  assert returns[1] == (Decimal("101") - Decimal("102")) / Decimal("102")

  stats = compute_series_statistics(returns)
  assert stats["mean"] > Decimal("0")
  assert stats["variance"] > Decimal("0")
  assert stats["std"] > Decimal("0")
  assert "skewness" in stats
  assert "excess_kurtosis" in stats


def test_cross_asset_covariance_and_correlation():
  # Case 1: Perfectly identical series -> correlation = 1.0
  r1 = [Decimal("0.01"), Decimal("-0.02"), Decimal("0.015"), Decimal("-0.005"), Decimal("0.03")]
  cov, corr = compute_cross_asset_covariance(r1, r1)
  assert corr == Decimal("1.0")

  # Case 2: Perfectly inverse series -> correlation = -1.0
  r2 = [-r for r in r1]
  cov_inv, corr_inv = compute_cross_asset_covariance(r1, r2)
  assert corr_inv == Decimal("-1.0")

  # Case 3: Truncated or insufficient series
  c_empty, r_empty = compute_cross_asset_covariance([], [])
  assert corr_empty == Decimal("0.0") if "corr_empty" in locals() else r_empty == Decimal("0.0")


def test_cornish_fisher_quantile_expansion():
  # When Skewness = 0 and Kurtosis = 0, Cornish-Fisher must equal standard Z
  z_normal = cornish_fisher_quantile(Z_95, Decimal("0.0"), Decimal("0.0"))
  assert abs(z_normal - Z_95) < Decimal("0.00001")

  # When Skewness < 0 (left skew) and Kurtosis > 0 (fat tail), Z_cf should be higher than Z
  z_fat = cornish_fisher_quantile(Z_95, Decimal("-0.5"), Decimal("2.0"))
  assert z_fat > Z_95


def test_portfolio_var_cvar_diversification_benefit():
  # Setup synthetic 2-asset returns
  # Stock A (Tech): high vol
  # Stock B (Defensive): lower vol, moderate positive correlation
  r_a = [
      Decimal("0.02"), Decimal("-0.01"), Decimal("0.03"), Decimal("-0.02"),
      Decimal("0.015"), Decimal("-0.005"), Decimal("0.025"), Decimal("-0.015"),
      Decimal("0.01"), Decimal("-0.02"), Decimal("0.018"), Decimal("-0.012"),
  ]
  r_b = [
      Decimal("0.005"), Decimal("0.002"), Decimal("0.01"), Decimal("-0.008"),
      Decimal("0.004"), Decimal("-0.001"), Decimal("0.008"), Decimal("-0.005"),
      Decimal("0.003"), Decimal("-0.006"), Decimal("0.005"), Decimal("-0.004"),
  ]

  engine = PortfolioVaREngine(":memory:")
  holdings = {"TECH_STOCK": Decimal("50000.00"), "DEF_STOCK": Decimal("50000.00")}
  equity = Decimal("100000.00")

  report = engine.compute_portfolio_risk(
      holdings=holdings,
      total_equity_inr=equity,
      custom_returns={"TECH_STOCK": r_a, "DEF_STOCK": r_b},
  )

  m = report.metrics
  assert m.holdings_count == 2
  assert m.var_95_inr > Decimal("0")
  assert m.var_99_inr > m.var_95_inr  # 99% VaR must strictly exceed 95% VaR
  assert m.cvar_95_inr > m.var_95_inr  # Expected Shortfall must strictly exceed VaR
  assert m.cvar_99_inr > m.cvar_95_inr  # 99% CVaR must exceed 95% CVaR
  assert m.diversification_benefit_pct > Decimal("0")  # Diversification benefit must be positive
  assert report.correlation_matrix["TECH_STOCK"]["TECH_STOCK"] == Decimal("1.0")


def test_risk_engine_portfolio_var_gate():
  app_cfg = load_app_config()
  cost_engine = NSECostEngine(app_cfg.costs["PERCENTAGE_CAPPED_DEFAULT"])

  # Configure profile with tight VaR cap (0.2% max portfolio VaR)
  profile = RiskProfileConfig(
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
      require_portfolio_var_cap=True,
      max_portfolio_var_95_pct=Decimal("0.002"),  # 0.20% = ₹200 cap
  )

  var_engine = PortfolioVaREngine(":memory:")
  # Mock returns with realistic intraday dispersion
  mock_returns = {
      "RELIANCE": [Decimal("0.002"), Decimal("-0.002")] * 25,
      "TCS": [Decimal("0.0015"), Decimal("-0.0015")] * 25,
  }
  # Monkey-patch get_aligned_minute_returns
  var_engine.get_aligned_minute_returns = lambda syms, lookback=1500: mock_returns

  risk = RiskEngine(
      profile=profile,
      cost_engine=cost_engine,
      entry_cutoff_ist="14:30",
      liquidation_start_ist="15:00",
      liquidation_deadline_ist="15:15",
      var_engine=var_engine,
  )

  # Evaluate entry for TCS when RELIANCE is already open
  decision = risk.evaluate_entry_signal(
      symbol="TCS",
      session_date="2026-11-12",
      signal_timestamp_utc=datetime(2026, 11, 12, 4, 30, tzinfo=timezone.utc),
      candidate_entry_price=Decimal("2500.00"),
      orb_low=Decimal("2475.00"),
      current_capital_inr=Decimal("100000.00"),
      available_cash_inr=Decimal("50000.00"),
      deployed_exposure_inr=Decimal("40000.00"),
      open_positions_Symbols={"RELIANCE"},
      daily_realized_pnl_inr=Decimal("0.00"),
      daily_unrealized_pnl_inr=Decimal("0.00"),
  )

  # With 0.2% VaR cap, portfolio VaR of ~2.4% must reject
  assert decision.approved is False
  assert decision.reason_code == "PORTFOLIO_VAR_LIMIT_EXCEEDED"
  assert "PORTFOLIO_VAR_LIMIT_EXCEEDED" in decision.explanation or "exceeds limit" in decision.explanation


def test_var_api_endpoint():
  client = TestClient(app)
  res = client.get("/risk/var?symbols=RELIANCE,TCS&lookback_bars=200")
  assert res.status_code == 200
  data = res.json()
  assert "metrics" in data
  assert "var_95_inr" in data["metrics"]
  assert "cvar_95_inr" in data["metrics"]
  assert "diversification_benefit_pct" in data["metrics"]
  assert "correlation_matrix" in data
  assert "RELIANCE" in data["correlation_matrix"]
  assert "TCS" in data["correlation_matrix"]
