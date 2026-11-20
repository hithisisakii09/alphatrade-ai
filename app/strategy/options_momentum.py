"""High-Velocity NIFTY Index Options Momentum Strategy.

Executes systematic Opening Range Breakout (ORB) on NIFTY 50 index options,
selecting optimal OTM Call/Put contracts, managing asymmetric risk-to-reward
with automated trailing profit locks and strict EOD liquidation.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, time
from decimal import Decimal
import logging
from typing import Literal

from app.core.clock import IST, to_ist
from app.data.option_chain import AngelOptionChainResolver, OptionContract
from app.execution.gateway import BrokerOrderRequest

logger = logging.getLogger(__name__)


@dataclass
class OptionPositionTracker:
  """Tracks an active option position, trailing stop, and profit milestones."""
  position_id: str
  symbol: str
  token: str
  option_type: Literal["CE", "PE"]
  strike: Decimal
  expiry: str
  quantity: int  # e.g. 65 shares (1 lot)
  entry_price: Decimal
  entry_time: datetime
  status: Literal["OPEN", "CLOSED"] = "OPEN"
  exit_price: Decimal | None = None
  exit_time: datetime | None = None
  exit_reason: str | None = None
  realized_pnl: Decimal = Decimal("0.00")

  # Dynamic Risk Bounds
  initial_stop_price: Decimal = Decimal("0.00")
  current_stop_price: Decimal = Decimal("0.00")
  target_price_1: Decimal = Decimal("0.00")
  trailing_activation_price: Decimal = Decimal("0.00")
  trailing_activated: bool = False
  highest_price_since_entry: Decimal = Decimal("0.00")

  def __post_init__(self):
    if self.initial_stop_price == Decimal("0.00"):
      # Hard stop loss: -25% of premium
      self.initial_stop_price = (self.entry_price * Decimal("0.75")).quantize(Decimal("0.05"))
      self.current_stop_price = self.initial_stop_price
    if self.target_price_1 == Decimal("0.00"):
      # Target 1: +50% of premium (+₹250+ on 1 lot)
      self.target_price_1 = (self.entry_price * Decimal("1.50")).quantize(Decimal("0.05"))
    if self.trailing_activation_price == Decimal("0.00"):
      # Trailing activates at +30% profit
      self.trailing_activation_price = (self.entry_price * Decimal("1.30")).quantize(Decimal("0.05"))
    if self.highest_price_since_entry == Decimal("0.00"):
      self.highest_price_since_entry = self.entry_price

  def update_mark_price(self, current_price: Decimal) -> None:
    """Updates peak tracking and ratchets trailing stop."""
    if current_price > self.highest_price_since_entry:
      self.highest_price_since_entry = current_price

    # Check trailing activation
    if not self.trailing_activated and current_price >= self.trailing_activation_price:
      self.trailing_activated = True
      # Move stop immediately to breakeven (entry price)
      self.current_stop_price = max(self.current_stop_price, self.entry_price)
      logger.info(
          "NIFTY Option Trailing Activated for %s at Rs.%.2f. Stop moved to Breakeven (Rs.%.2f)",
          self.symbol, current_price, self.entry_price,
      )

    # If trailing is active, trail 10% below highest peak
    if self.trailing_activated:
      trail_stop = (self.highest_price_since_entry * Decimal("0.90")).quantize(Decimal("0.05"))
      if trail_stop > self.current_stop_price:
        self.current_stop_price = trail_stop
        logger.info(
            "NIFTY Option Stop Ratcheted Higher for %s: Rs.%.2f (Peak: Rs.%.2f)",
            self.symbol, self.current_stop_price, self.highest_price_since_entry,
        )

  def check_exit_trigger(self, current_price: Decimal, current_time_ist: time) -> str | None:
    """Evaluates whether current price or time mandates an immediate exit."""
    # 1. Mandatory 15:10 IST EOD auto-liquidation
    if current_time_ist >= time(15, 10):
      return "EOD_FORCED_LIQUIDATION_1510"

    # 2. Hard or Trailing Stop Loss Hit
    if current_price <= self.current_stop_price:
      if self.trailing_activated:
        return "TRAILING_PROFIT_STOP_HIT"
      return "INITIAL_STOP_LOSS_HIT"

    # 3. Super Target Hit (+100% gain)
    super_target = (self.entry_price * Decimal("2.00")).quantize(Decimal("0.05"))
    if current_price >= super_target:
      return "SUPER_TARGET_100PCT_GAIN_HIT"

    return None


class NiftyOptionsMomentumEngine:
  """Systematic Intraday NIFTY Options Breakout Trading Engine."""

  def __init__(
      self,
      resolver: AngelOptionChainResolver | None = None,
      max_daily_trades: int = 2,
      max_risk_per_trade_inr: Decimal = Decimal("200.00"),
      max_budget_per_trade_inr: Decimal = Decimal("850.00"),
  ):
    self.resolver = resolver or AngelOptionChainResolver()
    self.max_daily_trades = max_daily_trades
    self.max_risk_per_trade_inr = max_risk_per_trade_inr
    self.max_budget_per_trade_inr = max_budget_per_trade_inr

    # Opening Range State
    self.orb_high: Decimal | None = None
    self.orb_low: Decimal | None = None
    self.orb_formed: bool = False

    # Daily Session Tracking
    self.session_trades_count: int = 0
    self.session_realized_pnl: Decimal = Decimal("0.00")
    self.active_position: OptionPositionTracker | None = None
    self.trade_history: list[OptionPositionTracker] = []

  def update_opening_range(self, candles: list[dict]) -> bool:
    """Computes ORB high and low from 09:15 to 09:30 IST candles."""
    orb_candles = []
    for c in candles:
      ts_ist = to_ist(c["timestamp_utc"])
      t = ts_ist.time()
      if time(9, 15) <= t < time(9, 30):
        orb_candles.append(c)

    if len(orb_candles) < 10:  # Need adequate 1m bars
      return False

    self.orb_high = max(Decimal(str(c["high"])) for c in orb_candles)
    self.orb_low = min(Decimal(str(c["low"])) for c in orb_candles)
    self.orb_formed = True
    logger.info("NIFTY ORB Formed: High=Rs.%.2f, Low=Rs.%.2f", self.orb_high, self.orb_low)
    return True

  def evaluate_breakout(
      self,
      current_spot_price: Decimal,
      current_candle_close: Decimal,
      current_rvol: float,
      current_time_utc: datetime,
  ) -> BrokerOrderRequest | None:
    """Evaluates 1-minute candle breakout against NIFTY ORB boundaries."""
    # Gates: Already in trade, max daily trades exceeded, or ORB not formed
    if self.active_position is not None:
      return None
    if self.session_trades_count >= self.max_daily_trades:
      return None
    if not self.orb_formed or self.orb_high is None or self.orb_low is None:
      return None

    current_time_ist = to_ist(current_time_utc).time()
    # Entry cutoff: no entries after 14:30 IST
    if current_time_ist < time(9, 30) or current_time_ist >= time(14, 30):
      return None

    # Check Relative Volume (RVOL) filter
    if current_rvol < 1.15:
      return None

    signal_direction: Literal["CE", "PE"] | None = None

    # Bullish Breakout
    if current_candle_close > self.orb_high:
      signal_direction = "CE"
    # Bearish Breakdown
    elif current_candle_close < self.orb_low:
      signal_direction = "PE"

    if not signal_direction:
      return None

    # Resolve target option contract (1-2 strikes OTM for high gamma & low cost)
    contract = self.resolver.resolve_strike(
        underlying="NIFTY",
        option_type=signal_direction,
        spot_price=float(current_spot_price),
        offset_strikes=1,
    )
    if not contract:
      logger.warning("No valid NIFTY %s option contract found.", signal_direction)
      return None

    logger.info(
        "NIFTY %s Breakout Detected @ Spot Rs.%.2f. Selected Contract: %s (Token: %s, Lotsize: %d)",
        signal_direction, current_spot_price, contract.symbol, contract.token, contract.lotsize,
    )

    # Build order request
    order_req = BrokerOrderRequest(
        symbol=contract.symbol,
        side="BUY",
        quantity=contract.lotsize,
        order_type="MARKET",
        exchange="NFO",
        symbol_token=contract.token,
        product_type="INTRADAY",
        tag=f"NIFTY_OPT_ORB_{signal_direction}",
    )
    return order_req

  def on_order_filled(
      self,
      contract: OptionContract,
      fill_price: Decimal,
      fill_quantity: int,
      fill_time_utc: datetime,
  ) -> OptionPositionTracker:
    """Initializes tracking for a filled option order."""
    pos = OptionPositionTracker(
        position_id=f"NOPT_{fill_time_utc.strftime('%H%M%S')}_{contract.symbol}",
        symbol=contract.symbol,
        token=contract.token,
        option_type=contract.option_type,  # type: ignore[arg-type]
        strike=contract.strike,
        expiry=contract.expiry,
        quantity=fill_quantity,
        entry_price=fill_price,
        entry_time=fill_time_utc,
    )
    self.active_position = pos
    self.session_trades_count += 1
    logger.info(
        "NIFTY Option Position Opened: %s | Qty: %d @ Rs.%.2f | Stop: Rs.%.2f | Target: Rs.%.2f",
        pos.symbol, pos.quantity, pos.entry_price, pos.current_stop_price, pos.target_price_1,
    )
    return pos

  def on_position_closed(
      self,
      exit_price: Decimal,
      exit_reason: str,
      exit_time_utc: datetime,
  ) -> OptionPositionTracker | None:
    """Closes and records an active option trade."""
    pos = self.active_position
    if not pos:
      return None

    pos.status = "CLOSED"
    pos.exit_price = exit_price
    pos.exit_reason = exit_reason
    pos.exit_time = exit_time_utc
    pos.realized_pnl = ((exit_price - pos.entry_price) * Decimal(pos.quantity)).quantize(Decimal("0.05"))

    self.session_realized_pnl += pos.realized_pnl
    self.trade_history.append(pos)
    self.active_position = None

    logger.info(
        "NIFTY Option Position Closed: %s | Exit: Rs.%.2f (%s) | PnL: Rs.%.2f",
        pos.symbol, exit_price, exit_reason, pos.realized_pnl,
    )
    return pos
