"""
Unit tests for Milestone M22: Continuous Multi-Session Paper Soak & Statistical Drift Surveillance.

Test Suite Coverage:
  1. Two-sample Kolmogorov-Smirnov test with identical distributions (no drift detected)
  2. Two-sample Kolmogorov-Smirnov test with shifted distributions (significant drift detected)
  3. CUSUM sequential detector under stable returns (no change detected)
  4. CUSUM sequential detector under severe downward shift (change-point detected)
  5. Drift health score and tri-state status transitions (NORMAL -> WATCH -> CRITICAL_DRIFT)
  6. Automated fail-safe risk reduction: evaluate_and_guard trips RiskEngine kill switch
  7. Dynamic profile loading & calibrated M21 parameter overrides (CALIBRATED_RESEARCH_M21)
  8. REST API endpoint GET /surveillance/drift integration test via TestClient
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

# Project root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.core.config import load_app_config
from app.core.money import round_price, to_decimal
from app.execution.costs import NSECostEngine
from app.risk.engine import RiskEngine
from app.surveillance.drift import (
    BaselineDistribution,
    CUSUMDetector,
    CUSUMState,
    DriftSurveillanceReport,
    KSResult,
    StatisticalDriftEngine,
    compute_two_sample_ks,
)


# ---------------------------------------------------------------------------
# Test 1: Two-Sample KS Test on Identical Distributions
# ---------------------------------------------------------------------------

def test_two_sample_ks_identical_distributions():
    # Identical samples
    sample_a = [Decimal("0.50"), Decimal("1.00"), Decimal("-0.80"), Decimal("0.20"), Decimal("1.50")] * 5
    sample_b = [Decimal("0.50"), Decimal("1.00"), Decimal("-0.80"), Decimal("0.20"), Decimal("1.50")] * 5

    res = compute_two_sample_ks(sample_a, sample_b, alpha=0.05)
    assert isinstance(res, KSResult)
    assert res.statistic_d == Decimal("0.0000"), f"Expected D=0.0, got {res.statistic_d}"
    assert res.p_value >= Decimal("0.9000"), f"Expected high p-value, got {res.p_value}"
    assert res.drift_detected is False
    assert res.sample_sizes == (25, 25)
    print("  [PASS] test_two_sample_ks_identical_distributions")


# ---------------------------------------------------------------------------
# Test 2: Two-Sample KS Test on Shifted Distributions
# ---------------------------------------------------------------------------

def test_two_sample_ks_shifted_distributions():
    # Sample A is strongly positive, Sample B is strongly negative
    sample_a = [Decimal("1.50"), Decimal("2.00"), Decimal("1.20"), Decimal("0.80"), Decimal("1.80")] * 6
    sample_b = [Decimal("-1.50"), Decimal("-2.00"), Decimal("-1.20"), Decimal("-0.80"), Decimal("-1.80")] * 6

    res = compute_two_sample_ks(sample_a, sample_b, alpha=0.05)
    assert res.statistic_d == Decimal("1.0000"), f"Expected D=1.0 for non-overlapping support, got {res.statistic_d}"
    assert res.p_value < Decimal("0.0100"), f"Expected p < 0.01, got {res.p_value}"
    assert res.drift_detected is True
    print(f"  [PASS] test_two_sample_ks_shifted_distributions (D={res.statistic_d}, p={res.p_value})")


# ---------------------------------------------------------------------------
# Test 3: CUSUM Detector under Stable Returns
# ---------------------------------------------------------------------------

def test_cusum_detector_stable_sequence():
    detector = CUSUMDetector(
        reference_return=Decimal("-0.2404"),
        threshold_h=Decimal("2.50"),
        allowance_delta=Decimal("0.10"),
    )
    # Returns at or above reference return: ref - R - delta <= 0
    stable_returns = [Decimal("0.50"), Decimal("-0.10"), Decimal("0.20"), Decimal("1.00"), Decimal("-0.20")] * 5
    for r in stable_returns:
        detector.update(r)

    st = detector.get_state()
    assert st.cusum_value == Decimal("0.0000"), f"Expected C+=0 for winning sequence, got {st.cusum_value}"
    assert st.is_change_detected is False
    assert st.change_index is None
    assert st.observations_count == 25
    print("  [PASS] test_cusum_detector_stable_sequence")


# ---------------------------------------------------------------------------
# Test 4: CUSUM Detector under Severe Downward Drift
# ---------------------------------------------------------------------------

def test_cusum_detector_downward_drift_triggers_change_point():
    detector = CUSUMDetector(
        reference_return=Decimal("0.0000"),
        threshold_h=Decimal("2.00"),
        allowance_delta=Decimal("0.10"),
    )
    # Severe sequence of consecutive large losses (-1.0 R)
    # Each step: 0 - (-1.0) - 0.10 = +0.80 accumulation
    # At step 1: 0.80, step 2: 1.60, step 3: 2.40 >= 2.00 -> Change detected!
    severe_losses = [Decimal("-1.00"), Decimal("-1.00"), Decimal("-1.00"), Decimal("-1.00")]
    for r in severe_losses:
        detector.update(r)

    st = detector.get_state()
    assert st.is_change_detected is True
    assert st.change_index == 3, f"Expected change detected at step 3, got {st.change_index}"
    assert st.cusum_value >= Decimal("2.0000")
    print(f"  [PASS] test_cusum_detector_downward_drift_triggers_change_point (C+={st.cusum_value}, step={st.change_index})")


# ---------------------------------------------------------------------------
# Test 5: Drift Health Score & Tri-State Status Transitions
# ---------------------------------------------------------------------------

def test_drift_health_score_and_status_transitions():
    baseline = BaselineDistribution.default_m20_baseline()
    engine = StatisticalDriftEngine(baseline=baseline)

    # 1. Nominal returns matching baseline win rate and expectancy (interleaved wins and losses)
    nominal_returns = [Decimal("1.05"), Decimal("-0.93"), Decimal("-0.93")] * 7
    rep_nominal = engine.analyze_trades(nominal_returns)
    assert rep_nominal.alert_status == "NORMAL", f"Expected NORMAL, got {rep_nominal.alert_status}"
    assert rep_nominal.drift_health_score_pct >= Decimal("75.00"), f"Expected >= 75%, got {rep_nominal.drift_health_score_pct}"

    # 2. Catastrophic drift (all severe losses)
    catastrophic_returns = [Decimal("-2.50")] * 20
    rep_catastrophic = engine.analyze_trades(catastrophic_returns)
    assert rep_catastrophic.alert_status == "CRITICAL_DRIFT", f"Expected CRITICAL_DRIFT, got {rep_catastrophic.alert_status}"
    assert rep_catastrophic.drift_health_score_pct < Decimal("50.00"), f"Expected < 50%, got {rep_catastrophic.drift_health_score_pct}"
    assert rep_catastrophic.cusum_state.is_change_detected is True
    print(f"  [PASS] test_drift_health_score_and_status_transitions (Nominal={rep_nominal.drift_health_score_pct}%, Catastrophic={rep_catastrophic.drift_health_score_pct}%)")


# ---------------------------------------------------------------------------
# Test 6: Automated Fail-Safe Risk Reduction Trips Kill Switch
# ---------------------------------------------------------------------------

def test_fail_safe_guard_trips_risk_engine_kill_switch():
    cfg = load_app_config()
    cost_engine = NSECostEngine(cfg.costs[cfg.active_cost_profile])
    risk_prof = cfg.strategy.risk_profiles[cfg.active_risk_profile]
    risk_engine = RiskEngine(risk_prof, cost_engine)

    assert risk_engine.kill_switch_active is False

    engine = StatisticalDriftEngine()
    # Feed severe negative drift
    severe_drift = [Decimal("-2.00")] * 15
    report = engine.evaluate_and_guard(severe_drift, risk_engine)

    assert report.alert_status == "CRITICAL_DRIFT"
    assert risk_engine.kill_switch_active is True
    assert "CRITICAL_DRIFT" in risk_engine.kill_switch_reason
    print("  [PASS] test_fail_safe_guard_trips_risk_engine_kill_switch")


# ---------------------------------------------------------------------------
# Test 7: Calibrated M21 Strategy Profile Loading & Overrides
# ---------------------------------------------------------------------------

def test_calibrated_research_profile_configuration():
    cfg = load_app_config(risk_profile_name="CALIBRATED_RESEARCH_M21")
    assert cfg.active_risk_profile == "CALIBRATED_RESEARCH_M21"

    strat = cfg.strategy
    risk_prof = strat.risk_profiles["CALIBRATED_RESEARCH_M21"]

    # Verify M21 Bayesian calibrated parameters applied
    assert strat.opening_range_minutes == 20, f"Expected OR=20, got {strat.opening_range_minutes}"
    assert strat.min_rvol_threshold == Decimal("1.75"), f"Expected RVOL=1.75, got {strat.min_rvol_threshold}"
    assert risk_prof.initial_stop_pct == Decimal("0.0075"), f"Expected Stop=0.75%, got {risk_prof.initial_stop_pct}"
    assert risk_prof.trailing_activation_pct == Decimal("0.0300"), f"Expected TA=3.0%, got {risk_prof.trailing_activation_pct}"
    assert risk_prof.trailing_drop_pct == Decimal("0.0050"), f"Expected TD=0.50%, got {risk_prof.trailing_drop_pct}"
    assert strat.max_vwap_std_multiplier == Decimal("3.0"), f"Expected VWAP=3.0, got {strat.max_vwap_std_multiplier}"
    assert strat.require_macro_trend_alignment is True
    assert strat.require_two_day_high_breakout is True
    assert strat.require_vwap_volatility_cap is True
    assert strat.require_regime_filter is True
    print("  [PASS] test_calibrated_research_profile_configuration")


# ---------------------------------------------------------------------------
# Test 8: REST API GET /surveillance/drift Endpoint
# ---------------------------------------------------------------------------

def test_api_drift_surveillance_endpoint():
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get("/surveillance/drift")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"

    data = resp.json()
    assert "drift_health_score_pct" in data, f"Missing drift_health_score_pct: {list(data.keys())}"
    assert "alert_status" in data
    assert data["alert_status"] in ("NORMAL", "WATCH", "CRITICAL_DRIFT")
    assert "ks_test" in data
    assert "statistic_d" in data["ks_test"]
    assert "p_value" in data["ks_test"]
    assert "cusum" in data
    assert "cusum_value" in data["cusum"]
    assert "recommendations" in data
    assert isinstance(data["recommendations"], list)
    print(f"  [PASS] test_api_drift_surveillance_endpoint (Health={data['drift_health_score_pct']}%, Status={data['alert_status']})")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_two_sample_ks_identical_distributions,
        test_two_sample_ks_shifted_distributions,
        test_cusum_detector_stable_sequence,
        test_cusum_detector_downward_drift_triggers_change_point,
        test_drift_health_score_and_status_transitions,
        test_fail_safe_guard_trips_risk_engine_kill_switch,
        test_calibrated_research_profile_configuration,
        test_api_drift_surveillance_endpoint,
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
    print(f"\n=== M22 Tests: {passed} passed, {failed} failed ===")
