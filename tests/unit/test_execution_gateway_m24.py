"""Unit tests for M24: Multi-Broker Execution Gateway, SOR & Real-Time Shadow Reconciler.

Covers:
  1. PaperBrokerGateway order placement (FILLED status, quantity match)
  2. PaperBrokerGateway cancel_order on FILLED (False) and SUBMITTED (True)
  3. MockAngelSmartAPIGateway position tracking after BUY
  4. SmartOrderRouter latency-based venue selection (PAPER 0.25ms vs ANGEL 12.5ms)
  5. SmartOrderRouter preferred_gateway override
  6. RealTimeShadowReconciler healthy state (zero discrepancies)
  7. RealTimeShadowReconciler orphan position detection & kill-switch
  8. M24 REST API endpoints integration test
"""

import os
from decimal import Decimal
from datetime import datetime, timezone

os.environ.setdefault("ALPHATRADE_EXECUTION_MODE", "PAPER")

from app.execution.gateway import (
    BrokerOrderRequest,
    BrokerOrderResponse,
    BrokerPosition,
    PaperBrokerGateway,
    MockAngelSmartAPIGateway,
    SmartOrderRouter,
)
from app.execution.shadow_reconciler import RealTimeShadowReconciler
from app.risk.engine import RiskEngine
from app.execution.costs import NSECostEngine
from app.core.config import load_app_config as load_config


def _make_risk_engine() -> RiskEngine:
    """Helper: build a minimal RiskEngine from loaded config."""
    cfg = load_config()
    cost_engine = NSECostEngine(profile=cfg.costs[cfg.active_cost_profile])
    profile = cfg.strategy.risk_profiles[cfg.active_risk_profile]
    return RiskEngine(profile=profile, cost_engine=cost_engine)


# ---------------------------------------------------------------------------
# Test 1: PaperBrokerGateway order placement
# ---------------------------------------------------------------------------

def test_paper_broker_gateway_order_placement():
    """Place a BUY order via PaperBrokerGateway; verify FILLED status and quantity."""
    gw = PaperBrokerGateway(paper_broker=None, ledger=None, name="PAPER_INTERNAL")
    assert gw.is_connected(), "Gateway must be connected after init"

    req = BrokerOrderRequest(
        symbol="RELIANCE",
        side="BUY",
        quantity=5,
        order_type="MARKET",
        price=Decimal("2800.00"),
    )
    resp = gw.place_order(req)

    assert resp.status == "FILLED", f"Expected FILLED, got {resp.status}"
    assert resp.filled_quantity == 5, f"Expected 5 filled, got {resp.filled_quantity}"
    assert resp.symbol == "RELIANCE"
    assert resp.average_price == Decimal("2800.00")
    assert resp.rejection_reason is None
    assert resp.broker_order_id.startswith("PB_ORD_")

    # Verify order retrievable via get_order_status
    retrieved = gw.get_order_status(resp.broker_order_id)
    assert retrieved is not None
    assert retrieved.status == "FILLED"


# ---------------------------------------------------------------------------
# Test 2: PaperBrokerGateway cancel_order
# ---------------------------------------------------------------------------

def test_paper_broker_gateway_cancel_order():
    """
    Cancel attempt on a FILLED order returns False.
    Cancel attempt on a SUBMITTED order returns True and marks CANCELLED.
    """
    gw = PaperBrokerGateway(paper_broker=None, ledger=None, name="PAPER_CANCEL_TEST")

    # Place an order -> FILLED
    req = BrokerOrderRequest(symbol="INFY", side="BUY", quantity=10, price=Decimal("1500.00"))
    resp = gw.place_order(req)
    assert resp.status == "FILLED"

    # Cancelling a FILLED order must return False
    result = gw.cancel_order(resp.broker_order_id)
    assert result is False, "Cannot cancel an already-FILLED order"

    # Inject a synthetic SUBMITTED order directly into internal dict
    submitted_id = "PB_ORD_FAKE_SUBMITTED_001"
    fake_submitted = BrokerOrderResponse(
        broker_order_id=submitted_id,
        client_order_id="CL_FAKE_001",
        symbol="INFY",
        side="BUY",
        quantity=10,
        filled_quantity=0,
        average_price=Decimal("0.00"),
        status="SUBMITTED",
        rejection_reason=None,
        timestamp_utc=datetime.now(timezone.utc),
    )
    gw._orders[submitted_id] = fake_submitted

    # Cancelling a SUBMITTED order must return True
    result2 = gw.cancel_order(submitted_id)
    assert result2 is True, "Must be able to cancel a SUBMITTED order"
    cancelled = gw.get_order_status(submitted_id)
    assert cancelled.status == "CANCELLED"
    assert cancelled.filled_quantity == 0


# ---------------------------------------------------------------------------
# Test 3: MockAngelSmartAPIGateway position tracking
# ---------------------------------------------------------------------------

def test_mock_angel_gateway_position_tracking():
    """Place a BUY via MockAngel; verify get_positions() tracks it correctly."""
    gw = MockAngelSmartAPIGateway(name="ANGEL_SMARTAPI_MOCK")
    assert gw.is_connected()

    req = BrokerOrderRequest(
        symbol="TCS",
        side="BUY",
        quantity=3,
        price=Decimal("3900.00"),
    )
    resp = gw.place_order(req)
    assert resp.status == "FILLED"
    assert resp.filled_quantity == 3

    positions = gw.get_positions()
    syms = [p.symbol for p in positions]
    assert "TCS" in syms, f"TCS position not tracked; got {syms}"

    tcs_pos = next(p for p in positions if p.symbol == "TCS")
    assert tcs_pos.quantity == 3
    assert tcs_pos.average_price == Decimal("3900.00")

    # Margins should reflect used margin (20% MIS of notional)
    margins = gw.get_margins()
    expected_margin = (Decimal("3") * Decimal("3900.00") * Decimal("0.20")).quantize(Decimal("0.01"))
    assert margins.used_margin == expected_margin


# ---------------------------------------------------------------------------
# Test 4: SmartOrderRouter selects lowest-latency gateway
# ---------------------------------------------------------------------------

def test_smart_order_router_selects_lowest_latency_gateway():
    """With PAPER (0.25 ms) and ANGEL (12.5 ms) registered, SOR must route to PAPER."""
    paper_gw = PaperBrokerGateway(name="PAPER_INTERNAL")
    angel_gw = MockAngelSmartAPIGateway(name="ANGEL_SMARTAPI_MOCK")

    sor = SmartOrderRouter(default_gateway="PAPER_INTERNAL")
    sor.register_gateway(paper_gw)
    sor.register_gateway(angel_gw)

    req = BrokerOrderRequest(symbol="HDFC", side="BUY", quantity=2, price=Decimal("1700.00"))
    resp = sor.route_order(req)

    # PAPER has latency 0.25 ms < ANGEL 12.5 ms → must route to PAPER
    assert resp.broker_order_id.startswith("PB_ORD_"), (
        f"Expected PaperBroker order (PB_ORD_ prefix), got {resp.broker_order_id}"
    )
    assert resp.status == "FILLED"
    assert resp.symbol == "HDFC"


# ---------------------------------------------------------------------------
# Test 5: SmartOrderRouter preferred_gateway override
# ---------------------------------------------------------------------------

def test_smart_order_router_prefers_requested_gateway():
    """With preferred_gateway='ANGEL_SMARTAPI_MOCK', SOR must use ANGEL even when PAPER is default."""
    paper_gw = PaperBrokerGateway(name="PAPER_INTERNAL")
    angel_gw = MockAngelSmartAPIGateway(name="ANGEL_SMARTAPI_MOCK")

    sor = SmartOrderRouter(default_gateway="PAPER_INTERNAL")
    sor.register_gateway(paper_gw)
    sor.register_gateway(angel_gw)

    req = BrokerOrderRequest(symbol="SBIN", side="BUY", quantity=4, price=Decimal("580.00"))
    resp = sor.route_order(req, preferred_gateway="ANGEL_SMARTAPI_MOCK")

    assert resp.broker_order_id.startswith("ANGEL_"), (
        f"Expected ANGEL order prefix, got {resp.broker_order_id}"
    )
    assert resp.status == "FILLED"
    assert resp.filled_quantity == 4


# ---------------------------------------------------------------------------
# Test 6: RealTimeShadowReconciler healthy state
# ---------------------------------------------------------------------------

def test_shadow_reconciler_healthy_state():
    """Matching internal positions with broker state produces HEALTHY report with 0 discrepancies."""
    risk_engine = _make_risk_engine()

    # Fresh MockAngel gateway with no positions
    broker_gw = MockAngelSmartAPIGateway(name="ANGEL_SMARTAPI_MOCK_HEALTHY")

    reconciler = RealTimeShadowReconciler(
        gateway=broker_gw,
        risk_engine=risk_engine,
    )

    # No internal positions, no broker positions → HEALTHY
    report = reconciler.reconcile(
        explicit_internal_positions={},
        explicit_internal_margin=Decimal("0.00"),
    )

    assert report.status == "HEALTHY", f"Expected HEALTHY, got {report.status}"
    assert report.discrepancies_count == 0
    assert report.kill_switch_triggered is False
    assert report.internal_positions_count == 0
    assert report.broker_positions_count == 0

    d = report.to_dict()
    assert d["status"] == "HEALTHY"
    assert d["discrepancies_count"] == 0
    assert "audit_id" in d


# ---------------------------------------------------------------------------
# Test 7: RealTimeShadowReconciler orphan position detection & kill-switch
# ---------------------------------------------------------------------------

def test_shadow_reconciler_detects_orphan_position_and_trips_kill_switch():
    """
    When broker reports a SBIN position that internal ledger doesn't know about,
    reconciler must flag ORPHAN_BROKER_POSITION (CRITICAL) and trip the kill-switch.
    """
    risk_engine = _make_risk_engine()
    risk_engine.reset_kill_switch()          # Ensure kill-switch starts clear
    assert risk_engine.kill_switch_active is False

    broker_gw = MockAngelSmartAPIGateway(name="ANGEL_SMARTAPI_MOCK_ORPHAN")

    # Inject an orphan position that internal ledger doesn't know about
    broker_gw.inject_mock_position(BrokerPosition(
        symbol="SBIN",
        quantity=10,
        average_price=Decimal("580.00"),
        current_price=Decimal("582.00"),
        unrealized_pnl=Decimal("20.00"),
    ))

    reconciler = RealTimeShadowReconciler(
        gateway=broker_gw,
        risk_engine=risk_engine,
    )

    report = reconciler.reconcile(
        explicit_internal_positions={},      # Internal knows nothing of SBIN
        explicit_internal_margin=Decimal("0.00"),
    )

    assert report.status == "DISCREPANCY_DETECTED", (
        f"Expected DISCREPANCY_DETECTED, got {report.status}"
    )
    assert report.discrepancies_count >= 1
    assert report.kill_switch_triggered is True

    types = [d.discrepancy_type for d in report.discrepancies]
    assert "ORPHAN_BROKER_POSITION" in types, f"Expected ORPHAN_BROKER_POSITION in {types}"

    critical = [d for d in report.discrepancies if d.severity == "CRITICAL"]
    assert len(critical) >= 1

    # RiskEngine kill-switch must be tripped
    assert risk_engine.kill_switch_active is True, "Kill-switch must be tripped on CRITICAL discrepancy"


# ---------------------------------------------------------------------------
# Test 8: M24 REST API endpoints integration test
# ---------------------------------------------------------------------------

def test_m24_api_endpoints_integration():
    """
    Smoke-test both M24 REST endpoints via TestClient(app):
      GET /execution/gateway/status       → 200 with expected keys
      GET /execution/reconciliation/status → 200 with expected keys
    """
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    # --- Gateway status endpoint ---
    r1 = client.get("/execution/gateway/status")
    assert r1.status_code == 200, f"Gateway status returned {r1.status_code}: {r1.text}"
    data1 = r1.json()
    # Endpoint returns: default_gateway, gateways_count, gateways
    assert "gateways" in data1, f"Missing 'gateways' key in {data1}"
    assert "default_gateway" in data1, f"Missing 'default_gateway' in {data1}"
    assert "gateways_count" in data1, f"Missing 'gateways_count' in {data1}"

    # --- Reconciliation status endpoint ---
    r2 = client.get("/execution/reconciliation/status")
    assert r2.status_code == 200, f"Reconciliation status returned {r2.status_code}: {r2.text}"
    data2 = r2.json()
    # Endpoint returns report.to_dict(): audit_id, status, discrepancies_count, timestamp_utc, ...
    assert "status" in data2, f"Missing 'status' key in {data2}"
    assert "discrepancies_count" in data2, f"Missing 'discrepancies_count' in {data2}"
    assert "audit_id" in data2, f"Missing 'audit_id' in {data2}"
