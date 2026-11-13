"""
Milestone M20: Augmented Multi-Month Walk-Forward & Factor Ablation Tests
=========================================================================
6 deterministic test functions verifying:
  1. Binomial 95% Wilson Score Confidence Interval calculations
  2. Student's t expectancy statistics, Sharpe, Sortino & one-tailed hypothesis test
  3. Factor ablation model configurations across Models 0 to 4
  4. Factor ablation engine multi-model backtest execution on synthetic data
  5. Marginal delta calculations relative to Model 0 baseline
  6. GET /research/augmented-study FastAPI endpoint response schema and integration
"""

import sys, os
from decimal import Decimal
from datetime import datetime, timezone
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.core.config import load_app_config
from app.core.clock import ist_datetime
from app.data.models import CandleBar
from app.main import app
from app.research.ablation import (
    FactorAblationEngine,
    ModelAblationMetrics,
    FactorAblationStudyResult,
    compute_wilson_score_interval,
    compute_expectancy_stats,
)


def _generate_synthetic_breakout_bars(symbol: str = "TEST_STOCK") -> tuple[list[CandleBar], Decimal]:
  """Constructs a deterministic 1-day candle bar sequence that forms an ORB and breaks out."""
  bars: list[CandleBar] = []
  prev_close = Decimal("1000.00")

  # 09:15 - 09:29: 15 bars for the opening range [1000.00, 1010.00]
  for m in range(15):
    ts = ist_datetime(2026, 11, 10, 9, 15 + m)
    bars.append(CandleBar(
        symbol=symbol,
        interval_minutes=1,
        timestamp_utc=ts,
        open=Decimal("1002.00"),
        high=Decimal("1010.00"),
        low=Decimal("1000.00"),
        close=Decimal("1005.00"),
        volume=Decimal("1000"),
        source="ANGEL_SMARTAPI",
    ))

  # 09:30 - 09:34: 5m bar that breaks out above 1010.00 with high volume
  for m in range(15, 20):
    ts = ist_datetime(2026, 11, 10, 9, m)
    bars.append(CandleBar(
        symbol=symbol,
        interval_minutes=1,
        timestamp_utc=ts,
        open=Decimal("1011.00"),
        high=Decimal("1015.00"),
        low=Decimal("1010.50"),
        close=Decimal("1014.00"),
        volume=Decimal("5000"),  # High RVOL
        source="ANGEL_SMARTAPI",
    ))

  # 09:35 - 10:00: Rising prices to hit profit target or run higher
  for m in range(20, 45):
    ts = ist_datetime(2026, 11, 10, 9, m if m < 60 else 59)
    bars.append(CandleBar(
        symbol=symbol,
        interval_minutes=1,
        timestamp_utc=ts,
        open=Decimal("1014.00"),
        high=Decimal("1025.00"),
        low=Decimal("1013.00"),
        close=Decimal("1022.00"),
        volume=Decimal("2000"),
        source="ANGEL_SMARTAPI",
    ))

  return bars, prev_close


# ---------------------------------------------------------------------------
# Test 1: Wilson Score Interval Calculations
# ---------------------------------------------------------------------------

def test_wilson_score_interval_bounds():
  """Verifies Wilson score confidence intervals for edge cases and realistic proportions."""
  # Case A: 0 samples
  lower, upper = compute_wilson_score_interval(0, 0)
  assert lower == Decimal("0.00")
  assert upper == Decimal("0.00")

  # Case B: 50 wins out of 100 trials (50.0%)
  lower, upper = compute_wilson_score_interval(50, 100)
  assert Decimal("40.00") <= lower <= Decimal("42.00"), f"Unexpected lower bound: {lower}"
  assert Decimal("58.00") <= upper <= Decimal("60.00"), f"Unexpected upper bound: {upper}"
  assert lower < Decimal("50.00") < upper

  # Case C: 100 wins out of 100 trials (100.0%)
  lower, upper = compute_wilson_score_interval(100, 100)
  assert Decimal("95.00") <= lower <= Decimal("97.00")
  assert upper == Decimal("100.00")

  # Case D: 0 wins out of 100 trials (0.0%)
  lower, upper = compute_wilson_score_interval(0, 100)
  assert lower == Decimal("0.00")
  assert Decimal("3.00") <= upper <= Decimal("5.00")


# ---------------------------------------------------------------------------
# Test 2: Expectancy Statistics & Hypothesis Testing
# ---------------------------------------------------------------------------

def test_expectancy_stats_and_hypothesis_test():
  """Verifies Student's t expectancy stats, Sharpe/Sortino ratios, and one-tailed t-test."""
  # Case A: Empty list
  empty_stats = compute_expectancy_stats([])
  assert empty_stats["mean_r"] == Decimal("0.0000")
  assert empty_stats["rejection_of_null"] is False
  assert empty_stats["p_value"] == Decimal("1.0000")

  # Case B: Strongly positive R-multiples with occasional loss
  pos_r = [Decimal("1.50"), Decimal("2.00"), Decimal("-0.40"), Decimal("1.20"), Decimal("1.80")] * 10
  pos_stats = compute_expectancy_stats(pos_r)
  assert pos_stats["mean_r"] > Decimal("1.0000")
  assert pos_stats["se_r"] > Decimal("0.0000")
  assert pos_stats["sharpe_ratio"] > Decimal("1.00")
  assert pos_stats["sortino_ratio"] > Decimal("1.00")
  assert pos_stats["t_statistic"] > Decimal("5.00")
  assert pos_stats["p_value"] < Decimal("0.0100")
  assert pos_stats["rejection_of_null"] is True

  # Case C: Negative R-multiples (Losses)
  neg_r = [Decimal("-1.00"), Decimal("-0.80"), Decimal("-1.00"), Decimal("-0.50")] * 5
  neg_stats = compute_expectancy_stats(neg_r)
  assert neg_stats["mean_r"] < Decimal("0.0000")
  assert neg_stats["rejection_of_null"] is False
  assert neg_stats["t_statistic"] < Decimal("0.00")


# ---------------------------------------------------------------------------
# Test 3: Factor Ablation Model Configurations
# ---------------------------------------------------------------------------

def test_factor_ablation_model_configs():
  """Verifies the exact boolean toggles across the 5 ablation model tiers."""
  cfg = load_app_config()
  engine = FactorAblationEngine(cfg)

  # Model 0: Baseline Naive ORB
  m0 = engine._build_model_config("MODEL_0_NAIVE_ORB")
  s0 = m0.strategy
  assert s0.min_rvol_threshold == Decimal("0.0")
  assert s0.require_macro_trend_alignment is False
  assert s0.require_two_day_high_breakout is False
  assert s0.require_vwap_volatility_cap is False
  assert s0.require_regime_filter is False
  assert m0.costs[m0.active_cost_profile].enable_market_impact is False

  # Model 1: Trend & RVOL
  m1 = engine._build_model_config("MODEL_1_TREND_RVOL")
  s1 = m1.strategy
  assert s1.min_rvol_threshold == Decimal("1.50")
  assert s1.require_macro_trend_alignment is True
  assert s1.require_two_day_high_breakout is True
  assert s1.require_vwap_volatility_cap is False
  assert s1.require_regime_filter is False

  # Model 2: VWAP Bands
  m2 = engine._build_model_config("MODEL_2_VWAP_BANDS")
  s2 = m2.strategy
  assert s2.require_macro_trend_alignment is True
  assert s2.require_vwap_volatility_cap is True
  assert s2.max_vwap_std_multiplier == Decimal("2.5")
  assert s2.require_regime_filter is False

  # Model 3: HMM Regime
  m3 = engine._build_model_config("MODEL_3_HMM_REGIME")
  s3 = m3.strategy
  assert s3.require_regime_filter is True
  assert s3.allowed_regimes == ["BULL", "NEUTRAL"]
  assert m3.costs[m3.active_cost_profile].enable_market_impact is False

  # Model 4: Full Institutional Stack
  m4 = engine._build_model_config("MODEL_4_FULL_STACK")
  s4 = m4.strategy
  r4 = m4.strategy.risk_profiles[m4.active_risk_profile]
  assert s4.require_regime_filter is True
  assert m4.costs[m4.active_cost_profile].enable_market_impact is True
  assert r4.require_participation_rate_cap is True
  assert r4.require_portfolio_var_cap is True
  assert r4.adaptive_atr_trailing is True


# ---------------------------------------------------------------------------
# Test 4: Factor Ablation Engine Multi-Model Execution
# ---------------------------------------------------------------------------

def test_factor_ablation_engine_execution():
  """Runs the 5-model ablation study on deterministic synthetic candles."""
  cfg = load_app_config()
  engine = FactorAblationEngine(cfg)

  bars, prev_close = _generate_synthetic_breakout_bars("TATASTEEL")
  bars_by_symbol = {"TATASTEEL": bars}
  prev_closes = {"TATASTEEL": prev_close}

  res = engine.run_ablation_study(bars_by_symbol, prev_closes)

  assert isinstance(res, FactorAblationStudyResult)
  assert res.data_provenance == "REAL_HISTORICAL_SQLITE"
  assert res.total_sessions >= 1
  assert res.total_candles == len(bars)
  assert len(res.models) == 5

  expected_ids = [
      "MODEL_0_NAIVE_ORB",
      "MODEL_1_TREND_RVOL",
      "MODEL_2_VWAP_BANDS",
      "MODEL_3_HMM_REGIME",
      "MODEL_4_FULL_STACK",
  ]
  actual_ids = [m.model_id for m in res.models]
  assert actual_ids == expected_ids
  assert res.best_model_id in expected_ids
  assert len(res.alpha_improvement_summary) > 0


# ---------------------------------------------------------------------------
# Test 5: Marginal Delta Attribution Checks
# ---------------------------------------------------------------------------

def test_ablation_marginal_deltas():
  """Verifies that marginal deltas are correctly calculated relative to Model 0."""
  cfg = load_app_config()
  engine = FactorAblationEngine(cfg)

  bars, prev_close = _generate_synthetic_breakout_bars("INFY")
  res = engine.run_ablation_study({"INFY": bars}, {"INFY": prev_close})

  m0 = res.models[0]
  assert m0.model_id == "MODEL_0_NAIVE_ORB"
  assert m0.delta_win_rate_pct == Decimal("0.00")
  assert m0.delta_expectancy_r == Decimal("0.0000")
  assert m0.delta_profit_factor == Decimal("0.00")
  assert m0.delta_max_dd_pct == Decimal("0.00")

  for m in res.models[1:]:
    expected_delta_wr = m.win_rate_pct - m0.win_rate_pct
    expected_delta_exp = m.net_expectancy_r - m0.net_expectancy_r
    assert m.delta_win_rate_pct == expected_delta_wr
    assert m.delta_expectancy_r == expected_delta_exp


# ---------------------------------------------------------------------------
# Test 6: API Endpoint Integration (GET /research/augmented-study)
# ---------------------------------------------------------------------------

def test_api_augmented_study_endpoint():
  """Verifies the GET /research/augmented-study route returns 200 with valid ablation schema."""
  client = TestClient(app)
  resp = client.get("/research/augmented-study?symbols=RELIANCE&bars_limit=150")
  assert resp.status_code == 200

  data = resp.json()
  assert "data_provenance" in data
  assert "total_sessions" in data
  assert "total_candles" in data
  assert "models" in data
  assert "best_model_id" in data
  assert len(data["models"]) == 5

  # Check Model 0 properties
  m0 = data["models"][0]
  assert m0["model_id"] == "MODEL_0_NAIVE_ORB"
  assert "win_rate_pct" in m0
  assert "profit_factor" in m0
  assert "net_expectancy_r" in m0
  assert "rejection_of_null" in m0
