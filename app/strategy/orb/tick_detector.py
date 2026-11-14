"""Streaming Tick Breakout Detector & Real-Time Strategy Evaluation (Milestone M26).

Conforms strictly to institutional sub-second execution standards:
1. Pure Decimal arithmetic with zero binary float drift.
2. Evaluates streaming ticks immediately against 15m Opening Range thresholds.
3. Integrates M25 MicrostructureLiquidityGate (rejects triggers if spread > 3x baseline).
4. Integrates M19 HMM Volatility Regime Gate (rejects triggers during BEAR regimes).
5. Sub-millisecond tick-to-signal reaction latency profiling.
6. Enforces strict fail-closed paper execution guard (assert_paper_mode()).
"""

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from decimal import Decimal
import time as pytime
from typing import Any
import uuid

from app.core.clock import to_ist
from app.core.execution_guard import assert_paper_mode
from app.core.money import round_price, to_decimal
from app.data.streaming import StreamingTickPacket
from app.market.microstructure import (
    MicrostructureLiquidityGate,
    OrderBookDepth,
    analyze_order_book,
)


@dataclass(frozen=True)
class OpeningRangeLevels:
  """15-minute Opening Range levels established between 09:15 and 09:30 IST."""
  symbol: str
  session_date: str
  orh: Decimal  # Opening Range High
  orl: Decimal  # Opening Range Low
  vwap: Decimal
  is_formed: bool = True
  rvol: Decimal = Decimal("1.50")
  macro_trend_aligned: bool = True


@dataclass(frozen=True)
class StreamingBreakoutSignal:
  """Prospective trade signal emitted immediately upon sub-second tick breakout."""
  signal_id: str
  symbol: str
  side: str  # "BUY"
  trigger_price: Decimal
  orh: Decimal
  tick_timestamp_utc: datetime
  detection_timestamp_utc: datetime
  latency_ms: float
  spread_bps: Decimal
  order_book_imbalance: Decimal
  regime: str
  status: str = "TRIGGERED"

  def to_dict(self) -> dict[str, Any]:
    return {
        "signal_id": self.signal_id,
        "symbol": self.symbol,
        "side": self.side,
        "trigger_price": str(self.trigger_price),
        "orh": str(self.orh),
        "tick_timestamp_utc": self.tick_timestamp_utc.isoformat(),
        "detection_timestamp_utc": self.detection_timestamp_utc.isoformat(),
        "latency_ms": round(self.latency_ms, 4),
        "spread_bps": str(self.spread_bps),
        "order_book_imbalance": str(self.order_book_imbalance),
        "regime": self.regime,
        "status": self.status,
    }


class StreamingTickBreakoutDetector:
  """Evaluates live ticks against Opening Range and Market Microstructure in real time."""

  def __init__(
      self,
      liquidity_gate: MicrostructureLiquidityGate | None = None,
      allowed_regimes: list[str] | None = None,
      entry_start_ist: str = "09:30:00",
      entry_cutoff_ist: str = "14:30:00",
  ):
    assert_paper_mode()
    self.liquidity_gate = liquidity_gate or MicrostructureLiquidityGate()
    self.allowed_regimes = allowed_regimes or ["BULL", "NEUTRAL"]
    self.entry_start = time.fromisoformat(entry_start_ist)
    self.entry_cutoff = time.fromisoformat(entry_cutoff_ist)

    self.levels: dict[str, OpeningRangeLevels] = {}  # symbol -> OpeningRangeLevels
    self.triggered_symbols: set[str] = set()
    self.signals: list[StreamingBreakoutSignal] = []
    self.current_regimes: dict[str, str] = {}  # symbol -> regime (BULL, NEUTRAL, BEAR)

  def register_opening_range(self, levels: OpeningRangeLevels) -> None:
    """Registers pre-computed Opening Range levels for a symbol."""
    self.levels[levels.symbol] = levels

  def set_regime(self, symbol: str, regime: str) -> None:
    """Updates the prevailing HMM volatility regime for a symbol."""
    self.current_regimes[symbol] = regime.upper()

  def reset_session(self) -> None:
    """Resets intraday trigger state at market rollover."""
    self.triggered_symbols.clear()
    self.signals.clear()

  def on_tick(
      self,
      packet: StreamingTickPacket,
      depth: OrderBookDepth,
      arrival_time: float | None = None,
  ) -> tuple[StreamingBreakoutSignal | None, str]:
    """Processes an incoming streaming tick.
    
    Returns:
      (signal, reason_code)
    """
    assert_paper_mode()
    t_start = arrival_time or pytime.time()
    sym = depth.symbol

    # 1. Verify symbol has established Opening Range
    if sym not in self.levels:
      return None, "NO_OPENING_RANGE"

    lvl = self.levels[sym]
    if not lvl.is_formed:
      return None, "OPENING_RANGE_NOT_FORMED"

    # 2. Check if already triggered today (strictly 1 entry per constituent)
    if sym in self.triggered_symbols:
      return None, "ALREADY_TRIGGERED_TODAY"

    # 3. Time Window Gate (09:30 to 14:30 IST)
    ist_time = to_ist(packet.exchange_timestamp_utc).time()
    if ist_time < self.entry_start:
      return None, "BEFORE_ENTRY_WINDOW"
    if ist_time > self.entry_cutoff:
      return None, "AFTER_ENTRY_CUTOFF"

    # 4. Breakout Condition: Tick Price > Opening Range High (ORH)
    ltp = packet.last_traded_price
    if ltp <= lvl.orh:
      return None, "PRICE_BELOW_ORH"

    # 5. Volatility Regime Gate (M19 HMM: Reject BEAR)
    regime = self.current_regimes.get(sym, "NEUTRAL")
    if regime not in self.allowed_regimes:
      return None, f"ADVERSE_REGIME:{regime}"

    # 6. Microstructure Liquidity & Spread Gate (M25)
    micro_snap = analyze_order_book(depth)
    is_liquid, reason, _ = self.liquidity_gate.evaluate_slice(
        snapshot=micro_snap,
        slice_quantity=100,
        side="BUY",
    )
    if not is_liquid:
      return None, f"LIQUIDITY_GATE_REJECTED:{reason}"

    # 7. Valid Breakout Signal Triggered!
    now_utc = datetime.now(timezone.utc)
    latency_ms = (pytime.time() - t_start) * 1000.0

    sig = StreamingBreakoutSignal(
        signal_id=f"SIG_TICK_{sym}_{uuid.uuid4().hex[:8]}",
        symbol=sym,
        side="BUY",
        trigger_price=ltp,
        orh=lvl.orh,
        tick_timestamp_utc=packet.exchange_timestamp_utc,
        detection_timestamp_utc=now_utc,
        latency_ms=latency_ms,
        spread_bps=micro_snap.spread_bps,
        order_book_imbalance=micro_snap.order_book_imbalance,
        regime=regime,
        status="TRIGGERED",
    )

    self.triggered_symbols.add(sym)
    self.signals.append(sig)
    return sig, "BREAKOUT_TRIGGERED"
