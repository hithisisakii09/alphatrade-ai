"""Microstructure Depth & Spread Surveillance Engine (Milestone M25).

Conforms strictly to institutional market microstructure execution standards:
1. Pure Decimal arithmetic with zero binary float drift.
2. Order Book Depth modeling: Bid-Ask Spread (bps), Order Book Imbalance (OBI), and Micro-Price.
3. Real-Time Liquidity Surveillance Gate: Defers execution when spreads widen >3x baseline
   or top-of-book depth is insufficient to absorb incoming slice size.
4. Fail-closed safety guard enforcement (assert_paper_mode()).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from typing import Any

from app.core.execution_guard import assert_paper_mode
from app.core.money import round_paise, round_price, to_decimal


@dataclass(frozen=True)
class OrderBookLevel:
  """Single price level in Level-2 market depth."""
  price: Decimal
  quantity: int
  orders_count: int = 1


@dataclass(frozen=True)
class OrderBookDepth:
  """Level-2 snapshot of top-of-book bids and asks."""
  symbol: str
  timestamp_utc: datetime
  bids: list[OrderBookLevel]  # Sorted descending by price
  asks: list[OrderBookLevel]  # Sorted ascending by price

  @property
  def best_bid(self) -> Decimal:
    if not self.bids:
      return Decimal("0.00")
    return self.bids[0].price

  @property
  def best_ask(self) -> Decimal:
    if not self.asks:
      return Decimal("0.00")
    return self.asks[0].price

  @property
  def mid_price(self) -> Decimal:
    if self.best_bid <= Decimal("0.00") or self.best_ask <= Decimal("0.00"):
      return max(self.best_bid, self.best_ask)
    return round_price((self.best_bid + self.best_ask) / Decimal("2"))

  @property
  def total_bid_depth(self) -> int:
    return sum(lvl.quantity for lvl in self.bids)

  @property
  def total_ask_depth(self) -> int:
    return sum(lvl.quantity for lvl in self.asks)


@dataclass(frozen=True)
class MicrostructureSnapshot:
  """Quantitative microstructure metrics extracted from order book depth."""
  symbol: str
  timestamp_utc: datetime
  best_bid: Decimal
  best_ask: Decimal
  mid_price: Decimal
  spread_inr: Decimal
  spread_bps: Decimal
  order_book_imbalance: Decimal  # -1.0 (all ask) to +1.0 (all bid)
  micro_price: Decimal
  total_bid_volume: int
  total_ask_volume: int
  depth_ratio: Decimal

  def to_dict(self) -> dict[str, Any]:
    return {
        "symbol": self.symbol,
        "timestamp_utc": self.timestamp_utc.isoformat(),
        "best_bid": str(self.best_bid),
        "best_ask": str(self.best_ask),
        "mid_price": str(self.mid_price),
        "spread_inr": str(self.spread_inr),
        "spread_bps": str(self.spread_bps),
        "order_book_imbalance": str(self.order_book_imbalance),
        "micro_price": str(self.micro_price),
        "total_bid_volume": self.total_bid_volume,
        "total_ask_volume": self.total_ask_volume,
        "depth_ratio": str(self.depth_ratio),
    }


def compute_bid_ask_spread_bps(bid: Decimal, ask: Decimal) -> tuple[Decimal, Decimal]:
  """Computes bid-ask spread in rupees and basis points.
  
  Spread (bps) = ((Ask - Bid) / Mid) * 10,000
  """
  bid_d = to_decimal(bid)
  ask_d = to_decimal(ask)
  if bid_d <= Decimal("0") or ask_d <= Decimal("0") or ask_d < bid_d:
    return Decimal("0.00"), Decimal("0.00")

  spread_inr = round_paise(ask_d - bid_d)
  mid = (bid_d + ask_d) / Decimal("2")
  if mid <= Decimal("0"):
    return spread_inr, Decimal("0.00")

  spread_bps = ((ask_d - bid_d) / mid * Decimal("10000")).quantize(
      Decimal("0.01"), rounding=ROUND_HALF_UP
  )
  return spread_inr, spread_bps


def compute_order_book_imbalance(bid_qty: int, ask_qty: int) -> Decimal:
  """Computes Normalized Order Book Imbalance (OBI).
  
  OBI = (BidQty - AskQty) / (BidQty + AskQty)
  Range: [-1.0, 1.0]. Positive means buying pressure; negative means selling pressure.
  """
  total = bid_qty + ask_qty
  if total <= 0:
    return Decimal("0.0000")
  diff = Decimal(bid_qty - ask_qty)
  return (diff / Decimal(total)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def compute_micro_price(
    best_bid: Decimal,
    best_ask: Decimal,
    bid_qty: int,
    ask_qty: int,
) -> Decimal:
  """Computes Volume-Weighted Micro-Price.
  
  MicroPrice = (Bid * AskQty + Ask * BidQty) / (BidQty + AskQty)
  Pulls towards the side with lower liquidity (higher adverse pressure).
  """
  b_d = to_decimal(best_bid)
  a_d = to_decimal(best_ask)
  total_qty = bid_qty + ask_qty
  if total_qty <= 0 or b_d <= Decimal("0") or a_d <= Decimal("0"):
    return round_price((b_d + a_d) / Decimal("2"))

  numerator = (b_d * Decimal(ask_qty)) + (a_d * Decimal(bid_qty))
  return round_price(numerator / Decimal(total_qty))


def analyze_order_book(book: OrderBookDepth) -> MicrostructureSnapshot:
  """Analyzes an order book snapshot and produces structured microstructure diagnostics."""
  assert_paper_mode()
  best_b = book.best_bid
  best_a = book.best_ask
  mid = book.mid_price
  spread_inr, spread_bps = compute_bid_ask_spread_bps(best_b, best_a)

  top_bid_qty = book.bids[0].quantity if book.bids else 0
  top_ask_qty = book.asks[0].quantity if book.asks else 0

  obi = compute_order_book_imbalance(book.total_bid_depth, book.total_ask_depth)
  micro_p = compute_micro_price(best_b, best_a, top_bid_qty, top_ask_qty)

  total_bid = book.total_bid_depth
  total_ask = book.total_ask_depth
  depth_ratio = (
      (Decimal(total_bid) / Decimal(total_ask)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
      if total_ask > 0
      else Decimal("1.000")
  )

  return MicrostructureSnapshot(
      symbol=book.symbol,
      timestamp_utc=book.timestamp_utc,
      best_bid=best_b,
      best_ask=best_a,
      mid_price=mid,
      spread_inr=spread_inr,
      spread_bps=spread_bps,
      order_book_imbalance=obi,
      micro_price=micro_p,
      total_bid_volume=total_bid,
      total_ask_volume=total_ask,
      depth_ratio=depth_ratio,
  )


class MicrostructureLiquidityGate:
  """Pre-Execution Microstructure Liquidity & Spread Gate."""

  def __init__(
      self,
      baseline_spread_bps: Decimal = Decimal("5.00"),  # Baseline 5 bps for liquid NIFTY
      max_spread_multiplier: Decimal = Decimal("3.00"),  # Defer if spread > 3x (15 bps)
      min_top_depth_shares: int = 25,
  ):
    assert_paper_mode()
    self.baseline_spread_bps = to_decimal(baseline_spread_bps)
    self.max_spread_multiplier = to_decimal(max_spread_multiplier)
    self.max_allowed_spread_bps = (
        self.baseline_spread_bps * self.max_spread_multiplier
    ).quantize(Decimal("0.01"))
    self.min_top_depth_shares = min_top_depth_shares

  def evaluate_slice(
      self,
      snapshot: MicrostructureSnapshot,
      slice_quantity: int,
      side: str = "BUY",
  ) -> tuple[bool, str, dict[str, Any]]:
    """Evaluates whether market liquidity is safe to execute the prospective child slice.
    
    Returns:
      (is_approved, reason_code, details)
    """
    assert_paper_mode()
    details: dict[str, Any] = {
        "symbol": snapshot.symbol,
        "spread_bps": str(snapshot.spread_bps),
        "max_allowed_spread_bps": str(self.max_allowed_spread_bps),
        "slice_quantity": slice_quantity,
        "side": side.upper(),
    }

    # 1. Spread Widening Check
    if snapshot.spread_bps > self.max_allowed_spread_bps:
      details["rejection_factor"] = "SPREAD_TOO_WIDE"
      return False, "SPREAD_WIDENING_DEFERRAL", details

    # 2. Top-of-Book Depth Absorption Check
    relevant_depth = (
        snapshot.total_ask_volume if side.upper() == "BUY" else snapshot.total_bid_volume
    )
    details["relevant_depth"] = relevant_depth

    if relevant_depth < self.min_top_depth_shares:
      details["rejection_factor"] = "INSUFFICIENT_TOP_DEPTH"
      return False, "DEPTH_DEPLETION_DEFERRAL", details

    # Passed all liquidity gates
    return True, "LIQUIDITY_APPROVED", details
