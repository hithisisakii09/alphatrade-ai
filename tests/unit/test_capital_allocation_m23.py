"""Unit tests for Milestone M23: Dynamic Portfolio Capital Allocation & Intraday Margin Stress."""

from datetime import datetime, timezone
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import RiskProfileConfig, load_app_config
from app.core.money import round_paise, round_price, to_decimal
from app.execution.costs import NSECostEngine
from app.risk.allocation import (
    RiskParityAllocator,
    compute_inverse_volatility_weights,
    compute_equal_risk_contribution_weights,
    compute_risk_contributions,
    compute_diversification_ratio,
    enforce_weight_caps,
)
from app.risk.engine import RiskEngine
from app.risk.margin_stress import (
    IntradayMarginStressEngine,
    IntradayMarginStressReport,
)


def _sample_covariance_matrix() -> dict[str, dict[str, Decimal]]:
  return {
      "RELIANCE": {
          "RELIANCE": Decimal("0.000400"),
          "TCS": Decimal("0.000100"),
          "INFY": Decimal("0.000050"),
      },
      "TCS": {
          "RELIANCE": Decimal("0.000100"),
          "TCS": Decimal("0.000900"),
          "INFY": Decimal("0.000200"),
      },
      "INFY": {
          "RELIANCE": Decimal("0.000050"),
          "TCS": Decimal("0.000200"),
          "INFY": Decimal("0.001600"),
      },
  }


def _sample_volatilities() -> dict[str, Decimal]:
  cov = _sample_covariance_matrix()
  return {s: cov[s][s].sqrt() for s in cov}


def test_inverse_volatility_weights_calculation():
  """Verifies naive risk parity weights are inversely proportional to volatility."""
  vols = _sample_volatilities()
  w = compute_inverse_volatility_weights(vols, max_weight=Decimal("1.0"))
  assert len(w) == 3
  # RELIANCE vol = 0.02, TCS vol = 0.03, INFY vol = 0.04
  # inv_vol: 50, 33.33, 25. Total = 108.33
  # RELIANCE weight should be highest, INFY lowest
  assert w["RELIANCE"] > w["TCS"] > w["INFY"]
  assert abs(sum(w.values()) - Decimal("1.0000")) < Decimal("0.0001")


def test_equal_risk_contribution_convergence():
  """Verifies ERC weights converge such that every asset has equal percentage risk contribution."""
  cov = _sample_covariance_matrix()
  vols = _sample_volatilities()
  w = compute_equal_risk_contribution_weights(
      cov_matrix=cov,
      volatilities=vols,
      max_weight=Decimal("1.0"),
      max_iterations=50,
      tolerance=Decimal("0.0001"),
  )
  vol_p, mrc, rc, prc = compute_risk_contributions(w, cov)
  assert vol_p > Decimal("0")
  target_prc = Decimal("1") / Decimal("3")
  for s in ["RELIANCE", "TCS", "INFY"]:
    assert abs(prc[s] - target_prc) < Decimal("0.001")
  assert abs(sum(w.values()) - Decimal("1.0000")) < Decimal("0.0001")


def test_weight_capping_and_redistribution():
  """Verifies enforce_weight_caps clamps single-name weights and redistributes residual."""
  raw = {
      "RELIANCE": Decimal("0.60"),
      "TCS": Decimal("0.25"),
      "INFY": Decimal("0.15"),
  }
  capped = enforce_weight_caps(raw, max_weight=Decimal("0.40"))
  assert capped["RELIANCE"] <= Decimal("0.4001")
  assert capped["TCS"] > Decimal("0.25")
  assert capped["INFY"] > Decimal("0.15")
  assert abs(sum(capped.values()) - Decimal("1.0000")) < Decimal("0.0001")


def test_diversification_ratio_properties():
  """Verifies Diversification Ratio DR >= 1.0000 and DR_ERC >= DR_Equal."""
  cov = _sample_covariance_matrix()
  vols = _sample_volatilities()
  w_eq = {"RELIANCE": Decimal("1") / Decimal("3"), "TCS": Decimal("1") / Decimal("3"), "INFY": Decimal("1") / Decimal("3")}
  vol_eq, _, _, _ = compute_risk_contributions(w_eq, cov)
  dr_eq = compute_diversification_ratio(w_eq, vols, vol_eq)

  w_erc = compute_equal_risk_contribution_weights(cov, vols, max_weight=Decimal("1.0"))
  vol_erc, _, _, _ = compute_risk_contributions(w_erc, cov)
  dr_erc = compute_diversification_ratio(w_erc, vols, vol_erc)

  assert dr_eq >= Decimal("1.0000")
  assert dr_erc >= dr_eq


def test_pre_trade_margin_stress_scenarios():
  """Verifies exact deterministic calculation of base, gap, vol-spike, and composite margin stress."""
  engine = IntradayMarginStressEngine(
      base_margin_rate=Decimal("0.20"),
      max_stressed_utilization_pct=Decimal("0.85"),
      gap_shock_pct=Decimal("0.025"),
      vol_expansion_multiplier=Decimal("1.50"),
      liquidity_haircut_pct=Decimal("0.0020"),
  )
  open_pos = {
      "RELIANCE": (10, Decimal("2500.00")),  # Notional: 25,000
      "TCS": (5, Decimal("3000.00")),        # Notional: 15,000
  }
  # Total notional: 40,000
  # Base margin: 40,000 * 0.20 = 8,000
  # Gap loss (-2.5%): 40,000 * 0.025 = 1,000
  # Post-gap notional: 39,000
  # Haircut (0.20%): 40,000 * 0.0020 = 80
  # Stressed cash: 100,000 - 1,000 - 80 = 98,920
  # Stressed margin (+50% vol spike = 30%): 39,000 * 0.30 = 11,700
  # Composite utilization: 11,700 / 98,920 = 11.83%
  rep = engine.evaluate_stress(
      open_positions=open_pos,
      total_equity_inr=Decimal("100000.00"),
      available_cash_inr=Decimal("100000.00"),
  )
  assert rep.base_margin_required_inr == Decimal("8000.00")
  assert rep.composite_stressed_margin_inr == Decimal("11700.00")
  assert rep.composite_stressed_cash_inr == Decimal("98920.00")
  assert rep.passed is True
  assert len(rep.scenarios) == 5


def test_risk_engine_pre_trade_margin_stress_gate_rejection():
  """Verifies RiskEngine rejects orders exceeding stressed margin tolerance with INTRADAY_MARGIN_STRESS_EXCEEDED."""
  profile = RiskProfileConfig(
      reference_capital=Decimal("100000.00"),
      max_risk_per_trade_pct=Decimal("0.01"),
      max_daily_loss_pct=Decimal("0.03"),
      max_capital_deployed_pct=Decimal("0.80"),
      max_single_position_pct=Decimal("0.40"),
      max_open_positions=3,
      exit_mode="INTRADAY_V2",
      initial_stop_pct=Decimal("0.0125"),
      min_stop_pct=Decimal("0.005"),
      max_stop_pct=Decimal("0.015"),
      trailing_activation_pct=Decimal("0.025"),
      trailing_drop_pct=Decimal("0.0075"),
      max_cost_to_risk_ratio=Decimal("0.15"),
      require_margin_stress_gate=True,
      max_stressed_margin_utilization_pct=Decimal("0.10"),  # Very tight 10% limit to trigger breach
  )
  cfg = load_app_config()
  cost_engine = NSECostEngine(profile=cfg.costs[cfg.active_cost_profile])
  risk_engine = RiskEngine(profile=profile, cost_engine=cost_engine)

  decision = risk_engine.evaluate_entry_signal(
      symbol="RELIANCE",
      session_date="2026-11-14",
      signal_timestamp_utc=datetime(2026, 11, 14, 4, 0, tzinfo=timezone.utc),
      candidate_entry_price=Decimal("2500.00"),
      orb_low=Decimal("2475.00"),
      current_capital_inr=Decimal("100000.00"),
      available_cash_inr=Decimal("100000.00"),
      deployed_exposure_inr=Decimal("0.00"),
      open_positions_Symbols=set(),
      daily_realized_pnl_inr=Decimal("0.00"),
      daily_unrealized_pnl_inr=Decimal("0.00"),
  )
  assert decision.approved is False
  assert decision.reason_code == "INTRADAY_MARGIN_STRESS_EXCEEDED"
  assert "Pre-trade margin stress test failed" in decision.explanation


def test_risk_engine_pre_trade_margin_stress_gate_approval():
  """Verifies RiskEngine approves prospective order when stressed margin utilization <= 85%."""
  profile = RiskProfileConfig(
      reference_capital=Decimal("100000.00"),
      max_risk_per_trade_pct=Decimal("0.01"),
      max_daily_loss_pct=Decimal("0.03"),
      max_capital_deployed_pct=Decimal("0.80"),
      max_single_position_pct=Decimal("0.40"),
      max_open_positions=3,
      exit_mode="INTRADAY_V2",
      initial_stop_pct=Decimal("0.0125"),
      min_stop_pct=Decimal("0.005"),
      max_stop_pct=Decimal("0.015"),
      trailing_activation_pct=Decimal("0.025"),
      trailing_drop_pct=Decimal("0.0075"),
      max_cost_to_risk_ratio=Decimal("0.15"),
      require_margin_stress_gate=True,
      max_stressed_margin_utilization_pct=Decimal("0.85"),  # Standard 85% limit
  )
  cfg = load_app_config()
  cost_engine = NSECostEngine(profile=cfg.costs[cfg.active_cost_profile])
  risk_engine = RiskEngine(profile=profile, cost_engine=cost_engine)

  decision = risk_engine.evaluate_entry_signal(
      symbol="RELIANCE",
      session_date="2026-11-14",
      signal_timestamp_utc=datetime(2026, 11, 14, 4, 0, tzinfo=timezone.utc),
      candidate_entry_price=Decimal("2500.00"),
      orb_low=Decimal("2475.00"),
      current_capital_inr=Decimal("100000.00"),
      available_cash_inr=Decimal("100000.00"),
      deployed_exposure_inr=Decimal("0.00"),
      open_positions_Symbols=set(),
      daily_realized_pnl_inr=Decimal("0.00"),
      daily_unrealized_pnl_inr=Decimal("0.00"),
  )
  assert decision.approved is True
  assert decision.approved_quantity > 0


def test_m23_api_endpoints_integration():
  """Verifies GET /risk/allocation and GET /risk/margin-stress return valid HTTP 200 responses."""
  client = TestClient(app)

  # Test allocation endpoint
  res_alloc = client.get("/risk/allocation?symbols=RELIANCE,TCS&method=EQUAL_RISK_CONTRIBUTION")
  assert res_alloc.status_code == 200
  data_alloc = res_alloc.json()
  assert "portfolio_volatility_daily_pct" in data_alloc
  assert "diversification_ratio" in data_alloc
  assert len(data_alloc["allocations"]) == 2

  # Test margin stress endpoint
  res_stress = client.get("/risk/margin-stress?symbols=RELIANCE,TCS")
  assert res_stress.status_code == 200
  data_stress = res_stress.json()
  assert "composite_stressed_margin_inr" in data_stress
  assert "composite_utilization_pct" in data_stress
  assert "scenarios" in data_stress
  assert len(data_stress["scenarios"]) == 5
