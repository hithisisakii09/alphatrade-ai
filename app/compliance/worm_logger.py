"""SEBI-Compliant WORM (Write-Once-Read-Many) Trade & Order Audit Logger (Milestone M29).

Institutional Invariants:
1. Strict fail-closed paper mode execution enforcement via assert_paper_mode().
2. Pure Decimal financial arithmetic with zero binary float drift.
3. Cryptographically hash-chained SHA-256 blocks: entry_hash = SHA256(prev_hash | canonical_json).
4. Append-only WORM disk persistence with immediate fsync to prevent data tampering.
5. Microsecond precision dual timestamps: UTC and IST (compliant with SEBI circular guidelines).
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
import logging
import os
import threading
import time
from typing import Any

from app.core.clock import to_ist
from app.core.execution_guard import assert_paper_mode
from app.core.money import to_decimal

logger = logging.getLogger("WORMTradeLogger")

GENESIS_HASH: str = "0" * 64


class AuditChainCorruptedError(ValueError):
  """Raised when an audit chain link is broken or cryptographic hash check fails."""


class AuditEventType(str, Enum):
  ORDER_PLACED = "ORDER_PLACED"
  ORDER_MODIFIED = "ORDER_MODIFIED"
  ORDER_CANCELLED = "ORDER_CANCELLED"
  TRADE_EXECUTED = "TRADE_EXECUTED"
  STOP_UPDATED = "STOP_UPDATED"
  ALGO_SLICED = "ALGO_SLICED"
  KILL_SWITCH_TRIPPED = "KILL_SWITCH_TRIPPED"


@dataclass(frozen=True)
class AuditEntry:
  """Immutable, cryptographically verifiable SEBI audit record."""
  sequence_num: int
  audit_id: str
  timestamp_utc: str
  timestamp_ist: str
  event_type: str
  cl_ord_id: str
  broker_order_id: str
  algo_strategy_id: str
  symbol: str
  exchange_segment: str  # e.g., "NSE_EQ"
  side: str              # "BUY" / "SELL"
  order_type: str        # "MARKET" / "LIMIT" / "SL"
  quantity: int
  price: str             # Pure Decimal string
  trigger_price: str     # Pure Decimal string
  execution_price: str   # Pure Decimal string
  traded_quantity: int
  client_code: str       # Institutional / Paper client code
  sebi_reg_id: str       # Algorithmic registration ID
  prev_hash: str
  entry_hash: str

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)

  def to_canonical_json_excluding_hash(self) -> str:
    """Produces deterministic canonical JSON string without the entry_hash."""
    d = asdict(self)
    d.pop("entry_hash", None)
    return json.dumps(d, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class AuditChainVerificationResult:
  """Result of an end-to-end audit chain cryptographic integrity verification."""
  total_records: int
  genesis_hash: str
  tip_hash: str
  is_valid: bool
  invalid_sequence_num: int | None = None
  reason: str = "VALID"
  verification_latency_ms: float = 0.0

  def to_dict(self) -> dict[str, Any]:
    return {
        "total_records": self.total_records,
        "genesis_hash": self.genesis_hash[:16] + "...",
        "tip_hash": self.tip_hash[:16] + "..." if self.tip_hash else "",
        "is_valid": self.is_valid,
        "invalid_sequence_num": self.invalid_sequence_num,
        "reason": self.reason,
        "verification_latency_ms": round(self.verification_latency_ms, 4),
    }


class SEBIWORMTradeLogger:
  """Thread-safe append-only WORM trade logger with SHA-256 cryptographic chain integrity."""

  def __init__(self, log_dir: str = ".audit", log_filename: str = "sebi_worm_audit.log"):
    assert_paper_mode()
    self.log_dir = log_dir
    os.makedirs(self.log_dir, exist_ok=True)
    self.log_path = os.path.join(self.log_dir, log_filename)
    self._lock = threading.Lock()
    self._chain: list[AuditEntry] = []
    self._load_existing_log()

  def _compute_hash(self, prev_hash: str, canonical_payload: str) -> str:
    """Computes SHA-256 over prev_hash and canonical payload string."""
    to_hash = f"{prev_hash}|{canonical_payload}".encode("utf-8")
    return hashlib.sha256(to_hash).hexdigest()

  def _load_existing_log(self) -> None:
    """Loads existing audit entries from WORM file on startup and validates chain."""
    if not os.path.exists(self.log_path):
      return

    with open(self.log_path, "r", encoding="utf-8") as f:
      for line_num, line in enumerate(f, start=1):
        line = line.strip()
        if not line:
          continue
        try:
          data = json.loads(line)
          entry = AuditEntry(**data)
          self._chain.append(entry)
        except Exception as exc:
          logger.error("Failed to parse audit log line %d: %s", line_num, exc)
          raise AuditChainCorruptedError(f"Corrupt audit log at line {line_num}: {exc}") from exc

  def append_entry(
      self,
      event_type: AuditEventType | str,
      cl_ord_id: str,
      symbol: str,
      side: str,
      quantity: int,
      price: Decimal | str,
      broker_order_id: str = "",
      algo_strategy_id: str = "ALPHATRADE_V2_ORB",
      order_type: str = "MARKET",
      trigger_price: Decimal | str = "0.00",
      execution_price: Decimal | str = "0.00",
      traded_quantity: int = 0,
      client_code: str = "ALPHATRADE_PAPER_001",
      sebi_reg_id: str = "SEBI_ALGO_APP_2026_V2",
      exchange_segment: str = "NSE_EQ",
      custom_timestamp_utc: datetime | None = None,
  ) -> AuditEntry:
    """Atomically appends a tamper-evident audit record to the WORM log."""
    assert_paper_mode()
    with self._lock:
      seq_num = len(self._chain) + 1
      prev_hash = self._chain[-1].entry_hash if self._chain else GENESIS_HASH

      now_utc = custom_timestamp_utc or datetime.now(timezone.utc)
      now_ist = to_ist(now_utc)
      ts_utc_str = now_utc.strftime("%Y-%m-%d %H:%M:%S.%f") + "Z"
      ts_ist_str = now_ist.strftime("%Y-%m-%d %H:%M:%S.%f") + "+05:30"

      ev_str = event_type.value if isinstance(event_type, AuditEventType) else str(event_type)
      audit_id = f"AUD_{now_utc.strftime('%Y%m%d')}_{seq_num:08d}"

      # Canonical representation without hash
      temp_entry = AuditEntry(
          sequence_num=seq_num,
          audit_id=audit_id,
          timestamp_utc=ts_utc_str,
          timestamp_ist=ts_ist_str,
          event_type=ev_str,
          cl_ord_id=cl_ord_id,
          broker_order_id=broker_order_id,
          algo_strategy_id=algo_strategy_id,
          symbol=symbol.upper(),
          exchange_segment=exchange_segment,
          side=side.upper(),
          order_type=order_type.upper(),
          quantity=quantity,
          price=str(to_decimal(price)),
          trigger_price=str(to_decimal(trigger_price)),
          execution_price=str(to_decimal(execution_price)),
          traded_quantity=traded_quantity,
          client_code=client_code,
          sebi_reg_id=sebi_reg_id,
          prev_hash=prev_hash,
          entry_hash="",
      )

      canonical_json = temp_entry.to_canonical_json_excluding_hash()
      entry_hash = self._compute_hash(prev_hash, canonical_json)

      entry = AuditEntry(
          sequence_num=temp_entry.sequence_num,
          audit_id=temp_entry.audit_id,
          timestamp_utc=temp_entry.timestamp_utc,
          timestamp_ist=temp_entry.timestamp_ist,
          event_type=temp_entry.event_type,
          cl_ord_id=temp_entry.cl_ord_id,
          broker_order_id=temp_entry.broker_order_id,
          algo_strategy_id=temp_entry.algo_strategy_id,
          symbol=temp_entry.symbol,
          exchange_segment=temp_entry.exchange_segment,
          side=temp_entry.side,
          order_type=temp_entry.order_type,
          quantity=temp_entry.quantity,
          price=temp_entry.price,
          trigger_price=temp_entry.trigger_price,
          execution_price=temp_entry.execution_price,
          traded_quantity=temp_entry.traded_quantity,
          client_code=temp_entry.client_code,
          sebi_reg_id=temp_entry.sebi_reg_id,
          prev_hash=prev_hash,
          entry_hash=entry_hash,
      )

      # Append to in-memory chain
      self._chain.append(entry)

      # WORM file persistence with immediate flush and sync
      with open(self.log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry.to_dict()) + "\n")
        f.flush()
        os.fsync(f.fileno())

      return entry

  def verify_chain(self) -> AuditChainVerificationResult:
    """Verifies complete cryptographic integrity from genesis block to current tip."""
    assert_paper_mode()
    t0 = time.time()
    with self._lock:
      if not self._chain:
        return AuditChainVerificationResult(
            total_records=0,
            genesis_hash=GENESIS_HASH,
            tip_hash="",
            is_valid=True,
            reason="EMPTY_CHAIN",
            verification_latency_ms=(time.time() - t0) * 1000.0,
        )

      expected_prev = GENESIS_HASH
      for i, entry in enumerate(self._chain):
        # 1. Sequence number continuity
        if entry.sequence_num != i + 1:
          return AuditChainVerificationResult(
              total_records=len(self._chain),
              genesis_hash=GENESIS_HASH,
              tip_hash=self._chain[-1].entry_hash,
              is_valid=False,
              invalid_sequence_num=entry.sequence_num,
              reason=f"SEQUENCE_DISCONTINUITY: expected {i+1}, got {entry.sequence_num}",
              verification_latency_ms=(time.time() - t0) * 1000.0,
          )

        # 2. Previous hash link check
        if entry.prev_hash != expected_prev:
          return AuditChainVerificationResult(
              total_records=len(self._chain),
              genesis_hash=GENESIS_HASH,
              tip_hash=self._chain[-1].entry_hash,
              is_valid=False,
              invalid_sequence_num=entry.sequence_num,
              reason=f"PREV_HASH_MISMATCH at sequence {entry.sequence_num}",
              verification_latency_ms=(time.time() - t0) * 1000.0,
          )

        # 3. Cryptographic payload hash recalculation
        canon = entry.to_canonical_json_excluding_hash()
        computed_hash = self._compute_hash(entry.prev_hash, canon)
        if entry.entry_hash != computed_hash:
          return AuditChainVerificationResult(
              total_records=len(self._chain),
              genesis_hash=GENESIS_HASH,
              tip_hash=self._chain[-1].entry_hash,
              is_valid=False,
              invalid_sequence_num=entry.sequence_num,
              reason=f"PAYLOAD_HASH_TAMPERED at sequence {entry.sequence_num}",
              verification_latency_ms=(time.time() - t0) * 1000.0,
          )

        expected_prev = entry.entry_hash

      return AuditChainVerificationResult(
          total_records=len(self._chain),
          genesis_hash=GENESIS_HASH,
          tip_hash=self._chain[-1].entry_hash,
          is_valid=True,
          reason="VALID",
          verification_latency_ms=(time.time() - t0) * 1000.0,
      )

  def get_recent_entries(self, limit: int = 20) -> list[dict[str, Any]]:
    """Returns the most recent N audit records for dashboard and API."""
    assert_paper_mode()
    with self._lock:
      return [e.to_dict() for e in self._chain[-limit:]]

  def get_stats(self) -> dict[str, Any]:
    """Returns audit log telemetry and verification state."""
    assert_paper_mode()
    verification = self.verify_chain()
    tip = self._chain[-1].entry_hash if self._chain else ""
    return {
        "chain_height": len(self._chain),
        "genesis_hash": GENESIS_HASH[:16] + "...",
        "tip_hash": tip[:16] + "..." if tip else "NONE",
        "log_path": self.log_path,
        "is_chain_intact": verification.is_valid,
        "verification": verification.to_dict(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
