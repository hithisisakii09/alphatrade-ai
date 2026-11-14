"""Unit tests for Milestone M26: Real-Time Level-2 WebSocket Streaming,
Market-by-Price (MBP) Book Reconstruction & Sub-Second Tick Breakout Engine.

Covers:
  1. Binary packet encoder & decoder parity (zero float drift, round-trip fidelity)
  2. SnapQuote Mode 3 Level-2 depth ladder binary parsing & buffer size validation
  3. LiveOrderBook depth update, sorting, thread-safety & Microstructure snapshot extraction
  4. StreamingFeedHandler tick dispatch, callback invocation & latency telemetry
  5. StreamingTickBreakoutDetector fires immediately on ORH cross
  6. StreamingTickBreakoutDetector rejects breakout on wide bid-ask spread (MicrostructureLiquidityGate)
  7. StreamingTickBreakoutDetector rejects breakout in adverse volatility regime (BEAR)
  8. M26 REST API endpoints integration test (/market/streaming/status & /market/depth/{symbol})
"""

from datetime import datetime, timezone
from decimal import Decimal
import os
import sys

os.environ.setdefault("ALPHATRADE_EXECUTION_MODE", "PAPER")

from app.core.execution_guard import assert_paper_mode
from app.core.money import round_price, to_decimal
from app.data.streaming import (
    BinaryPacketParser,
    OrderBookLevel,
    LiveOrderBook,
    StreamingFeedHandler,
    StreamingTickPacket,
    SUBSCRIPTION_MODE_LTP,
    SUBSCRIPTION_MODE_QUOTE,
    SUBSCRIPTION_MODE_SNAPQUOTE,
    EXCHANGE_NSE_CM,
)
from app.market.microstructure import (
    MicrostructureLiquidityGate,
    OrderBookDepth,
    analyze_order_book,
)
from app.strategy.orb.tick_detector import (
    OpeningRangeLevels,
    StreamingBreakoutSignal,
    StreamingTickBreakoutDetector,
)


# ---------------------------------------------------------------------------
# Test 1: Binary packet encoder & decoder round-trip parity
# ---------------------------------------------------------------------------

def test_binary_packet_encoder_and_decoder_parity():
  """Verifies little-endian binary serialization preserves pure Decimal precision."""
  assert_paper_mode()
  now_utc = datetime(2026, 11, 14, 5, 0, 0, tzinfo=timezone.utc)
  bids = [
      OrderBookLevel(price=Decimal("2850.50"), quantity=100, orders_count=2),
      OrderBookLevel(price=Decimal("2850.00"), quantity=250, orders_count=5),
  ]
  asks = [
      OrderBookLevel(price=Decimal("2851.00"), quantity=150, orders_count=3),
      OrderBookLevel(price=Decimal("2851.50"), quantity=300, orders_count=7),
  ]

  original_packet = StreamingTickPacket(
      subscription_mode=SUBSCRIPTION_MODE_SNAPQUOTE,
      exchange_type=EXCHANGE_NSE_CM,
      token="2885",
      sequence_number=1042,
      exchange_timestamp_utc=now_utc,
      last_traded_price=Decimal("2850.75"),
      last_traded_quantity=25,
      average_traded_price=Decimal("2849.30"),
      volume_traded_today=450200,
      total_buy_quantity=60000,
      total_sell_quantity=55000,
      open_price=Decimal("2840.00"),
      high_price=Decimal("2855.00"),
      low_price=Decimal("2838.00"),
      close_price=Decimal("2845.00"),
      bids=bids,
      asks=asks,
  )

  encoded_bytes = BinaryPacketParser.encode_packet(original_packet)
  assert len(encoded_bytes) == (BinaryPacketParser.HEADER_SIZE + (10 * BinaryPacketParser.DEPTH_ENTRY_SIZE))

  decoded_packet = BinaryPacketParser.parse_packet(encoded_bytes)
  assert decoded_packet.token == "2885"
  assert decoded_packet.sequence_number == 1042
  assert decoded_packet.last_traded_price == Decimal("2850.75")
  assert decoded_packet.last_traded_quantity == 25
  assert decoded_packet.average_traded_price == Decimal("2849.30")
  assert decoded_packet.volume_traded_today == 450200
  assert len(decoded_packet.bids) == 2
  assert decoded_packet.bids[0].price == Decimal("2850.50")
  assert decoded_packet.bids[0].quantity == 100
  assert len(decoded_packet.asks) == 2
  assert decoded_packet.asks[0].price == Decimal("2851.00")
  assert decoded_packet.asks[0].quantity == 150


# ---------------------------------------------------------------------------
# Test 2: SnapQuote Mode 3 Level-2 depth ladder binary parsing & buffer size
# ---------------------------------------------------------------------------

def test_snapquote_mode3_depth_parsing():
  """Verifies 5-depth order book parsing and short-buffer exception handling."""
  assert_paper_mode()

  # 1. Truncated buffer check
  short_buffer = b"\x00" * 50
  try:
    BinaryPacketParser.parse_packet(short_buffer)
    assert False, "Expected ValueError for short buffer"
  except ValueError as e:
    assert "Buffer too short" in str(e)

  # 2. Valid full 5-bid 5-ask ladder
  now_utc = datetime(2026, 11, 14, 5, 0, 0, tzinfo=timezone.utc)
  bids = [
      OrderBookLevel(price=round_price(Decimal("3800.00") - Decimal(str(i * 0.50))), quantity=100 * (i + 1), orders_count=i + 1)
      for i in range(5)
  ]
  asks = [
      OrderBookLevel(price=round_price(Decimal("3801.00") + Decimal(str(i * 0.50))), quantity=80 * (i + 1), orders_count=i + 1)
      for i in range(5)
  ]
  packet = StreamingTickPacket(
      subscription_mode=SUBSCRIPTION_MODE_SNAPQUOTE,
      exchange_type=EXCHANGE_NSE_CM,
      token="11536",
      sequence_number=501,
      exchange_timestamp_utc=now_utc,
      last_traded_price=Decimal("3800.50"),
      last_traded_quantity=10,
      average_traded_price=Decimal("3798.20"),
      volume_traded_today=120000,
      total_buy_quantity=25000,
      total_sell_quantity=24000,
      open_price=Decimal("3780.00"),
      high_price=Decimal("3805.00"),
      low_price=Decimal("3775.00"),
      close_price=Decimal("3790.00"),
      bids=bids,
      asks=asks,
  )

  raw = BinaryPacketParser.encode_packet(packet)
  decoded = BinaryPacketParser.parse_packet(raw)

  assert len(decoded.bids) == 5
  assert len(decoded.asks) == 5
  assert decoded.bids[0].price == Decimal("3800.00")
  assert decoded.asks[0].price == Decimal("3801.00")
  assert decoded.bids[4].price == Decimal("3798.00")
  assert decoded.asks[4].price == Decimal("3803.00")


# ---------------------------------------------------------------------------
# Test 3: LiveOrderBook depth update, sorting & Microstructure snapshot
# ---------------------------------------------------------------------------

def test_live_order_book_depth_update_and_snapshot():
  """Verifies LiveOrderBook sorts bid/ask ladders and yields compatible OrderBookDepth."""
  assert_paper_mode()
  book = LiveOrderBook(symbol="INFY", token="1594")
  now_utc = datetime(2026, 11, 14, 5, 10, 0, tzinfo=timezone.utc)

  unsorted_bids = [
      OrderBookLevel(price=Decimal("1849.00"), quantity=200, orders_count=3),
      OrderBookLevel(price=Decimal("1850.00"), quantity=500, orders_count=6),  # Best bid
      OrderBookLevel(price=Decimal("1848.50"), quantity=100, orders_count=1),
  ]
  unsorted_asks = [
      OrderBookLevel(price=Decimal("1851.50"), quantity=300, orders_count=4),
      OrderBookLevel(price=Decimal("1850.50"), quantity=400, orders_count=5),  # Best ask
      OrderBookLevel(price=Decimal("1852.00"), quantity=150, orders_count=2),
  ]

  packet = StreamingTickPacket(
      subscription_mode=SUBSCRIPTION_MODE_SNAPQUOTE,
      exchange_type=EXCHANGE_NSE_CM,
      token="1594",
      sequence_number=77,
      exchange_timestamp_utc=now_utc,
      last_traded_price=Decimal("1850.25"),
      last_traded_quantity=50,
      average_traded_price=Decimal("1848.00"),
      volume_traded_today=890000,
      total_buy_quantity=50000,
      total_sell_quantity=48000,
      open_price=Decimal("1840.00"),
      high_price=Decimal("1855.00"),
      low_price=Decimal("1835.00"),
      close_price=Decimal("1842.00"),
      bids=unsorted_bids,
      asks=unsorted_asks,
  )

  book.update_from_tick(packet)

  snapshot = book.get_snapshot()
  assert snapshot.symbol == "INFY"
  assert snapshot.bids[0].price == Decimal("1850.00")  # Descending
  assert snapshot.asks[0].price == Decimal("1850.50")  # Ascending

  micro = analyze_order_book(snapshot)
  assert micro.best_bid == Decimal("1850.00")
  assert micro.best_ask == Decimal("1850.50")
  assert micro.spread_inr == Decimal("0.50")
  assert micro.mid_price == Decimal("1850.25")
  assert micro.spread_bps > Decimal("0.00")


# ---------------------------------------------------------------------------
# Test 4: StreamingFeedHandler tick dispatch, callback & telemetry
# ---------------------------------------------------------------------------

def test_streaming_feed_handler_tick_dispatch_and_metrics():
  """Verifies StreamingFeedHandler dispatches events to registered callbacks and profiles latency."""
  assert_paper_mode()
  handler = StreamingFeedHandler()
  handler.register_instrument("RELIANCE", "2885")

  received_packets: list[StreamingTickPacket] = []
  received_depths: list[OrderBookDepth] = []

  def _test_cb(pkt: StreamingTickPacket, depth: OrderBookDepth):
    received_packets.append(pkt)
    received_depths.append(depth)

  handler.add_tick_callback(_test_cb)

  now_utc = datetime(2026, 11, 14, 5, 15, 0, tzinfo=timezone.utc)
  packet = StreamingTickPacket(
      subscription_mode=SUBSCRIPTION_MODE_SNAPQUOTE,
      exchange_type=EXCHANGE_NSE_CM,
      token="2885",
      sequence_number=999,
      exchange_timestamp_utc=now_utc,
      last_traded_price=Decimal("2852.00"),
      last_traded_quantity=10,
      average_traded_price=Decimal("2850.00"),
      volume_traded_today=150000,
      total_buy_quantity=10000,
      total_sell_quantity=12000,
      open_price=Decimal("2845.00"),
      high_price=Decimal("2855.00"),
      low_price=Decimal("2840.00"),
      close_price=Decimal("2848.00"),
      bids=[OrderBookLevel(price=Decimal("2851.50"), quantity=100, orders_count=1)],
      asks=[OrderBookLevel(price=Decimal("2852.50"), quantity=100, orders_count=1)],
  )

  raw_bytes = BinaryPacketParser.encode_packet(packet)
  handler.handle_binary_frame(raw_bytes)

  assert len(received_packets) == 1
  assert len(received_depths) == 1
  assert received_packets[0].token == "2885"
  assert received_depths[0].symbol == "RELIANCE"

  telem = handler.get_telemetry()
  assert telem["is_connected"] is True
  assert telem["total_frames_processed"] == 1
  assert telem["subscribed_instruments_count"] == 1
  assert telem["latency_p95_ms"] >= 0.0


# ---------------------------------------------------------------------------
# Test 5: StreamingTickBreakoutDetector fires on ORH cross
# ---------------------------------------------------------------------------

def test_streaming_tick_breakout_detector_fires_on_orh_cross():
  """Verifies detector emits a StreamingBreakoutSignal immediately when tick exceeds ORH within entry window."""
  assert_paper_mode()
  detector = StreamingTickBreakoutDetector(
      entry_start_ist="09:30:00",
      entry_cutoff_ist="14:30:00",
  )
  detector.register_opening_range(
      OpeningRangeLevels(
          symbol="RELIANCE",
          session_date="2026-11-14",
          orh=Decimal("2855.00"),
          orl=Decimal("2835.00"),
          vwap=Decimal("2845.00"),
          is_formed=True,
      )
  )
  detector.set_regime("RELIANCE", "BULL")

  # Tick 1: Inside entry window (10:00 AM IST = 04:30 UTC), but price 2854.00 <= ORH
  t_utc = datetime(2026, 11, 14, 4, 30, 0, tzinfo=timezone.utc)
  depth = OrderBookDepth(
      symbol="RELIANCE",
      timestamp_utc=t_utc,
      bids=[OrderBookLevel(price=Decimal("2853.50"), quantity=500, orders_count=5)],
      asks=[OrderBookLevel(price=Decimal("2854.00"), quantity=500, orders_count=5)],
  )
  packet1 = StreamingTickPacket(
      subscription_mode=SUBSCRIPTION_MODE_SNAPQUOTE,
      exchange_type=EXCHANGE_NSE_CM,
      token="2885",
      sequence_number=1,
      exchange_timestamp_utc=t_utc,
      last_traded_price=Decimal("2854.00"),
      last_traded_quantity=50,
      average_traded_price=Decimal("2850.00"),
      volume_traded_today=200000,
      total_buy_quantity=50000,
      total_sell_quantity=50000,
      open_price=Decimal("2840.00"),
      high_price=Decimal("2854.00"),
      low_price=Decimal("2835.00"),
      close_price=Decimal("2850.00"),
      bids=depth.bids,
      asks=depth.asks,
  )

  sig1, reason1 = detector.on_tick(packet1, depth)
  assert sig1 is None
  assert reason1 == "PRICE_BELOW_ORH"

  # Tick 2: Price crosses ORH -> 2856.00 > 2855.00
  depth2 = OrderBookDepth(
      symbol="RELIANCE",
      timestamp_utc=t_utc,
      bids=[OrderBookLevel(price=Decimal("2855.50"), quantity=500, orders_count=5)],
      asks=[OrderBookLevel(price=Decimal("2856.00"), quantity=500, orders_count=5)],
  )
  packet2 = StreamingTickPacket(
      subscription_mode=SUBSCRIPTION_MODE_SNAPQUOTE,
      exchange_type=EXCHANGE_NSE_CM,
      token="2885",
      sequence_number=2,
      exchange_timestamp_utc=t_utc,
      last_traded_price=Decimal("2856.00"),
      last_traded_quantity=100,
      average_traded_price=Decimal("2851.00"),
      volume_traded_today=201000,
      total_buy_quantity=50000,
      total_sell_quantity=50000,
      open_price=Decimal("2840.00"),
      high_price=Decimal("2856.00"),
      low_price=Decimal("2835.00"),
      close_price=Decimal("2855.00"),
      bids=depth2.bids,
      asks=depth2.asks,
  )

  sig2, reason2 = detector.on_tick(packet2, depth2)
  assert sig2 is not None
  assert reason2 == "BREAKOUT_TRIGGERED"
  assert sig2.symbol == "RELIANCE"
  assert sig2.trigger_price == Decimal("2856.00")
  assert sig2.orh == Decimal("2855.00")
  assert sig2.latency_ms >= 0.0

  # Tick 3: Second breakout same day -> rejected (1 entry per symbol per day)
  sig3, reason3 = detector.on_tick(packet2, depth2)
  assert sig3 is None
  assert reason3 == "ALREADY_TRIGGERED_TODAY"


# ---------------------------------------------------------------------------
# Test 6: StreamingTickBreakoutDetector rejects wide bid-ask spread
# ---------------------------------------------------------------------------

def test_streaming_tick_breakout_detector_rejects_wide_spread():
  """Verifies detector rejects trigger if bid-ask spread breaches MicrostructureLiquidityGate."""
  assert_paper_mode()
  detector = StreamingTickBreakoutDetector(
      liquidity_gate=MicrostructureLiquidityGate(baseline_spread_bps=Decimal("5.00")),
      entry_start_ist="09:30:00",
      entry_cutoff_ist="14:30:00",
  )
  detector.register_opening_range(
      OpeningRangeLevels(
          symbol="TCS",
          session_date="2026-11-14",
          orh=Decimal("3800.00"),
          orl=Decimal("3780.00"),
          vwap=Decimal("3790.00"),
          is_formed=True,
      )
  )
  detector.set_regime("TCS", "BULL")

  # Tick with wide spread: Bid 3770.00, Ask 3830.00 -> Spread ~157 bps (threshold is 3x baseline = 15 bps)
  t_utc = datetime(2026, 11, 14, 4, 45, 0, tzinfo=timezone.utc)
  depth = OrderBookDepth(
      symbol="TCS",
      timestamp_utc=t_utc,
      bids=[OrderBookLevel(price=Decimal("3770.00"), quantity=200, orders_count=2)],
      asks=[OrderBookLevel(price=Decimal("3830.00"), quantity=200, orders_count=2)],
  )
  packet = StreamingTickPacket(
      subscription_mode=SUBSCRIPTION_MODE_SNAPQUOTE,
      exchange_type=EXCHANGE_NSE_CM,
      token="11536",
      sequence_number=10,
      exchange_timestamp_utc=t_utc,
      last_traded_price=Decimal("3810.00"),  # Crosses ORH!
      last_traded_quantity=20,
      average_traded_price=Decimal("3800.00"),
      volume_traded_today=50000,
      total_buy_quantity=10000,
      total_sell_quantity=10000,
      open_price=Decimal("3785.00"),
      high_price=Decimal("3815.00"),
      low_price=Decimal("3770.00"),
      close_price=Decimal("3810.00"),
      bids=depth.bids,
      asks=depth.asks,
  )

  sig, reason = detector.on_tick(packet, depth)
  assert sig is None
  assert "LIQUIDITY_GATE_REJECTED" in reason
  assert "SPREAD_WIDENING_DEFERRAL" in reason


# ---------------------------------------------------------------------------
# Test 7: StreamingTickBreakoutDetector rejects adverse regime (BEAR)
# ---------------------------------------------------------------------------

def test_streaming_tick_breakout_detector_rejects_bear_regime():
  """Verifies detector blocks prospective breakout during BEAR volatility regime."""
  assert_paper_mode()
  detector = StreamingTickBreakoutDetector()
  detector.register_opening_range(
      OpeningRangeLevels(
          symbol="INFY",
          session_date="2026-11-14",
          orh=Decimal("1850.00"),
          orl=Decimal("1830.00"),
          vwap=Decimal("1840.00"),
          is_formed=True,
      )
  )
  # Prevailing regime is classified as BEAR by M19 HMM
  detector.set_regime("INFY", "BEAR")

  t_utc = datetime(2026, 11, 14, 5, 0, 0, tzinfo=timezone.utc)
  depth = OrderBookDepth(
      symbol="INFY",
      timestamp_utc=t_utc,
      bids=[OrderBookLevel(price=Decimal("1851.50"), quantity=300, orders_count=3)],
      asks=[OrderBookLevel(price=Decimal("1852.00"), quantity=300, orders_count=3)],
  )
  packet = StreamingTickPacket(
      subscription_mode=SUBSCRIPTION_MODE_SNAPQUOTE,
      exchange_type=EXCHANGE_NSE_CM,
      token="1594",
      sequence_number=12,
      exchange_timestamp_utc=t_utc,
      last_traded_price=Decimal("1852.00"),  # > 1850.00 ORH
      last_traded_quantity=50,
      average_traded_price=Decimal("1845.00"),
      volume_traded_today=100000,
      total_buy_quantity=20000,
      total_sell_quantity=20000,
      open_price=Decimal("1835.00"),
      high_price=Decimal("1855.00"),
      low_price=Decimal("1830.00"),
      close_price=Decimal("1850.00"),
      bids=depth.bids,
      asks=depth.asks,
  )

  sig, reason = detector.on_tick(packet, depth)
  assert sig is None
  assert reason == "ADVERSE_REGIME:BEAR"


# ---------------------------------------------------------------------------
# Test 8: M26 REST API endpoints integration test
# ---------------------------------------------------------------------------

def test_m26_api_endpoints_integration():
  """Smoke-tests /market/streaming/status and /market/depth/{symbol} endpoints."""
  from fastapi.testclient import TestClient
  from app.main import app

  client = TestClient(app)

  # --- Streaming status endpoint ---
  r1 = client.get("/market/streaming/status")
  assert r1.status_code == 200, f"Streaming status returned {r1.status_code}: {r1.text}"
  data1 = r1.json()
  assert "is_connected" in data1
  assert "total_frames_processed" in data1
  assert "latency_p95_ms" in data1
  assert data1["subscribed_instruments_count"] >= 3

  # --- Market depth endpoint (subscribed symbol) ---
  r2 = client.get("/market/depth/RELIANCE")
  assert r2.status_code == 200, f"Market depth returned {r2.status_code}: {r2.text}"
  data2 = r2.json()
  assert data2["status"] == "ACTIVE"
  assert data2["symbol"] == "RELIANCE"
  assert "book" in data2
  assert "microstructure" in data2
  assert len(data2["book"]["bids"]) >= 1
  assert "spread_bps" in data2["microstructure"]
  assert "micro_price" in data2["microstructure"]

  # --- Market depth endpoint (unsubscribed symbol) ---
  r3 = client.get("/market/depth/UNKNOWN_XYZ")
  assert r3.status_code == 200
  data3 = r3.json()
  assert data3["status"] == "NOT_SUBSCRIBED"
