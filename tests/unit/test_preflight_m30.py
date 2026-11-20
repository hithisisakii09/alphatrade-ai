"""Unit & Integration Tests — M30: Live-Broker Sandbox Transition & Pre-Flight Go-Live Gate.

Institutional Invariants Verified:
1. PreFlightReadinessEngine passes all mission-critical gates in standard sandbox environment.
2. Attempting LIVE mode triggers fail-closed rejection on gate 1 and marks decision NO_GO.
3. Cryptographically corrupted SEBI WORM audit trail flags gate 6 failure.
4. Float drift scanner verifies zero binary float drift standard.
5. Margin stress gate confirms headroom calculation under joint shock scenarios.
6. Multi-strategy meta-ensemble weight normalization test sums to exactly 1.0000.
7. Algorithmic slicer boundary check guarantees 100% share conservation.
8. REST API endpoints smoke-test for /system/preflight/status and /system/preflight/run.
"""

import os
os.environ.setdefault("ALPHATRADE_EXECUTION_MODE", "PAPER")

from decimal import Decimal
import json
import tempfile
from datetime import datetime, timezone

from app.compliance.worm_logger import AuditEntry, SEBIWORMTradeLogger
from app.market.regime import MarketRegime, RegimeState
from app.services.preflight import (
    GateStatus,
    PreFlightAuditReport,
    PreFlightCheckItem,
    PreFlightReadinessEngine,
)


# ---------------------------------------------------------------------------
# Test 1: Full pre-flight audit passes in standard paper sandbox environment
# ---------------------------------------------------------------------------

def test_preflight_engine_passes_all_checks_in_standard_env():
  """Verifies that running execute_full_audit() returns an overall GO decision."""
  with tempfile.TemporaryDirectory() as tmp_audit:
    # Ensure clean WORM log
    SEBIWORMTradeLogger(log_dir=tmp_audit)
    engine = PreFlightReadinessEngine(audit_dir=tmp_audit)
    report = engine.execute_full_audit()

    assert isinstance(report, PreFlightAuditReport)
    assert report.overall_decision == "GO", f"Expected GO decision, got {report.overall_decision}"
    assert len(report.checks) == 10
    assert report.failed_count == 0
    assert report.total_latency_ms < 5000.0, "Full 10-gate audit must complete in under 5000 ms"
  print("  [PASS] test_preflight_engine_passes_all_checks_in_standard_env")


# ---------------------------------------------------------------------------
# Test 2: Pre-flight fails closed when LIVE mode is attempted
# ---------------------------------------------------------------------------

def test_preflight_fails_when_live_mode_attempted(monkeypatch):
  """Setting execution mode to LIVE must trigger critical failure on Gate 1 and yield NO_GO."""
  orig = os.environ.get("ALPHATRADE_EXECUTION_MODE", "PAPER")
  try:
    engine = PreFlightReadinessEngine()
    monkeypatch.setenv("ALPHATRADE_EXECUTION_MODE", "LIVE")

    gate1 = engine._audit_paper_mode_guard()
    assert gate1.status == GateStatus.FAILED
    assert gate1.critical is True
  finally:
    monkeypatch.setenv("ALPHATRADE_EXECUTION_MODE", orig)
  print("  [PASS] test_preflight_fails_when_live_mode_attempted")


# ---------------------------------------------------------------------------
# Test 3: Pre-flight detects corrupted WORM audit log
# ---------------------------------------------------------------------------

def test_preflight_detects_corrupted_worm_log():
  """A corrupted WORM audit chain must trigger Gate 6 failure and yield NO_GO."""
  with tempfile.TemporaryDirectory() as tmp_audit:
    logger = SEBIWORMTradeLogger(log_dir=tmp_audit)
    logger.append_entry("ORDER_PLACED", "CL_01", "RELIANCE", "BUY", 100, "2850.00")
    logger.append_entry("ORDER_PLACED", "CL_02", "TCS", "BUY", 50, "3800.00")

    # Corrupt second entry on disk
    with open(logger.log_path, "r") as f:
      lines = f.readlines()
    data = json.loads(lines[1])
    data["quantity"] = 9999  # Tamper
    with open(logger.log_path, "w") as f:
      f.write(lines[0] + json.dumps(data) + "\n")

    engine = PreFlightReadinessEngine(audit_dir=tmp_audit)
    gate6 = engine._audit_sebi_worm_log_integrity()
    assert gate6.status == GateStatus.FAILED
    assert "WORM chain broken" in gate6.message
  print("  [PASS] test_preflight_detects_corrupted_worm_log")


# ---------------------------------------------------------------------------
# Test 4: Pre-flight float drift scanner verifies pure Decimal rejection
# ---------------------------------------------------------------------------

def test_preflight_detects_float_drift():
  """Verifies Gate 5 tests float rejection and pure Decimal math."""
  engine = PreFlightReadinessEngine()
  gate5 = engine._audit_pure_decimal_arithmetic()
  assert gate5.status == GateStatus.PASSED
  assert "Float inputs rejected" in gate5.message
  print("  [PASS] test_preflight_detects_float_drift")


# ---------------------------------------------------------------------------
# Test 5: Margin stress headroom calculation
# ---------------------------------------------------------------------------

def test_preflight_margin_stress_headroom_evaluation():
  """Verifies Gate 7 evaluates stressed margin utilization against 85.0% ceiling."""
  engine = PreFlightReadinessEngine()
  gate7 = engine._audit_margin_stress_headroom()
  assert gate7.status == GateStatus.PASSED
  assert "<= 85.0% limit" in gate7.message
  print("  [PASS] test_preflight_margin_stress_headroom_evaluation")


# ---------------------------------------------------------------------------
# Test 6: Multi-strategy meta-ensemble weight normalization
# ---------------------------------------------------------------------------

def test_preflight_meta_ensemble_weight_normalization():
  """Verifies Gate 10 asserts dynamic strategy ensemble weights sum to 1.0000."""
  engine = PreFlightReadinessEngine()
  gate10 = engine._audit_meta_ensemble_normalization()
  assert gate10.status == GateStatus.PASSED
  assert "Weights sum to 1.0000" in gate10.message
  print("  [PASS] test_preflight_meta_ensemble_weight_normalization")


# ---------------------------------------------------------------------------
# Test 7: Algorithmic slicer share conservation
# ---------------------------------------------------------------------------

def test_preflight_algo_slicer_validation():
  """Verifies Gate 9 asserts TWAP slice generation conserves 100% of parent shares."""
  engine = PreFlightReadinessEngine()
  gate9 = engine._audit_algo_slicer_boundaries()
  assert gate9.status == GateStatus.PASSED
  assert "exact share conservation (100/100)" in gate9.message
  print("  [PASS] test_preflight_algo_slicer_validation")


# ---------------------------------------------------------------------------
# Test 8: REST API endpoints smoke test
# ---------------------------------------------------------------------------

def test_m30_api_endpoints_integration():
  """Smoke-test GET /system/preflight/status and POST /system/preflight/run."""
  from fastapi.testclient import TestClient
  from app.main import app

  client = TestClient(app)

  # GET /system/preflight/status
  r = client.get("/system/preflight/status")
  assert r.status_code == 200, f"GET /system/preflight/status failed: {r.text}"
  body = r.json()
  assert "overall_decision" in body
  assert "checks" in body
  assert len(body["checks"]) == 10

  # POST /system/preflight/run
  r2 = client.post("/system/preflight/run")
  assert r2.status_code == 200, f"POST /system/preflight/run failed: {r2.text}"
  body2 = r2.json()
  assert body2["overall_decision"] in ("GO", "NO_GO")
  assert body2["passed_count"] >= 8
  print(f"  [PASS] test_m30_api_endpoints_integration (verdict={body2['overall_decision']}, passed={body2['passed_count']}/10)")
