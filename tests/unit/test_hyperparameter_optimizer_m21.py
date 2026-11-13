"""
Unit tests for Milestone M21: Adaptive Signal Calibration & Bayesian Hyperparameter Optimizer.

Tests:
  1. HyperparamPoint creation, validation, and label generation
  2. Random grid sampler — validity constraints (trailing_activation > initial_stop)
  3. Composite score function correctness
  4. Config builder (_apply_params_to_config) — verifies all 8 params applied correctly
  5. Single-point evaluator on fast in-memory dataset (< 2s)
  6. GP surrogate (mean/variance predictions on small train set)
  7. Full optimizer pipeline on a tiny dataset (5 coarse + 3 bayes evals)
  8. API endpoint via TestClient — returns top_10_configs and best_params fields
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
import types

# Project root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.core.config import load_app_config
from app.core.money import to_decimal
from app.research.optimizer import (
    HyperparamPoint,
    HyperparamOptimizationResult,
    ParameterEvalResult,
    _apply_params_to_config,
    _compute_composite_score,
    _gp_predict,
    _point_to_vector,
    _random_grid_points,
    compute_wilson_score_interval,
    run_hyperparameter_optimization,
    MIN_TRADES_FOR_RANKING,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_point(
    orm: int = 15,
    rvol: str = "1.50",
    stop: str = "0.0125",
    act: str = "0.0250",
    drop: str = "0.0075",
    vwap: str = "2.5",
    trend: bool = False,
    hmm: bool = False,
) -> HyperparamPoint:
    return HyperparamPoint(
        opening_range_minutes=orm,
        min_rvol_threshold=Decimal(rvol),
        initial_stop_pct=Decimal(stop),
        trailing_activation_pct=Decimal(act),
        trailing_drop_pct=Decimal(drop),
        max_vwap_std_multiplier=Decimal(vwap),
        require_macro_trend=trend,
        require_regime_filter=hmm,
    )


def _make_eval_result(exp_r: float, sharpe: float, max_dd: float, trades: int = 20) -> ParameterEvalResult:
    r = ParameterEvalResult(
        point=_make_point(),
        total_trades=trades,
        wins=int(trades * 0.45),
        win_rate_pct=Decimal("45.00"),
        win_rate_ci_lower=Decimal("32.00"),
        win_rate_ci_upper=Decimal("58.00"),
        profit_factor=Decimal("1.10"),
        net_expectancy_r=Decimal(str(exp_r)),
        expectancy_ci_lower_r=Decimal(str(exp_r - 0.1)),
        expectancy_ci_upper_r=Decimal(str(exp_r + 0.1)),
        sharpe_ratio=Decimal(str(sharpe)),
        sortino_ratio=Decimal(str(sharpe * 0.8)),
        max_drawdown_pct=Decimal(str(max_dd)),
        avg_cost_to_risk_pct=Decimal("10.00"),
        t_statistic=Decimal("1.50"),
        p_value=Decimal("0.0668"),
        rejection_of_null=False,
        evaluation_time_s=0.1,
    )
    r.composite_score = _compute_composite_score(r)
    return r


# ---------------------------------------------------------------------------
# Test 1: HyperparamPoint construction and label
# ---------------------------------------------------------------------------

def test_hyperparam_point_construction_and_label():
    pt = _make_point(orm=10, rvol="1.25", stop="0.0100", act="0.0200", drop="0.0050", trend=True, hmm=False)
    assert pt.opening_range_minutes == 10
    assert pt.min_rvol_threshold == Decimal("1.25")
    assert pt.initial_stop_pct == Decimal("0.0100")
    assert pt.trailing_activation_pct == Decimal("0.0200")
    assert pt.require_macro_trend is True
    assert pt.require_regime_filter is False
    label = pt.label()
    assert "OR10m" in label
    assert "RVOL1.25" in label
    assert "TREND" in label
    assert "NOHMM" in label
    # to_dict completeness
    d = pt.to_dict()
    assert "opening_range_minutes" in d
    assert "require_macro_trend" in d
    assert d["opening_range_minutes"] == 10
    print("  [PASS] test_hyperparam_point_construction_and_label")


# ---------------------------------------------------------------------------
# Test 2: Random grid sampler validity constraints
# ---------------------------------------------------------------------------

def test_random_grid_sampler_constraints():
    points = _random_grid_points(50, seed=99)
    assert len(points) > 0, "Must generate at least 1 point"
    for pt in points:
        # trailing_activation must exceed initial_stop
        assert pt.trailing_activation_pct > pt.initial_stop_pct, (
            f"trailing_activation ({pt.trailing_activation_pct}) must > initial_stop ({pt.initial_stop_pct})"
        )
        # trailing_drop must be < trailing_activation
        assert pt.trailing_drop_pct < pt.trailing_activation_pct, (
            f"trailing_drop ({pt.trailing_drop_pct}) must < trailing_activation ({pt.trailing_activation_pct})"
        )
    # All labels must be unique
    labels = [p.label() for p in points]
    assert len(labels) == len(set(labels)), "Labels must be unique across random samples"
    print(f"  [PASS] test_random_grid_sampler_constraints ({len(points)} points, all valid)")


# ---------------------------------------------------------------------------
# Test 3: Composite score function
# ---------------------------------------------------------------------------

def test_composite_score_function():
    # High expectancy + high Sharpe + low DD → positive score
    good = _make_eval_result(exp_r=0.15, sharpe=0.80, max_dd=5.0, trades=25)
    assert float(good.composite_score) > 0.0, f"Expected positive score, got {good.composite_score}"

    # Negative expectancy + negative Sharpe + high DD → negative score
    bad = _make_eval_result(exp_r=-0.25, sharpe=-0.30, max_dd=15.0, trades=25)
    assert float(bad.composite_score) < float(good.composite_score), "Good must score better than bad"

    # Zero trades → penalised score of -999
    zero_trades = _make_eval_result(exp_r=0.0, sharpe=0.0, max_dd=0.0, trades=0)
    assert float(zero_trades.composite_score) == -999.0, f"Expected -999 for zero-trades, got {zero_trades.composite_score}"

    # Below MIN_TRADES_FOR_RANKING has penalty compared to full trades
    sub_min = _make_eval_result(exp_r=0.15, sharpe=0.80, max_dd=5.0, trades=MIN_TRADES_FOR_RANKING - 1)
    assert float(sub_min.composite_score) < float(good.composite_score)

    print(f"  [PASS] test_composite_score_function (good={good.composite_score:.4f}, bad={bad.composite_score:.4f})")


# ---------------------------------------------------------------------------
# Test 4: Config builder applies params correctly
# ---------------------------------------------------------------------------

def test_config_builder_applies_params():
    base_cfg = load_app_config()
    pt = _make_point(orm=5, rvol="0.00", stop="0.0150", act="0.0300", drop="0.0100",
                     vwap="2.0", trend=True, hmm=True)
    new_cfg = _apply_params_to_config(base_cfg, pt)

    strat = new_cfg.strategy
    risk = strat.risk_profiles[new_cfg.active_risk_profile]

    assert strat.opening_range_minutes == 5, f"Expected OR=5, got {strat.opening_range_minutes}"
    assert strat.min_rvol_threshold == Decimal("0.00"), f"Expected RVOL=0, got {strat.min_rvol_threshold}"
    assert risk.initial_stop_pct == Decimal("0.0150"), f"Expected stop=1.5%, got {risk.initial_stop_pct}"
    assert risk.trailing_activation_pct == Decimal("0.0300"), f"Expected TA=3%, got {risk.trailing_activation_pct}"
    assert risk.trailing_drop_pct == Decimal("0.0100"), f"Expected TD=1%, got {risk.trailing_drop_pct}"
    assert strat.max_vwap_std_multiplier == Decimal("2.0"), f"Expected VWAP=2.0, got {strat.max_vwap_std_multiplier}"
    assert strat.require_macro_trend_alignment is True
    assert strat.require_two_day_high_breakout is True
    assert strat.require_regime_filter is True
    # Base config must NOT be mutated
    assert base_cfg.strategy.opening_range_minutes != 5, "Base config must remain immutable"
    print("  [PASS] test_config_builder_applies_params")


# ---------------------------------------------------------------------------
# Test 5: GP surrogate mean/variance predictions
# ---------------------------------------------------------------------------

def test_gp_surrogate_predictions():
    # Simple 1-D case embedded in 8-D (all other dims fixed to 0.5)
    X_train = [
        [0.1, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5],
        [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5],
        [0.9, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5],
    ]
    y_train = [-0.5, 0.0, 0.5]  # linear trend in dim 0

    # Query near training point → low variance
    mu_near, sigma_near = _gp_predict(X_train, y_train, [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5])
    # Query far from training → higher variance
    mu_far, sigma_far = _gp_predict(X_train, y_train, [2.0, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5])

    assert sigma_far > sigma_near, (
        f"Uncertainty must be higher far from training data: near={sigma_near:.6f}, far={sigma_far:.6f}"
    )
    # Mean near training should be close to 0.0
    assert abs(mu_near) < 0.5, f"GP mean near training should be ~0.0, got {mu_near:.4f}"
    print(f"  [PASS] test_gp_surrogate_predictions (mu_near={mu_near:.4f}, σ_near={sigma_near:.6f}, σ_far={sigma_far:.6f})")


# ---------------------------------------------------------------------------
# Test 6: Point-to-vector normalisation
# ---------------------------------------------------------------------------

def test_point_to_vector_normalisation():
    pt_max = _make_point(orm=30, rvol="2.00", stop="0.0200", act="0.0300", drop="0.0100", vwap="3.0", trend=True, hmm=True)
    pt_min = _make_point(orm=5, rvol="0.00", stop="0.0075", act="0.0200", drop="0.0050", vwap="2.0", trend=False, hmm=False)

    vec_max = _point_to_vector(pt_max)
    vec_min = _point_to_vector(pt_min)

    # All values should be in [0, ∞) — normalised by axis max
    for v in vec_max:
        assert v >= 0.0, f"Vector value must be non-negative, got {v}"
    # Binary flags correctly encoded
    assert vec_max[-2] == 1.0  # require_macro_trend
    assert vec_max[-1] == 1.0  # require_regime_filter
    assert vec_min[-2] == 0.0
    assert vec_min[-1] == 0.0
    # Max OR normalised to 1.0
    assert abs(vec_max[0] - 1.0) < 1e-9, f"Max OR should normalise to 1.0, got {vec_max[0]}"
    print("  [PASS] test_point_to_vector_normalisation")


# ---------------------------------------------------------------------------
# Test 7: Full optimizer pipeline on tiny in-memory dataset
# ---------------------------------------------------------------------------

def test_optimizer_pipeline_tiny_dataset():
    """
    Runs the full 2-stage optimizer on a tiny SQLite in-memory dataset.
    Uses 3 coarse evaluations and 2 Bayesian iterations for speed (< 30s).
    Verifies structure of HyperparamOptimizationResult.
    """
    import sqlite3
    from app.core.database import SCHEMA_DDL
    from app.core.money import round_price

    import tempfile, os
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_path = tmp.name
    tmp.close()

    # Build DB with synthetic bars for RELIANCE
    disk_conn = sqlite3.connect(tmp_path)
    disk_conn.executescript(SCHEMA_DDL)

    # Insert 200 synthetic 1m bars (a simple uptrend for RELIANCE)
    base_ts = "2026-10-01T03:45:00"  # 09:15 IST = 03:45 UTC
    rows = []
    base_price = 2800.0
    for i in range(200):
        minute_offset = i
        h, m = divmod(minute_offset, 60)
        from datetime import datetime, timedelta, timezone
        dt = datetime(2026, 10, 1, 3, 45, tzinfo=timezone.utc) + timedelta(minutes=i)
        ts_str = dt.isoformat()
        price = base_price + i * 0.5
        rows.append((
            "RELIANCE", 1, ts_str,
            round(price, 2), round(price + 2, 2), round(price - 1, 2), round(price + 0.5, 2),
            100000
        ))
    disk_conn.executemany(
        "INSERT OR IGNORE INTO market_candles (symbol, interval_minutes, timestamp_utc, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows
    )
    disk_conn.commit()
    disk_conn.close()

    try:
        result = run_hyperparameter_optimization(
            db_url=f"sqlite:///{tmp_path}",
            symbols=["RELIANCE"],
            bars_limit=200,
            max_coarse_evals=3,
            bayesian_iterations=2,
            top_k_seeds=2,
            is_fraction=0.70,
            random_seed=7,
        )
    finally:
        os.unlink(tmp_path)

    # Structure verification
    assert isinstance(result, HyperparamOptimizationResult)
    assert result.data_provenance == "REAL_HISTORICAL_SQLITE"
    assert result.total_evaluations >= 3  # at least coarse evals completed
    assert result.symbols == ["RELIANCE"]
    assert len(result.coarse_results) >= 1
    # to_dict works
    d = result.to_dict()
    assert "top_10_configs" in d
    assert "best_params" in d
    assert "improvement_vs_baseline_r" in d
    print(f"  [PASS] test_optimizer_pipeline_tiny_dataset "
          f"({result.total_evaluations} evals, {result.total_candles_evaluated} candles)")


# ---------------------------------------------------------------------------
# Test 8: API endpoint via TestClient
# ---------------------------------------------------------------------------

def test_api_hyperparameter_optimization_endpoint():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    # Use very small params to keep it fast (bars_limit=100, coarse=3, bayes=2)
    resp = client.get(
        "/research/hyperparameter-optimization?symbols=RELIANCE&bars_limit=100&coarse_evals=3&bayes_iters=2"
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    data = resp.json()
    assert "top_10_configs" in data, f"Missing top_10_configs in response: {list(data.keys())}"
    assert "best_params" in data, f"Missing best_params in response: {list(data.keys())}"
    assert "total_evaluations" in data
    assert data["total_evaluations"] >= 3
    assert isinstance(data["top_10_configs"], list)
    print(f"  [PASS] test_api_hyperparameter_optimization_endpoint "
          f"(total_evaluations={data['total_evaluations']})")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_hyperparam_point_construction_and_label,
        test_random_grid_sampler_constraints,
        test_composite_score_function,
        test_config_builder_applies_params,
        test_gp_surrogate_predictions,
        test_point_to_vector_normalisation,
        test_optimizer_pipeline_tiny_dataset,
        test_api_hyperparameter_optimization_endpoint,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            import traceback
            print(f"  [FAIL] {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n=== M21 Tests: {passed} passed, {failed} failed ===")
