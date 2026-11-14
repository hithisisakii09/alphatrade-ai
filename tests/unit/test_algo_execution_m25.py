"""Unit tests for Milestone M25: Institutional Algorithmic Order Execution Slicer,
Microstructure Depth Tracker & Post-Trade TCA Implementation Shortfall Engine.

Covers:
  1. TWAP slice generation & exact integer share conservation (with residual reconciliation)
  2. Dynamic VWAP volume-curve slice generation & exact integer share conservation
  3. Microstructure metrics (Spread BPS, Order Book Imbalance OBI, Micro-Price)
  4. Microstructure liquidity gate deferral on spread widening
  5. Post-Trade TCA Implementation Shortfall decomposition (Market Impact, Delay, Opportunity, Fees)
  6. Algorithmic execution abort on excessive adverse price slippage
  7. AlgoExecutionCoordinator integration with PaperBrokerGateway
  8. M25 REST API endpoints integration test (/execution/algo/status, /execution/tca/summary)
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import sys

os.environ.setdefault("ALPHATRADE_EXECUTION_MODE", "PAPER")

from app.core.execution_guard import assert_paper_mode
from app.core.money import round_paise, round_price, to_decimal
from app.execution.algo_slicer import (
    AlgoExecutionCoordinator,
    ChildOrderSlice,
    DynamicVWAPExecutionEngine,
    ParentOrderRequest,
    TWAPExecutionEngine,
)
from app.execution.gateway import PaperBrokerGateway
from app.execution.tca import (
    ExecutionSliceFill,
    ImplementationShortfallBreakdown,
    compute_implementation_shortfall,
)
from app.market.microstructure import (
    MicrostructureLiquidityGate,
    MicrostructureSnapshot,
    OrderBookDepth,
    OrderBookLevel,
    analyze_order_book,
    compute_bid_ask_spread_bps,
    compute_micro_price,
    compute_order_book_imbalance,
)


# ---------------------------------------------------------------------------
# Test 1: TWAP slice generation & exact integer share conservation
# ---------------------------------------------------------------------------

def test_twap_slice_generation_and_exact_quantity_conservation():
  """Verifies TWAP distributes non-divisible parent quantities with exact remainder conservation."""
  now = datetime.now(timezone.utc)
  req = ParentOrderRequest(
      parent_order_id="TEST_TWAP_CONSERVE",
      symbol="RELIANCE",
      side="BUY",
      total_quantity=23,  # Non-divisible across 5 slices (4, 4, 4, 4, 4 + 3 remainder -> 5, 5, 5, 4, 4)
      start_time_utc=now,
      end_time_utc=now + timedelta(minutes=10),
      algo_type="TWAP",
      num_slices=5,
      arrival_price=Decimal("2850.00"),
  )
  twap = TWAPExecutionEngine(req)

  assert len(twap.slices) == 5, f"Expected 5 slices, got {len(twap.slices)}"
  quantities = [s.quantity for s in twap.slices]
  assert quantities == [5, 5, 5, 4, 4], f"Expected [5, 5, 5, 4, 4], got {quantities}"
  assert sum(quantities) == 23, "Total slice quantity MUST equal parent quantity exactly"

  # Verify scheduled timestamps are strictly monotonic
  times = [s.scheduled_time_utc for s in twap.slices]
  for i in range(1, len(times)):
    assert times[i] >= times[i - 1], "Slice scheduled times must be monotonically non-decreasing"


# ---------------------------------------------------------------------------
# Test 2: Dynamic VWAP volume-curve slice generation & exact conservation
# ---------------------------------------------------------------------------

def test_dynamic_vwap_slice_generation_and_exact_quantity_conservation():
  """Verifies Dynamic VWAP weights slices by canonical volume curve with exact integer conservation."""
  now = datetime.now(timezone.utc)
  req = ParentOrderRequest(
      parent_order_id="TEST_VWAP_CONSERVE",
      symbol="TCS",
      side="BUY",
      total_quantity=1000,
      start_time_utc=now,
      end_time_utc=now + timedelta(minutes=30),
      algo_type="VWAP",
      num_slices=5,
      arrival_price=Decimal("3800.00"),
  )
  vwap = DynamicVWAPExecutionEngine(req)

  assert len(vwap.slices) == 5
  quantities = [s.quantity for s in vwap.slices]
  assert sum(quantities) == 1000, f"Expected sum 1000, got {sum(quantities)}"

  # Canonical profile: [0.24, 0.18, 0.14, 0.16, 0.28]
  # Slices 1 and 5 should have higher allocation than slice 3 (midday trough)
  assert quantities[0] > quantities[2], "Opening slice must have greater weight than midday slice"
  assert quantities[4] > quantities[2], "Closing slice must have greater weight than midday slice"


# ---------------------------------------------------------------------------
# Test 3: Microstructure metrics (Spread BPS, OBI, Micro-Price)
# ---------------------------------------------------------------------------

def test_microstructure_metrics_calculation():
  """Verifies calculation of Spread BPS, Order Book Imbalance (OBI), and Micro-Price."""
  # Bid: 2850.00 (qty 500), Ask: 2851.00 (qty 300) -> Mid = 2850.50
  spread_inr, spread_bps = compute_bid_ask_spread_bps(Decimal("2850.00"), Decimal("2851.00"))
  assert spread_inr == Decimal("1.00")
  # 1.00 / 2850.50 * 10,000 = 3.51 bps
  assert spread_bps == Decimal("3.51")

  # Order Book Imbalance: Bid 1500 vs Ask 500 -> (1500 - 500) / 2000 = +0.5000 (Strong buy pressure)
  obi = compute_order_book_imbalance(bid_qty=1500, ask_qty=500)
  assert obi == Decimal("0.5000")

  # Micro-Price: pulls toward the ask when bids dominate liquidity
  # (2850.00 * 300 + 2851.00 * 500) / 800 = (855000 + 1425500) / 800 = 2280500 / 800 = 2850.625 -> 2850.65
  micro_p = compute_micro_price(
      best_bid=Decimal("2850.00"),
      best_ask=Decimal("2851.00"),
      bid_qty=500,
      ask_qty=300,
  )
  assert micro_p > Decimal("2850.50"), "Micro-price must be higher than mid-price when bid depth > ask depth"
  assert micro_p == Decimal("2850.6250")


# ---------------------------------------------------------------------------
# Test 4: Microstructure liquidity gate deferral on spread widening
# ---------------------------------------------------------------------------

def test_microstructure_liquidity_gate_deferral_on_spread_widening():
  """Verifies liquidity gate halts execution when spread exceeds 3x baseline tolerance."""
  gate = MicrostructureLiquidityGate(
      baseline_spread_bps=Decimal("5.00"),
      max_spread_multiplier=Decimal("3.00"),  # 15.00 bps max
      min_top_depth_shares=50,
  )
  now = datetime.now(timezone.utc)

  # Scenario A: Normal liquid book (spread 3.5 bps < 15.0 bps, ask depth 300 >= 50) -> APPROVED
  normal_book = OrderBookDepth(
      symbol="RELIANCE",
      timestamp_utc=now,
      bids=[OrderBookLevel(price=Decimal("2850.00"), quantity=500)],
      asks=[OrderBookLevel(price=Decimal("2851.00"), quantity=300)],
  )
  snap_normal = analyze_order_book(normal_book)
  approved, reason, details = gate.evaluate_slice(snap_normal, slice_quantity=100, side="BUY")
  assert approved is True
  assert reason == "LIQUIDITY_APPROVED"

  # Scenario B: Stressed wide spread (spread 20.0 bps > 15.0 bps) -> DEFERRED
  stressed_book = OrderBookDepth(
      symbol="RELIANCE",
      timestamp_utc=now,
      bids=[OrderBookLevel(price=Decimal("2845.00"), quantity=500)],
      asks=[OrderBookLevel(price=Decimal("2852.00"), quantity=300)],
  )
  snap_stressed = analyze_order_book(stressed_book)
  approved_b, reason_b, details_b = gate.evaluate_slice(snap_stressed, slice_quantity=100, side="BUY")
  assert approved_b is False
  assert reason_b == "SPREAD_WIDENING_DEFERRAL"


# ---------------------------------------------------------------------------
# Test 5: Post-Trade TCA Implementation Shortfall decomposition
# ---------------------------------------------------------------------------

def test_tca_implementation_shortfall_decomposition():
  """Verifies Perold (1988) Implementation Shortfall component mathematical identities."""
  now = datetime.now(timezone.utc)
  fills = [
      ExecutionSliceFill("S01", quantity=500, price=Decimal("1003.00"), timestamp_utc=now, fee_inr=Decimal("10.00")),
      ExecutionSliceFill("S02", quantity=300, price=Decimal("1005.00"), timestamp_utc=now, fee_inr=Decimal("10.00")),
  ]
  # Decision = 1000.00, Arrival = 1002.00, Total Ordered = 1000
  # Total Filled = 800, Avg Exec = (500*1003 + 300*1005) / 800 = (501500 + 301500) / 800 = 803000 / 800 = 1003.75
  # Unfilled = 200, Final Market Price = 1008.00
  rep = compute_implementation_shortfall(
      symbol="TCS",
      side="BUY",
      total_ordered_quantity=1000,
      decision_price=Decimal("1000.00"),
      arrival_price=Decimal("1002.00"),
      fills=fills,
      final_market_price=Decimal("1008.00"),
      interval_vwap=Decimal("1004.00"),
      interval_twap=Decimal("1004.50"),
  )

  assert rep.total_filled_quantity == 800
  assert rep.unfilled_quantity == 200
  assert rep.average_execution_price == Decimal("1003.75")

  # Market Impact: (1003.75 - 1002.00) * 800 = 1400.00 INR
  assert rep.market_impact_inr == Decimal("1400.00")
  # Delay Cost: (1003.00 - 1000.00) * 800 = 2400.00 INR
  assert rep.delay_cost_inr == Decimal("2400.00")
  # Opportunity Cost: (1008.00 - 1000.00) * 200 = 1600.00 INR
  assert rep.opportunity_cost_inr == Decimal("1600.00")
  # Total Fees: 20.00 INR
  assert rep.total_fees_inr == Decimal("20.00")

  # Total Shortfall INR: (1003.75 - 1000.00)*800 + 1600.00 + 20.00 = 3000.00 + 1600.00 + 20.00 = 4620.00 INR
  assert rep.total_shortfall_inr == Decimal("4620.00")

  # Benchmark comparisons:
  # Executed 1003.75 vs VWAP 1004.00 -> outperforming VWAP (bought cheaper!) -> negative bps
  assert rep.vwap_shortfall_bps < Decimal("0.00")
  assert rep.twap_shortfall_bps < Decimal("0.00")


# ---------------------------------------------------------------------------
# Test 6: Algorithmic execution abort on excessive adverse price slippage
# ---------------------------------------------------------------------------

def test_algo_slicer_aborts_on_excessive_adverse_slippage():
  """Verifies algorithmic order aborts when market price moves beyond max_slippage_bps."""
  now = datetime.now(timezone.utc)
  req = ParentOrderRequest(
      parent_order_id="TEST_ABORT_SLIP",
      symbol="INFY",
      side="BUY",
      total_quantity=500,
      start_time_utc=now,
      end_time_utc=now + timedelta(minutes=15),
      algo_type="TWAP",
      num_slices=5,
      arrival_price=Decimal("1500.00"),
      max_slippage_bps=Decimal("30.00"),  # 30 bps = 4.50 INR -> Max acceptable price = 1504.50
  )
  twap = TWAPExecutionEngine(req)
  assert twap.status == "ACTIVE"

  # Normal price movement (1502.00 -> +13.3 bps <= 30 bps) -> No abort
  aborted, reason = twap.check_abort_condition(Decimal("1502.00"))
  assert aborted is False
  assert twap.status == "ACTIVE"

  # Extreme price breakout jump (1506.00 -> +40.0 bps > 30 bps) -> Abort triggered!
  aborted2, reason2 = twap.check_abort_condition(Decimal("1506.00"))
  assert aborted2 is True
  assert twap.status == "ABORTED"
  assert "MAX_SLIPPAGE_EXCEEDED" in twap.abort_reason


# ---------------------------------------------------------------------------
# Test 7: AlgoExecutionCoordinator integration with PaperBrokerGateway
# ---------------------------------------------------------------------------

def test_algo_execution_coordinator_with_broker_gateway():
  """Verifies end-to-end slice routing through AlgoExecutionCoordinator and BrokerGateway."""
  now = datetime.now(timezone.utc)
  gateway = PaperBrokerGateway(name="PAPER_INTERNAL")

  coordinator = AlgoExecutionCoordinator()
  req = ParentOrderRequest(
      parent_order_id="TEST_COORD_001",
      symbol="SBIN",
      side="BUY",
      total_quantity=200,
      start_time_utc=now,
      end_time_utc=now + timedelta(minutes=10),
      algo_type="TWAP",
      num_slices=2,
      arrival_price=Decimal("600.00"),
  )
  twap = TWAPExecutionEngine(req)
  coordinator.register_algorithm(twap)

  book = OrderBookDepth(
      symbol="SBIN",
      timestamp_utc=now,
      bids=[OrderBookLevel(price=Decimal("599.80"), quantity=1000)],
      asks=[OrderBookLevel(price=Decimal("600.20"), quantity=800)],
  )

  # Execute slice 1 (100 shares)
  resp1 = coordinator.execute_slice(
      parent_order_id="TEST_COORD_001",
      gateway=gateway,
      book_depth=book,
      current_price=Decimal("600.00"),
  )
  assert resp1 is not None
  assert resp1.status == "FILLED"
  assert resp1.filled_quantity == 100
  assert twap.slices[0].status == "FILLED"
  assert twap.status == "ACTIVE"

  # Execute slice 2 (100 shares) -> completion
  resp2 = coordinator.execute_slice(
      parent_order_id="TEST_COORD_001",
      gateway=gateway,
      book_depth=book,
      current_price=Decimal("600.10"),
  )
  assert resp2 is not None
  assert resp2.status == "FILLED"
  assert twap.slices[1].status == "FILLED"
  assert twap.status == "COMPLETED"


# ---------------------------------------------------------------------------
# Test 8: M25 REST API endpoints integration test
# ---------------------------------------------------------------------------

def test_m25_api_endpoints_integration():
  """Smoke-tests /execution/algo/status and /execution/tca/summary endpoints."""
  from fastapi.testclient import TestClient
  from app.main import app

  client = TestClient(app)

  # --- Algo status endpoint ---
  r1 = client.get("/execution/algo/status")
  assert r1.status_code == 200, f"Algo status returned {r1.status_code}: {r1.text}"
  data1 = r1.json()
  assert "active_algorithms_count" in data1
  assert "algorithms" in data1
  assert len(data1["algorithms"]) >= 1

  # --- TCA summary endpoint ---
  r2 = client.get("/execution/tca/summary")
  assert r2.status_code == 200, f"TCA summary returned {r2.status_code}: {r2.text}"
  data2 = r2.json()
  assert "total_tca_reports" in data2
  assert "reports" in data2
  assert len(data2["reports"]) >= 1
  first_rep = data2["reports"][0]
  assert "market_impact_inr" in first_rep
  assert "vwap_shortfall_bps" in first_rep
