"""Unit & Integration Tests — M28: Automated Disaster Recovery, Hot Snapshots & Intraday State Replay.

Institutional Invariants Verified:
1. SHA-256 cryptographic integrity on every snapshot round-trip.
2. Snapshot corruption detection raises SnapshotIntegrityError.
3. Zero-downtime rehydration restores 100% of positions and algo orders.
4. Deterministic tick replayer reports zero discrepancies on clean state.
5. Fail-closed paper-mode guard rejects LIVE-mode execution attempts.
6. REST API endpoints smoke-test for /system/recovery/status and POST /system/recovery/checkpoint.
"""

import os
os.environ.setdefault("ALPHATRADE_EXECUTION_MODE", "PAPER")

import json
import tempfile
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.services.recovery import (
    HotStateSnapshotManager,
    IntradayTickReplayer,
    SnapshotIntegrityError,
    StateSnapshot,
    RehydrationReport,
)
from app.data.models import CandleBar


# ---------------------------------------------------------------------------
# Shared test data helpers
# ---------------------------------------------------------------------------

def _make_positions():
    return [
        {
            "position_id": "POS_RELIANCE_001",
            "symbol": "RELIANCE",
            "quantity": 100,
            "entry_price": "2850.00",
            "current_stop_price": "2885.00",
            "highest_price_since_entry": "2925.00",
            "trailing_activated": True,
        },
        {
            "position_id": "POS_TCS_001",
            "symbol": "TCS",
            "quantity": 50,
            "entry_price": "3800.00",
            "current_stop_price": "3752.50",
            "highest_price_since_entry": "3815.00",
            "trailing_activated": False,
        },
    ]


def _make_algos():
    return [
        {
            "parent_order_id": "ALGO_TWAP_INFY_001",
            "symbol": "INFY",
            "side": "BUY",
            "total_quantity": 300,
            "filled_quantity": 100,
            "algo_type": "TWAP",
            "status": "ACTIVE",
        }
    ]


def _make_ledger():
    return {
        "cash_balance_inr": "850000.00",
        "deployed_exposure_inr": "475000.00",
        "realized_pnl_inr": "4500.00",
    }


def _make_mgr(tmp_dir: str) -> HotStateSnapshotManager:
    return HotStateSnapshotManager(snapshot_dir=os.path.join(tmp_dir, "snapshots"))


# ---------------------------------------------------------------------------
# Test 1: Snapshot creation and SHA-256 integrity round-trip
# ---------------------------------------------------------------------------

def test_snapshot_creation_and_sha256_integrity():
    """Create snapshot, verify SHA-256 checksum is non-empty and verifies on load."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _make_mgr(tmp)
        snapshot = mgr.create_snapshot(
            session_date="2026-11-14",
            open_positions=_make_positions(),
            active_algo_orders=_make_algos(),
            ledger_state=_make_ledger(),
            broker_positions={"RELIANCE": 100, "TCS": 50},
        )

        assert snapshot.snapshot_id.startswith("SNAP_2026-11-14_"), \
            "Snapshot ID must carry session date prefix"
        assert len(snapshot.sha256_checksum) == 64, \
            "SHA-256 hex digest must be 64 characters"
        assert len(snapshot.open_positions) == 2

        # Round-trip load — must pass integrity check without raising
        loaded = mgr.load_latest_snapshot()
        assert loaded is not None
        assert loaded.snapshot_id == snapshot.snapshot_id
        assert loaded.sha256_checksum == snapshot.sha256_checksum
        assert mgr.total_snapshots_created == 1
    print("  [PASS] test_snapshot_creation_and_sha256_integrity")


# ---------------------------------------------------------------------------
# Test 2: Snapshot load restores positions with full fidelity
# ---------------------------------------------------------------------------

def test_snapshot_load_and_verify_positions():
    """Load snapshot and verify every position field is preserved exactly."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _make_mgr(tmp)
        mgr.create_snapshot(
            session_date="2026-11-14",
            open_positions=_make_positions(),
            ledger_state=_make_ledger(),
        )
        loaded = mgr.load_latest_snapshot()

        assert loaded is not None
        assert len(loaded.open_positions) == 2

        rel = next(p for p in loaded.open_positions if p["symbol"] == "RELIANCE")
        assert rel["quantity"] == 100
        assert rel["entry_price"] == "2850.00"
        assert rel["trailing_activated"] is True
        assert rel["highest_price_since_entry"] == "2925.00"

        tcs = next(p for p in loaded.open_positions if p["symbol"] == "TCS")
        assert tcs["trailing_activated"] is False
        assert tcs["current_stop_price"] == "3752.50"
    print("  [PASS] test_snapshot_load_and_verify_positions")


# ---------------------------------------------------------------------------
# Test 3: Snapshot integrity violation raises SnapshotIntegrityError
# ---------------------------------------------------------------------------

def test_snapshot_integrity_violation_raises_error():
    """Corrupting the snapshot JSON must raise SnapshotIntegrityError on load."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _make_mgr(tmp)
        mgr.create_snapshot(
            session_date="2026-11-14",
            open_positions=_make_positions(),
            ledger_state=_make_ledger(),
        )

        # Tamper: corrupt one position's quantity
        with open(mgr.latest_snapshot_path, "r") as f:
            payload = json.load(f)
        payload["open_positions"][0]["quantity"] = 9999  # tamper
        with open(mgr.latest_snapshot_path, "w") as f:
            json.dump(payload, f)

        try:
            mgr.load_latest_snapshot()
            raise AssertionError("Expected SnapshotIntegrityError was not raised")
        except SnapshotIntegrityError as e:
            assert "integrity violation" in str(e).lower()
    print("  [PASS] test_snapshot_integrity_violation_raises_error")


# ---------------------------------------------------------------------------
# Test 4: Rehydration with no snapshot returns HEALTHY with zero positions
# ---------------------------------------------------------------------------

def test_rehydration_with_no_snapshot_returns_healthy():
    """When no snapshot file exists, rehydrate_system_state must return HEALTHY with zero count."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _make_mgr(tmp)
        report = mgr.rehydrate_system_state()

        assert isinstance(report, RehydrationReport)
        assert report.snapshot_id == "NONE"
        assert report.positions_restored == 0
        assert report.algos_rearmed == 0
        assert report.reconciliation_status == "HEALTHY"
        assert report.latency_ms >= 0.0
    print("  [PASS] test_rehydration_with_no_snapshot_returns_healthy")


# ---------------------------------------------------------------------------
# Test 5: Rehydration restores positions and algo orders
# ---------------------------------------------------------------------------

def test_rehydration_restores_positions_and_algos():
    """Full rehydration must restore correct counts for positions and algo orders."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _make_mgr(tmp)
        mgr.create_snapshot(
            session_date="2026-11-14",
            open_positions=_make_positions(),
            active_algo_orders=_make_algos(),
            ledger_state=_make_ledger(),
        )

        report = mgr.rehydrate_system_state(expected_session_date="2026-11-14")

        assert report.positions_restored == 2, "Must restore both RELIANCE and TCS positions"
        assert report.algos_rearmed == 1, "Must rearm the TWAP INFY parent order"
        assert report.reconciliation_status == "HEALTHY"
        assert report.latency_ms < 500.0, "Rehydration must complete within 500ms"
        assert report.snapshot_id.startswith("SNAP_")
    print("  [PASS] test_rehydration_restores_positions_and_algos")


# ---------------------------------------------------------------------------
# Test 6: IntradayTickReplayer reports zero discrepancies on clean state
# ---------------------------------------------------------------------------

def test_tick_replayer_deterministic_match():
    """Replay bars for the same symbols present in rehydrated positions → 0 discrepancies."""
    now_utc = datetime.now(timezone.utc)
    replayer = IntradayTickReplayer()

    bars = [
        CandleBar(
            symbol="RELIANCE",
            timestamp_utc=now_utc,
            open=Decimal("2850.00"),
            high=Decimal("2925.00"),
            low=Decimal("2845.00"),
            close=Decimal("2920.00"),
            volume=50000,
        ),
        CandleBar(
            symbol="TCS",
            timestamp_utc=now_utc,
            open=Decimal("3800.00"),
            high=Decimal("3815.00"),
            low=Decimal("3790.00"),
            close=Decimal("3805.00"),
            volume=30000,
        ),
    ]

    result = replayer.replay_and_compare(
        historical_bars=bars,
        rehydrated_positions=_make_positions(),
    )

    assert result["discrepancies_count"] == 0
    assert result["is_deterministic_match"] is True
    assert result["status"] == "VERIFIED_DETERMINISTIC"
    assert result["total_bars_replayed"] == 2
    assert set(result["matching_symbols"]) == {"RELIANCE", "TCS"}
    print("  [PASS] test_tick_replayer_deterministic_match")


# ---------------------------------------------------------------------------
# Test 7: Fail-closed paper mode guard
# ---------------------------------------------------------------------------

def test_fail_closed_paper_mode_guard(monkeypatch):
    """Setting LIVE mode must trigger LiveTradingProhibitedError on HotStateSnapshotManager init."""
    from app.core.execution_guard import LiveTradingProhibitedError

    monkeypatch.setenv("ALPHATRADE_EXECUTION_MODE", "LIVE")
    try:
        HotStateSnapshotManager(snapshot_dir="tmp_live_snap_guard")
        raise AssertionError("Expected LiveTradingProhibitedError was not raised")
    except LiveTradingProhibitedError:
        pass
    print("  [PASS] test_fail_closed_paper_mode_guard")


# ---------------------------------------------------------------------------
# Test 8: REST API endpoint integration smoke test
# ---------------------------------------------------------------------------

def test_m28_api_endpoints_integration():
    """Smoke-test GET /system/recovery/status and POST /system/recovery/checkpoint."""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    # GET /system/recovery/status
    r = client.get("/system/recovery/status")
    assert r.status_code == 200, f"GET /system/recovery/status failed: {r.text}"
    body = r.json()
    assert "snapshot_exists" in body
    assert "total_snapshots_created" in body
    assert "timestamp_utc" in body

    # POST /system/recovery/checkpoint
    r2 = client.post("/system/recovery/checkpoint")
    assert r2.status_code == 200, f"POST /system/recovery/checkpoint failed: {r2.text}"
    body2 = r2.json()
    assert body2["status"] == "SNAPSHOT_CREATED"
    assert "snapshot_id" in body2
    assert body2["sha256"].endswith("...")
    print(f"  [PASS] test_m28_api_endpoints_integration (snapshot_id={body2['snapshot_id'][:20]}...)")
