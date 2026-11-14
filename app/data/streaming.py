"""Real-Time Level-2 WebSocket Streaming & Market-by-Price (MBP) Engine (Milestone M26).

Conforms strictly to institutional market data standards:
1. Pure Decimal arithmetic with zero binary float drift.
2. Binary packet decoding for Angel One SmartAPI WebSocket 2.0 (LTP, Quote, 5-Depth SnapQuote).
3. In-memory thread-safe LiveOrderBook maintaining 5-depth Market-by-Price (MBP) ladders.
4. Microsecond latency tracking (p50, p95, p99) and packet throughput counters.
5. Strict fail-closed paper trading mode enforcement (assert_paper_mode()).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
import struct
import threading
import time
from typing import Any, Callable

from app.core.execution_guard import assert_paper_mode
from app.core.money import round_paise, round_price, to_decimal
from app.market.microstructure import OrderBookDepth, OrderBookLevel


# Angel One SmartAPI WebSocket 2.0 Subscription Modes
SUBSCRIPTION_MODE_LTP = 1
SUBSCRIPTION_MODE_QUOTE = 2
SUBSCRIPTION_MODE_SNAPQUOTE = 3  # Full 5-Depth MBP Order Book

EXCHANGE_NSE_CM = 1  # NSE Cash Equities


@dataclass(frozen=True)
class StreamingTickPacket:
  """Structured market tick unpacked from raw binary WebSocket frames."""
  subscription_mode: int
  exchange_type: int
  token: str
  sequence_number: int
  exchange_timestamp_utc: datetime
  last_traded_price: Decimal
  last_traded_quantity: int
  average_traded_price: Decimal
  volume_traded_today: int
  total_buy_quantity: int
  total_sell_quantity: int
  open_price: Decimal
  high_price: Decimal
  low_price: Decimal
  close_price: Decimal
  bids: list[OrderBookLevel] = field(default_factory=list)
  asks: list[OrderBookLevel] = field(default_factory=list)

  def to_dict(self) -> dict[str, Any]:
    return {
        "subscription_mode": self.subscription_mode,
        "exchange_type": self.exchange_type,
        "token": self.token,
        "sequence_number": self.sequence_number,
        "exchange_timestamp_utc": self.exchange_timestamp_utc.isoformat(),
        "last_traded_price": str(self.last_traded_price),
        "last_traded_quantity": self.last_traded_quantity,
        "average_traded_price": str(self.average_traded_price),
        "volume_traded_today": self.volume_traded_today,
        "total_buy_quantity": self.total_buy_quantity,
        "total_sell_quantity": self.total_sell_quantity,
        "open_price": str(self.open_price),
        "high_price": str(self.high_price),
        "low_price": str(self.low_price),
        "close_price": str(self.close_price),
        "bids": [{"price": str(b.price), "quantity": b.quantity, "orders": b.orders_count} for b in self.bids],
        "asks": [{"price": str(a.price), "quantity": a.quantity, "orders": a.orders_count} for a in self.asks],
    }


class BinaryPacketParser:
  """Unpacks Angel One WebSocket 2.0 binary frames into StreamingTickPackets.
  
  Binary Layout (SnapQuote Mode 3):
    Header & Metadata (123 bytes):
      - mode: uint8 (1 byte)
      - exchange_type: uint8 (1 byte)
      - token: char[25] (25 bytes)
      - sequence_number: int64 (8 bytes)
      - exchange_timestamp_ms: int64 (8 bytes)
      - ltp_paise: int64 (8 bytes)
      - ltq: int64 (8 bytes)
      - atp_paise: int64 (8 bytes)
      - volume: int64 (8 bytes)
      - total_buy_qty: int64 (8 bytes)
      - total_sell_qty: int64 (8 bytes)
      - open_paise: int64 (8 bytes)
      - high_paise: int64 (8 bytes)
      - low_paise: int64 (8 bytes)
      - close_paise: int64 (8 bytes)
    Depth Ladders (200 bytes):
      - 10 entries (5 bids followed by 5 asks)
      - Each entry: flag (int16), quantity (int64), price_paise (int64), order_count (int16) -> 20 bytes
  """

  HEADER_FORMAT = "<BB25sqqqqqqqqqqqq"
  HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 123 bytes

  DEPTH_ENTRY_FORMAT = "<hqqh"
  DEPTH_ENTRY_SIZE = struct.calcsize(DEPTH_ENTRY_FORMAT)  # 20 bytes

  @classmethod
  def parse_packet(cls, buffer: bytes) -> StreamingTickPacket:
    """Parses binary buffer into a StreamingTickPacket."""
    assert_paper_mode()
    if len(buffer) < cls.HEADER_SIZE:
      raise ValueError(f"Buffer too short for binary tick header: {len(buffer)} < {cls.HEADER_SIZE}")

    (
        mode,
        exch,
        token_bytes,
        seq,
        ts_ms,
        ltp_p,
        ltq,
        atp_p,
        vol,
        tot_buy,
        tot_sell,
        open_p,
        high_p,
        low_p,
        close_p,
    ) = struct.unpack_from(cls.HEADER_FORMAT, buffer, 0)

    token = token_bytes.decode("ascii", errors="ignore").rstrip("\x00").strip()
    ts_utc = datetime.fromtimestamp(ts_ms / 1000.0, timezone.utc)

    # Convert paise (int64) to Decimal INR with zero float drift
    def _paise_to_decimal(val_paise: int) -> Decimal:
      return round_price(Decimal(val_paise) / Decimal("100"))

    ltp = _paise_to_decimal(ltp_p)
    atp = _paise_to_decimal(atp_p)
    open_p_dec = _paise_to_decimal(open_p)
    high_p_dec = _paise_to_decimal(high_p)
    low_p_dec = _paise_to_decimal(low_p)
    close_p_dec = _paise_to_decimal(close_p)

    bids: list[OrderBookLevel] = []
    asks: list[OrderBookLevel] = []

    # Parse 5-depth ladders if mode is SnapQuote (Mode 3)
    offset = cls.HEADER_SIZE
    if mode == SUBSCRIPTION_MODE_SNAPQUOTE and len(buffer) >= (cls.HEADER_SIZE + (10 * cls.DEPTH_ENTRY_SIZE)):
      for i in range(10):
        flag, depth_qty, depth_price_p, ord_cnt = struct.unpack_from(cls.DEPTH_ENTRY_FORMAT, buffer, offset)
        offset += cls.DEPTH_ENTRY_SIZE

        price_dec = _paise_to_decimal(depth_price_p)
        lvl = OrderBookLevel(price=price_dec, quantity=depth_qty, orders_count=ord_cnt)
        if i < 5:
          if depth_qty > 0 and price_dec > Decimal("0.00"):
            bids.append(lvl)
        else:
          if depth_qty > 0 and price_dec > Decimal("0.00"):
            asks.append(lvl)

    return StreamingTickPacket(
        subscription_mode=mode,
        exchange_type=exch,
        token=token,
        sequence_number=seq,
        exchange_timestamp_utc=ts_utc,
        last_traded_price=ltp,
        last_traded_quantity=ltq,
        average_traded_price=atp,
        volume_traded_today=vol,
        total_buy_quantity=tot_buy,
        total_sell_quantity=tot_sell,
        open_price=open_p_dec,
        high_price=high_p_dec,
        low_price=low_p_dec,
        close_price=close_p_dec,
        bids=bids,
        asks=asks,
    )

  @classmethod
  def encode_packet(cls, packet: StreamingTickPacket) -> bytes:
    """Encodes a StreamingTickPacket into binary struct for testing and replay simulation."""
    assert_paper_mode()
    token_bytes = packet.token.encode("ascii")[:25].ljust(25, b"\x00")
    ts_ms = int(packet.exchange_timestamp_utc.timestamp() * 1000)

    def _dec_to_paise(d: Decimal) -> int:
      return int((to_decimal(d) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    header_bytes = struct.pack(
        cls.HEADER_FORMAT,
        packet.subscription_mode,
        packet.exchange_type,
        token_bytes,
        packet.sequence_number,
        ts_ms,
        _dec_to_paise(packet.last_traded_price),
        packet.last_traded_quantity,
        _dec_to_paise(packet.average_traded_price),
        packet.volume_traded_today,
        packet.total_buy_quantity,
        packet.total_sell_quantity,
        _dec_to_paise(packet.open_price),
        _dec_to_paise(packet.high_price),
        _dec_to_paise(packet.low_price),
        _dec_to_paise(packet.close_price),
    )

    if packet.subscription_mode != SUBSCRIPTION_MODE_SNAPQUOTE:
      return header_bytes

    # 5 bids and 5 asks
    depth_bytes = bytearray()
    for i in range(5):
      if i < len(packet.bids):
        b = packet.bids[i]
        depth_bytes.extend(struct.pack(cls.DEPTH_ENTRY_FORMAT, 1, b.quantity, _dec_to_paise(b.price), b.orders_count))
      else:
        depth_bytes.extend(struct.pack(cls.DEPTH_ENTRY_FORMAT, 1, 0, 0, 0))

    for i in range(5):
      if i < len(packet.asks):
        a = packet.asks[i]
        depth_bytes.extend(struct.pack(cls.DEPTH_ENTRY_FORMAT, 2, a.quantity, _dec_to_paise(a.price), a.orders_count))
      else:
        depth_bytes.extend(struct.pack(cls.DEPTH_ENTRY_FORMAT, 2, 0, 0, 0))

    return header_bytes + bytes(depth_bytes)


class LiveOrderBook:
  """Thread-safe live Market-by-Price (MBP) book maintained per instrument token."""

  def __init__(self, symbol: str, token: str):
    assert_paper_mode()
    self.symbol = symbol
    self.token = token
    self._lock = threading.Lock()
    self.last_updated_utc: datetime = datetime.now(timezone.utc)
    self.last_price: Decimal = Decimal("0.00")
    self.last_quantity: int = 0
    self.volume_today: int = 0
    self.bids: list[OrderBookLevel] = []
    self.asks: list[OrderBookLevel] = []

  def update_from_tick(self, tick: StreamingTickPacket) -> None:
    """Updates order book state atomically from an incoming tick."""
    with self._lock:
      self.last_updated_utc = tick.exchange_timestamp_utc
      self.last_price = tick.last_traded_price
      self.last_quantity = tick.last_traded_quantity
      self.volume_today = tick.volume_traded_today

      if tick.bids:
        # Sort descending by price
        self.bids = sorted(tick.bids, key=lambda x: x.price, reverse=True)[:5]
      if tick.asks:
        # Sort ascending by price
        self.asks = sorted(tick.asks, key=lambda x: x.price)[:5]

  def get_snapshot(self) -> OrderBookDepth:
    """Extracts a thread-safe OrderBookDepth model compatible with M25 microstructure."""
    with self._lock:
      return OrderBookDepth(
          symbol=self.symbol,
          timestamp_utc=self.last_updated_utc,
          bids=list(self.bids),
          asks=list(self.asks),
      )

  def to_dict(self) -> dict[str, Any]:
    with self._lock:
      return {
          "symbol": self.symbol,
          "token": self.token,
          "last_updated_utc": self.last_updated_utc.isoformat(),
          "last_price": str(self.last_price),
          "last_quantity": self.last_quantity,
          "volume_today": self.volume_today,
          "best_bid": str(self.bids[0].price) if self.bids else "0.00",
          "best_ask": str(self.asks[0].price) if self.asks else "0.00",
          "bids": [{"price": str(b.price), "quantity": b.quantity, "orders": b.orders_count} for b in self.bids],
          "asks": [{"price": str(a.price), "quantity": a.quantity, "orders": a.orders_count} for a in self.asks],
      }


class StreamingFeedHandler:
  """High-throughput WebSocket streaming feed handler managing live subscriptions."""

  def __init__(self):
    assert_paper_mode()
    self.books: dict[str, LiveOrderBook] = {}  # token -> LiveOrderBook
    self.token_to_symbol: dict[str, str] = {}
    self.symbol_to_token: dict[str, str] = {}
    self.callbacks: list[Callable[[StreamingTickPacket, OrderBookDepth], None]] = []

    # Operational telemetry
    self.is_connected: bool = True
    self.total_frames_processed: int = 0
    self.total_bytes_processed: int = 0
    self.dropped_frames: int = 0
    self.start_time: float = time.time()
    self._latencies_ms: list[float] = []
    self._lock = threading.Lock()

  def register_instrument(self, symbol: str, token: str) -> None:
    """Registers an instrument token for live tracking."""
    self.token_to_symbol[token] = symbol
    self.symbol_to_token[symbol] = token
    if token not in self.books:
      self.books[token] = LiveOrderBook(symbol=symbol, token=token)

  def add_tick_callback(self, cb: Callable[[StreamingTickPacket, OrderBookDepth], None]) -> None:
    """Registers an asynchronous callback to receive real-time tick and depth events."""
    self.callbacks.append(cb)

  def handle_binary_frame(self, frame_bytes: bytes, arrival_time: float | None = None) -> StreamingTickPacket:
    """Parses binary frame, updates local order book, and dispatches callbacks."""
    t_start = arrival_time or time.time()
    packet = BinaryPacketParser.parse_packet(frame_bytes)

    with self._lock:
      self.total_frames_processed += 1
      self.total_bytes_processed += len(frame_bytes)

      book = self.books.get(packet.token)
      if not book:
        sym = self.token_to_symbol.get(packet.token, f"SYM_{packet.token}")
        book = LiveOrderBook(symbol=sym, token=packet.token)
        self.books[packet.token] = book

      book.update_from_tick(packet)
      depth_snapshot = book.get_snapshot()

      # Profile processing latency (ms)
      latency_ms = (time.time() - t_start) * 1000.0
      self._latencies_ms.append(latency_ms)
      if len(self._latencies_ms) > 10000:
        self._latencies_ms = self._latencies_ms[-5000:]

    # Dispatch callbacks
    for cb in self.callbacks:
      try:
        cb(packet, depth_snapshot)
      except Exception:
        pass

    return packet

  def get_telemetry(self) -> dict[str, Any]:
    """Returns streaming throughput and latency diagnostics."""
    with self._lock:
      elapsed = max(0.001, time.time() - self.start_time)
      fps = float(self.total_frames_processed) / elapsed

      if self._latencies_ms:
        sorted_l = sorted(self._latencies_ms)
        n = len(sorted_l)
        p50 = sorted_l[int(n * 0.50)]
        p95 = sorted_l[min(n - 1, int(n * 0.95))]
        p99 = sorted_l[min(n - 1, int(n * 0.99))]
      else:
        p50 = p95 = p99 = 0.0

      return {
          "is_connected": self.is_connected,
          "subscribed_instruments_count": len(self.books),
          "total_frames_processed": self.total_frames_processed,
          "total_bytes_processed": self.total_bytes_processed,
          "throughput_frames_per_sec": round(fps, 2),
          "dropped_frames": self.dropped_frames,
          "latency_p50_ms": round(p50, 4),
          "latency_p95_ms": round(p95, 4),
          "latency_p99_ms": round(p99, 4),
          "timestamp_utc": datetime.now(timezone.utc).isoformat(),
      }
