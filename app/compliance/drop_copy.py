"""Institutional FIX 4.4 / 5.0 Drop-Copy Compliance Feed (Milestone M29).

Institutional Invariants:
1. Strict fail-closed paper mode execution enforcement via assert_paper_mode().
2. Pure Decimal financial arithmetic with zero binary float drift.
3. FIX 4.4 standard message framing with SOH (\\x01) tags and 3-digit CheckSum (Tag 10).
4. Real-time broadcast to compliance subscribers and in-memory ring-buffer logging.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import threading
from typing import Any, Callable

from app.core.execution_guard import assert_paper_mode
from app.core.money import to_decimal


def format_fix_transact_time(dt: datetime | None = None) -> str:
  """Formats datetime to FIX standard UTC timestamp: YYYYMMDD-HH:MM:SS.sss."""
  dt = dt or datetime.now(timezone.utc)
  return dt.strftime("%Y%m%d-%H:%M:%S.%f")[:-3]


@dataclass(frozen=True)
class FIXDropCopyMessage:
  """Standard FIX 4.4 ExecutionReport (MsgType=8) drop-copy message."""
  msg_seq_num: int
  sending_time: str
  cl_ord_id: str
  order_id: str
  exec_id: str
  exec_type: str       # "0"=New, "1"=Partial, "2"=Fill, "4"=Cancel, "8"=Rejected
  ord_status: str      # "0"=New, "1"=Partial, "2"=Filled, "4"=Cancelled, "8"=Rejected
  symbol: str
  side: str            # "1"=Buy, "2"=Sell
  order_qty: int
  price: str           # Pure Decimal string
  last_qty: int
  last_px: str         # Pure Decimal string
  leaves_qty: int
  cum_qty: int
  account: str
  sender_comp_id: str = "ALPHATRADE_OMS"
  target_comp_id: str = "SEBI_DROP_COPY"

  def compute_checksum(self, body_str: str) -> str:
    """Computes FIX standard 3-digit modulo 256 checksum over byte representation."""
    total = sum(body_str.encode("latin-1"))
    return f"{total % 256:03d}"

  def to_raw_fix(self, delimiter: str = "\x01") -> str:
    """Renders raw FIX protocol wire message with Tag 9 (BodyLength) and Tag 10 (CheckSum)."""
    # Body tags (MsgType through Account)
    body_tags = [
        f"35=8",
        f"49={self.sender_comp_id}",
        f"56={self.target_comp_id}",
        f"34={self.msg_seq_num}",
        f"52={self.sending_time}",
        f"11={self.cl_ord_id}",
        f"37={self.order_id}",
        f"17={self.exec_id}",
        f"150={self.exec_type}",
        f"39={self.ord_status}",
        f"55={self.symbol}",
        f"54={self.side}",
        f"38={self.order_qty}",
        f"44={self.price}",
        f"32={self.last_qty}",
        f"31={self.last_px}",
        f"151={self.leaves_qty}",
        f"14={self.cum_qty}",
        f"60={self.sending_time}",
        f"1={self.account}",
    ]
    body_str = delimiter.join(body_tags) + delimiter

    # Tag 8 and Tag 9
    header_str = f"8=FIX.4.4{delimiter}9={len(body_str.encode('latin-1'))}{delimiter}"
    pre_checksum = header_str + body_str

    chk = self.compute_checksum(pre_checksum)
    return f"{pre_checksum}10={chk}{delimiter}"

  def to_printable(self) -> str:
    """Returns human-readable representation using pipe '|' delimiter."""
    return self.to_raw_fix(delimiter="|")

  def to_dict(self) -> dict[str, Any]:
    return {
        "msg_seq_num": self.msg_seq_num,
        "sending_time": self.sending_time,
        "cl_ord_id": self.cl_ord_id,
        "order_id": self.order_id,
        "exec_id": self.exec_id,
        "exec_type": self.exec_type,
        "ord_status": self.ord_status,
        "symbol": self.symbol,
        "side": "BUY" if self.side == "1" else "SELL",
        "order_qty": self.order_qty,
        "price": self.price,
        "last_qty": self.last_qty,
        "last_px": self.last_px,
        "leaves_qty": self.leaves_qty,
        "cum_qty": self.cum_qty,
        "account": self.account,
        "printable_fix": self.to_printable(),
    }


class FIXDropCopyFeed:
  """Thread-safe simulated FIX 4.4 Drop-Copy compliance feed with broadcast dispatch."""

  def __init__(self, buffer_capacity: int = 1000):
    assert_paper_mode()
    self._capacity = buffer_capacity
    self._buffer: deque[FIXDropCopyMessage] = deque(maxlen=self._capacity)
    self._subscribers: list[Callable[[FIXDropCopyMessage], None]] = []
    self._lock = threading.Lock()
    self._seq_counter: int = 0

  def subscribe(self, callback: Callable[[FIXDropCopyMessage], None]) -> None:
    """Registers a compliance callback receiver for live FIX drop-copy feeds."""
    assert_paper_mode()
    with self._lock:
      self._subscribers.append(callback)

  def publish_execution_report(
      self,
      cl_ord_id: str,
      order_id: str,
      exec_type: str,
      ord_status: str,
      symbol: str,
      side: str,
      order_qty: int,
      price: Decimal | str,
      last_qty: int = 0,
      last_px: Decimal | str = "0.00",
      cum_qty: int = 0,
      leaves_qty: int | None = None,
      account: str = "ALPHATRADE_PAPER_001",
      custom_time: datetime | None = None,
  ) -> FIXDropCopyMessage:
    """Constructs, serializes, buffers, and broadcasts a FIX ExecutionReport."""
    assert_paper_mode()
    with self._lock:
      self._seq_counter += 1
      seq = self._seq_counter

      leaves = leaves_qty if leaves_qty is not None else max(0, order_qty - cum_qty)
      side_fix = "1" if side.upper() in ("BUY", "1") else "2"
      sending_time = format_fix_transact_time(custom_time)
      exec_id = f"EXEC_{seq:08d}"

      msg = FIXDropCopyMessage(
          msg_seq_num=seq,
          sending_time=sending_time,
          cl_ord_id=cl_ord_id,
          order_id=order_id,
          exec_id=exec_id,
          exec_type=str(exec_type),
          ord_status=str(ord_status),
          symbol=symbol.upper(),
          side=side_fix,
          order_qty=order_qty,
          price=str(to_decimal(price)),
          last_qty=last_qty,
          last_px=str(to_decimal(last_px)),
          leaves_qty=leaves,
          cum_qty=cum_qty,
          account=account,
      )

      self._buffer.append(msg)
      subscribers = list(self._subscribers)

    # Dispatch outside lock
    for sub in subscribers:
      try:
        sub(msg)
      except Exception:
        pass

    return msg

  def get_recent_messages(self, limit: int = 20) -> list[dict[str, Any]]:
    """Returns the most recent N FIX drop-copy messages."""
    assert_paper_mode()
    with self._lock:
      msgs = list(self._buffer)
      return [m.to_dict() for m in msgs[-limit:]]

  def get_stats(self) -> dict[str, Any]:
    """Returns telemetry of the drop-copy compliance feed."""
    assert_paper_mode()
    with self._lock:
      return {
          "total_messages_broadcast": self._seq_counter,
          "buffer_size": len(self._buffer),
          "buffer_capacity": self._capacity,
          "active_subscribers": len(self._subscribers),
          "last_seq_num": self._seq_counter,
          "timestamp_utc": datetime.now(timezone.utc).isoformat(),
      }
