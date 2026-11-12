"""Unit and integration tests for Milestone M15: Non-Parametric Monte Carlo Bootstrap Resampling & Risk of Ruin Simulator."""

from decimal import Decimal
import pytest
from fastapi.testclient import TestClient

from app.api.routes import router
from app.research.monte_carlo import (
    MonteCarloSimulationResult,
    run_monte_carlo_resampling,
)


def test_monte_carlo_empty_trade_pool():
  """Verifies that an empty trade pool safely returns zero ruin and unchanged equity."""
  res = run_monte_carlo_resampling(
      trade_pnls=[],
      starting_capital_inr=Decimal("100000.00"),
      iterations=100,
  )
  assert res.sample_trade_count == 0
  assert res.ruin_probability_pct == Decimal("0.00")
  assert res.median_ending_equity_inr == Decimal("100000.00")
  assert res.sample_paths == {}


def test_monte_carlo_deterministic_seed():
  """Verifies that identical random seeds produce byte-identical reproducible simulation metrics."""
  pnls = [Decimal("500.00"), Decimal("-250.00"), Decimal("1000.00"), Decimal("-500.00")] * 10
  res1 = run_monte_carlo_resampling(trade_pnls=pnls, iterations=500, seed=12345)
  res2 = run_monte_carlo_resampling(trade_pnls=pnls, iterations=500, seed=12345)

  assert res1.ruin_probability_pct == res2.ruin_probability_pct
  assert res1.median_max_drawdown_pct == res2.median_max_drawdown_pct
  assert res1.median_ending_equity_inr == res2.median_ending_equity_inr
  assert res1.sample_paths["median"] == res2.sample_paths["median"]


def test_monte_carlo_loss_pool_triggers_ruin():
  """Verifies that a pool consisting strictly of substantial losses correctly registers high ruin probability."""
  # 50 consecutive losses of ₹500 each = ₹25,000 loss on ₹100,000 (25% drawdown)
  loss_pnls = [Decimal("-500.00")] * 50
  res = run_monte_carlo_resampling(
      trade_pnls=loss_pnls,
      starting_capital_inr=Decimal("100000.00"),
      iterations=200,
      ruin_drawdown_pct=Decimal("0.10"),  # 10% threshold
      seed=42,
  )
  assert res.ruin_probability_pct == Decimal("100.0000")
  assert res.drawdown_10pct_prob == Decimal("100.0000")
  assert res.median_ending_equity_inr == Decimal("75000.00")


def test_monte_carlo_api_endpoint():
  """Verifies that GET /research/monte-carlo endpoint returns valid schema and metrics."""
  from fastapi import FastAPI
  app = FastAPI()
  app.include_router(router)
  client = TestClient(app)

  resp = client.get("/research/monte-carlo?iterations=100&ruin_drawdown_pct=0.10")
  assert resp.status_code == 200
  data = resp.json()

  assert "total_iterations" in data
  assert data["total_iterations"] == 100
  assert "ruin_probability_pct" in data
  assert "median_max_drawdown_pct" in data
  assert "sample_paths" in data
