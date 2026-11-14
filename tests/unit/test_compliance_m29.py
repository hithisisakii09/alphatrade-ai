"""Unit & Integration Tests — M29: Regulatory Compliance, WORM Audit Logging & FIX Drop-Copy.

Institutional Invariants Verified:
1. SEBI WORM Trade Logger initializes with GENESIS_HASH and chains SHA-256 blocks seamlessly.
2. Tamper detection: single-byte modification invalidates cryptographic proof.
3. Microsecond precision dual timestamps (UTC + IST) and SEBI mandatory fields.
4. FIX 4.4 ExecutionReport message framing, SOH delimiter, Tag 9 length, and Tag 10 checksum.
5. FIX Drop-Copy feed broadcast, subscriber notification, and ring-buffer capacity.
6. Append-only WORM file persistence across logger process reloads.
7. Fail-closed paper-mode guard rejects LIVE-mode execution attempts.
8. REST API endpoints smoke-test for /compliance/audit-trail and /compliance/drop-copy/status.
"""

import os
os.environ.setdefault("ALPHATRADE_EXECUTION_MODE", "PAPER")

from decimal import Decimal
import json
import tempfile
from datetime import datetime, timezone

from app.compliance.drop_copy import (
    FIXDropCopyFeed,
    FIXDropCopyMessage,
    format_fix_transact_time,
)
from app.compliance.worm_logger import (
    AuditEventType,
    AuditEntry,
    GENESIS_HASH,
    SEBIWORMTradeLogger,
)


# ---------------------------------------------------------------------------
# Test 1: Genesis block and cryptographic hash chaining
# ---------------------------------------------------------------------------

def test_worm_logger_genesis_and_chaining():
  """Verifies that entries chain SHA-256 hashes sequentially starting from GENESIS_HASH."""
  with tempfile.TemporaryDirectory() as tmp:
    logger = SEBIWORMTradeLogger(log_dir=tmp)
    assert len(logger._chain) == 0

    e1 = logger.append_entry(
        event_type=AuditEventType.ORDER_PLACED,
        cl_ord_id="CL_001",
        symbol="RELIANCE",
        side="BUY",
        quantity=100,
        price=Decimal("2850.00"),
        broker_order_id="BRK_001",
    )
    assert e1.sequence_num == 1
    assert e1.prev_hash == GENESIS_HASH
    assert len(e1.entry_hash) == 64

    e2 = logger.append_entry(
        event_type=AuditEventType.TRADE_EXECUTED,
        cl_ord_id="CL_001",
        symbol="RELIANCE",
        side="BUY",
        quantity=100,
        price=Decimal("2850.00"),
        execution_price=Decimal("2850.00"),
        traded_quantity=100,
        broker_order_id="BRK_001",
    )
    assert e2.sequence_num == 2
    assert e2.prev_hash == e1.entry_hash
    assert len(e2.entry_hash) == 64

    ver = logger.verify_chain()
    assert ver.is_valid is True
    assert ver.total_records == 2
    assert ver.reason == "VALID"
    assert ver.tip_hash == e2.entry_hash
  print("  [PASS] test_worm_logger_genesis_and_chaining")


# ---------------------------------------------------------------------------
# Test 2: Tamper detection breaks hash chain
# ---------------------------------------------------------------------------

def test_worm_logger_tamper_detection_breaks_chain():
  """Modifying an entry's quantity or price must fail cryptographic verification."""
  with tempfile.TemporaryDirectory() as tmp:
    logger = SEBIWORMTradeLogger(log_dir=tmp)
    for i in range(1, 6):
      logger.append_entry(
          event_type=AuditEventType.ORDER_PLACED,
          cl_ord_id=f"CL_{i:03d}",
          symbol="TCS",
          side="BUY",
          quantity=50 * i,
          price=Decimal("3800.00"),
      )

    ver_clean = logger.verify_chain()
    assert ver_clean.is_valid is True

    # Mutate entry 3 quantity
    orig_entry = logger._chain[2]
    mutated = AuditEntry(
        sequence_num=orig_entry.sequence_num,
        audit_id=orig_entry.audit_id,
        timestamp_utc=orig_entry.timestamp_utc,
        timestamp_ist=orig_entry.timestamp_ist,
        event_type=orig_entry.event_type,
        cl_ord_id=orig_entry.cl_ord_id,
        broker_order_id=orig_entry.broker_order_id,
        algo_strategy_id=orig_entry.algo_strategy_id,
        symbol=orig_entry.symbol,
        exchange_segment=orig_entry.exchange_segment,
        side=orig_entry.side,
        order_type=orig_entry.order_type,
        quantity=orig_entry.quantity + 999,  # Tamper
        price=orig_entry.price,
        trigger_price=orig_entry.trigger_price,
        execution_price=orig_entry.execution_price,
        traded_quantity=orig_entry.traded_quantity,
        client_code=orig_entry.client_code,
        sebi_reg_id=orig_entry.sebi_reg_id,
        prev_hash=orig_entry.prev_hash,
        entry_hash=orig_entry.entry_hash,
    )
    logger._chain[2] = mutated

    ver_tampered = logger.verify_chain()
    assert ver_tampered.is_valid is False
    assert ver_tampered.invalid_sequence_num == 3
    assert "PAYLOAD_HASH_TAMPERED" in ver_tampered.reason
  print("  [PASS] test_worm_logger_tamper_detection_breaks_chain")


# ---------------------------------------------------------------------------
# Test 3: SEBI mandatory fields and microsecond precision
# ---------------------------------------------------------------------------

def test_worm_logger_sebi_fields_and_microsecond_precision():
  """Verifies presence of dual microsecond timestamps, SEBI reg ID, and Decimal formatting."""
  with tempfile.TemporaryDirectory() as tmp:
    logger = SEBIWORMTradeLogger(log_dir=tmp)
    entry = logger.append_entry(
        event_type=AuditEventType.ALGO_SLICED,
        cl_ord_id="CL_SLICE_001",
        symbol="INFY",
        side="BUY",
        quantity=200,
        price=Decimal("1850.50"),
        broker_order_id="BRK_SLICE_001",
        algo_strategy_id="VWAP_SLICER_M25",
        client_code="PAPER_CLIENT_001",
        sebi_reg_id="SEBI_IN_2026_099",
    )

    assert entry.client_code == "PAPER_CLIENT_001"
    assert entry.sebi_reg_id == "SEBI_IN_2026_099"
    assert entry.algo_strategy_id == "VWAP_SLICER_M25"
    assert entry.exchange_segment == "NSE_EQ"
    assert entry.price == "1850.50"
    assert "." in entry.timestamp_utc and entry.timestamp_utc.endswith("Z")
    assert "." in entry.timestamp_ist and "+05:30" in entry.timestamp_ist
  print("  [PASS] test_worm_logger_sebi_fields_and_microsecond_precision")


# ---------------------------------------------------------------------------
# Test 4: FIX 4.4 message formatting and checksum computation
# ---------------------------------------------------------------------------

def test_fix_drop_copy_message_formatting_and_checksum():
  """Verifies standard FIX 4.4 wire format, Tag 9 length, and Tag 10 checksum."""
  msg = FIXDropCopyMessage(
      msg_seq_num=1,
      sending_time="20261114-10:30:00.000",
      cl_ord_id="CL_TEST_001",
      order_id="ORD_TEST_001",
      exec_id="EXEC_00000001",
      exec_type="2",  # Fill
      ord_status="2", # Filled
      symbol="RELIANCE",
      side="1",       # Buy
      order_qty=100,
      price="2850.00",
      last_qty=100,
      last_px="2850.00",
      leaves_qty=0,
      cum_qty=100,
      account="ALPHATRADE_PAPER_001",
  )

  raw_fix = msg.to_raw_fix()
  assert raw_fix.startswith("8=FIX.4.4\x019=")
  assert "\x0135=8\x01" in raw_fix
  assert "\x0110=" in raw_fix
  assert raw_fix.endswith("\x01")

  # Validate checksum
  chk_idx = raw_fix.rfind("10=")
  body_for_chk = raw_fix[:chk_idx]
  expected_chk = f"{sum(body_for_chk.encode('latin-1')) % 256:03d}"
  actual_chk = raw_fix[chk_idx + 3 : chk_idx + 6]
  assert actual_chk == expected_chk, f"Checksum mismatch: expected {expected_chk}, got {actual_chk}"

  printable = msg.to_printable()
  assert "|" in printable and "35=8" in printable
  assert "10=" in printable
  print("  [PASS] test_fix_drop_copy_message_formatting_and_checksum")


# ---------------------------------------------------------------------------
# Test 5: FIX Drop-Copy feed broadcast and subscriber dispatch
# ---------------------------------------------------------------------------

def test_fix_drop_copy_feed_broadcast_and_buffering():
  """Verifies publishing execution reports broadcasts to subscribers and logs to ring buffer."""
  feed = FIXDropCopyFeed(buffer_capacity=10)
  dispatched: list[FIXDropCopyMessage] = []
  feed.subscribe(lambda m: dispatched.append(m))

  for i in range(1, 15):
    feed.publish_execution_report(
        cl_ord_id=f"CL_FEED_{i}",
        order_id=f"ORD_FEED_{i}",
        exec_type="0",
        ord_status="0",
        symbol="TCS",
        side="BUY",
        order_qty=10 * i,
        price=Decimal("3800.00"),
    )

  assert len(dispatched) == 14, "Subscriber must receive all 14 broadcast messages"
  stats = feed.get_stats()
  assert stats["total_messages_broadcast"] == 14
  assert stats["buffer_size"] == 10, "Ring buffer must respect capacity limit"
  assert stats["active_subscribers"] == 1

  recent = feed.get_recent_messages(limit=5)
  assert len(recent) == 5
  assert recent[-1]["cl_ord_id"] == "CL_FEED_14"
  print("  [PASS] test_fix_drop_copy_feed_broadcast_and_buffering")


# ---------------------------------------------------------------------------
# Test 6: WORM log file persistence and cross-process reload
# ---------------------------------------------------------------------------

def test_worm_log_file_persistence_and_reload():
  """Verifies appending persists to disk and a new logger instance reloads exact state."""
  with tempfile.TemporaryDirectory() as tmp:
    logger1 = SEBIWORMTradeLogger(log_dir=tmp, log_filename="audit_reload.log")
    logger1.append_entry(
        event_type=AuditEventType.ORDER_PLACED,
        cl_ord_id="CL_PERSIST_001",
        symbol="HDFCBANK",
        side="BUY",
        quantity=50,
        price=Decimal("1700.00"),
    )
    logger1.append_entry(
        event_type=AuditEventType.TRADE_EXECUTED,
        cl_ord_id="CL_PERSIST_001",
        symbol="HDFCBANK",
        side="BUY",
        quantity=50,
        price=Decimal("1700.00"),
        execution_price=Decimal("1700.00"),
        traded_quantity=50,
    )
    tip1 = logger1._chain[-1].entry_hash

    # Re-initialize new logger instance against the same file
    logger2 = SEBIWORMTradeLogger(log_dir=tmp, log_filename="audit_reload.log")
    assert len(logger2._chain) == 2
    assert logger2._chain[-1].entry_hash == tip1

    ver = logger2.verify_chain()
    assert ver.is_valid is True
    assert ver.total_records == 2
  print("  [PASS] test_worm_log_file_persistence_and_reload")


# ---------------------------------------------------------------------------
# Test 7: Fail-closed paper mode guard
# ---------------------------------------------------------------------------

def test_compliance_fail_closed_paper_mode_guard(monkeypatch):
  """Setting LIVE mode must trigger LiveTradingProhibitedError on compliance initialization."""
  from app.core.execution_guard import LiveTradingProhibitedError

  orig = os.environ.get("ALPHATRADE_EXECUTION_MODE", "PAPER")
  try:
    monkeypatch.setenv("ALPHATRADE_EXECUTION_MODE", "LIVE")
    try:
      SEBIWORMTradeLogger(log_dir="tmp_live_compliance_guard")
      raise AssertionError("Expected LiveTradingProhibitedError was not raised")
    except LiveTradingProhibitedError:
      pass

    try:
      FIXDropCopyFeed()
      raise AssertionError("Expected LiveTradingProhibitedError was not raised")
    except LiveTradingProhibitedError:
      pass
  finally:
    monkeypatch.setenv("ALPHATRADE_EXECUTION_MODE", orig)
  print("  [PASS] test_compliance_fail_closed_paper_mode_guard")


# ---------------------------------------------------------------------------
# Test 8: REST API endpoints integration smoke test
# ---------------------------------------------------------------------------

def test_m29_api_endpoints_integration():
  """Smoke-test GET /compliance/audit-trail and GET /compliance/drop-copy/status."""
  from fastapi.testclient import TestClient
  from app.main import app

  client = TestClient(app)

  # GET /compliance/audit-trail
  r = client.get("/compliance/audit-trail?limit=10")
  assert r.status_code == 200, f"GET /compliance/audit-trail failed: {r.text}"
  body = r.json()
  assert "chain_height" in body
  assert "genesis_hash" in body
  assert "is_chain_intact" in body
  assert body["is_chain_intact"] is True
  assert "recent_entries" in body
  assert len(body["recent_entries"]) >= 1

  # GET /compliance/drop-copy/status
  from app.api.routes import _drop_copy_feed
  if _drop_copy_feed.get_stats()["total_messages_broadcast"] == 0:
    _drop_copy_feed.publish_execution_report(
        cl_ord_id="CLORD_TEST_API_001",
        order_id="BROKER_TEST_API_001",
        exec_type="0",
        ord_status="0",
        symbol="RELIANCE",
        side="BUY",
        order_qty=100,
        price="2850.00",
    )
  r2 = client.get("/compliance/drop-copy/status?limit=10")
  assert r2.status_code == 200, f"GET /compliance/drop-copy/status failed: {r2.text}"
  body2 = r2.json()
  assert "total_messages_broadcast" in body2
  assert "buffer_size" in body2
  assert "recent_messages" in body2
  assert len(body2["recent_messages"]) >= 1
  assert "printable_fix" in body2["recent_messages"][0]
  print(f"  [PASS] test_m29_api_endpoints_integration (height={body['chain_height']}, msgs={body2['total_messages_broadcast']})")
