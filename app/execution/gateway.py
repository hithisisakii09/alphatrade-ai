"""Multi-Broker Execution Gateway & Smart Order Router (SOR) Abstraction (Milestone M24).

Conforms strictly to institutional execution architecture:
1. Protocol-agnostic abstract BrokerGateway interface (Order placement, cancellation, positions, margins).
2. Pure Decimal financial arithmetic with zero binary float drift.
3. Strict fail-closed paper mode enforcement (assert_paper_mode()).
4. Concrete PaperBrokerGateway adapter integrating internal PaperBroker and PortfolioLedger.
5. Concrete MockAngelSmartAPIGateway simulating external OpenAPI broker trade streams.
6. Multi-venue SmartOrderRouter (SOR) selecting optimal execution paths.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
import time
from typing import Any
import uuid

from app.core.execution_guard import assert_paper_mode
from app.core.money import round_paise, round_price, to_decimal


@dataclass(frozen=True)
class BrokerOrderRequest:
  """Standardized outbound order request."""
  symbol: str
  side: str  # "BUY" or "SELL"
  quantity: int
  order_type: str = "MARKET"  # "MARKET", "LIMIT", "SL-M"
  price: Decimal | None = None
  trigger_price: Decimal | None = None
  idempotency_key: str = field(default_factory=lambda: f"IDEMP_{uuid.uuid4().hex[:12]}")
  product_type: str = "MIS"  # Cash Intraday MIS
  tag: str | None = None


@dataclass(frozen=True)
class BrokerOrderResponse:
  """Standardized broker order placement / lifecycle response."""
  broker_order_id: str
  client_order_id: str
  symbol: str
  side: str
  quantity: int
  filled_quantity: int
  average_price: Decimal
  status: str  # "SUBMITTED", "ACCEPTED", "REJECTED", "FILLED", "CANCELLED"
  rejection_reason: str | None
  timestamp_utc: datetime


@dataclass(frozen=True)
class BrokerPosition:
  """Position record reported by broker."""
  symbol: str
  quantity: int
  average_price: Decimal
  current_price: Decimal
  unrealized_pnl: Decimal
  product: str = "MIS"


@dataclass(frozen=True)
class BrokerMarginInfo:
  """Account margin utilization reported by broker."""
  available_cash: Decimal
  used_margin: Decimal
  total_equity: Decimal
  collateral: Decimal = Decimal("0.00")


@dataclass(frozen=True)
class GatewayConnectionStatus:
  """Health and protocol connection status of a broker gateway."""
  gateway_name: str
  protocol: str  # "IN_PROCESS", "REST", "FIX"
  connected: bool
  latency_ms: float
  last_heartbeat_utc: datetime


class BrokerGateway(ABC):
  """Abstract Protocol-Agnostic Broker Execution Gateway."""

  def __init__(self, name: str):
    assert_paper_mode()
    self.name = name

  @abstractmethod
  def connect(self) -> bool:
    """Establishes connection / session with broker."""
    pass

  @abstractmethod
  def disconnect(self) -> None:
    """Terminates session."""
    pass

  @abstractmethod
  def is_connected(self) -> bool:
    """Returns True if gateway is active and authenticated."""
    pass

  @abstractmethod
  def get_status(self) -> GatewayConnectionStatus:
    """Returns connection diagnostics and latency."""
    pass

  @abstractmethod
  def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResponse:
    """Places an order with the broker."""
    pass

  @abstractmethod
  def cancel_order(self, broker_order_id: str) -> bool:
    """Cancels an open order."""
    pass

  @abstractmethod
  def get_order_status(self, broker_order_id: str) -> BrokerOrderResponse | None:
    """Fetches order lifecycle state."""
    pass

  @abstractmethod
  def get_positions(self) -> list[BrokerPosition]:
    """Fetches open position book from broker."""
    pass

  @abstractmethod
  def get_margins(self) -> BrokerMarginInfo:
    """Fetches real-time margin ledger from broker."""
    pass


class PaperBrokerGateway(BrokerGateway):
  """Adapter connecting the internal PaperBroker and PortfolioLedger to BrokerGateway."""

  def __init__(self, paper_broker: Any = None, ledger: Any = None, name: str = "PAPER_INTERNAL"):
    super().__init__(name=name)
    self.paper_broker = paper_broker
    self.ledger = ledger
    self._connected = True
    self._orders: dict[str, BrokerOrderResponse] = {}

  def connect(self) -> bool:
    assert_paper_mode()
    self._connected = True
    return True

  def disconnect(self) -> None:
    self._connected = False

  def is_connected(self) -> bool:
    return self._connected

  def get_status(self) -> GatewayConnectionStatus:
    return GatewayConnectionStatus(
        gateway_name=self.name,
        protocol="IN_PROCESS",
        connected=self._connected,
        latency_ms=0.25,
        last_heartbeat_utc=datetime.now(timezone.utc),
    )

  def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResponse:
    assert_paper_mode()
    now = datetime.now(timezone.utc)
    broker_id = f"PB_ORD_{uuid.uuid4().hex[:10]}"

    if request.quantity <= 0:
      resp = BrokerOrderResponse(
          broker_order_id=broker_id,
          client_order_id=request.idempotency_key,
          symbol=request.symbol,
          side=request.side,
          quantity=request.quantity,
          filled_quantity=0,
          average_price=Decimal("0.00"),
          status="REJECTED",
          rejection_reason="QUANTITY_MUST_BE_POSITIVE",
          timestamp_utc=now,
      )
      self._orders[broker_id] = resp
      return resp

    fill_price = request.price or Decimal("1000.00")
    resp = BrokerOrderResponse(
        broker_order_id=broker_id,
        client_order_id=request.idempotency_key,
        symbol=request.symbol,
        side=request.side,
        quantity=request.quantity,
        filled_quantity=request.quantity,
        average_price=fill_price,
        status="FILLED",
        rejection_reason=None,
        timestamp_utc=now,
    )
    self._orders[broker_id] = resp
    return resp

  def cancel_order(self, broker_order_id: str) -> bool:
    if broker_order_id in self._orders:
      ord_resp = self._orders[broker_order_id]
      if ord_resp.status in ("SUBMITTED", "ACCEPTED"):
        self._orders[broker_order_id] = BrokerOrderResponse(
            broker_order_id=ord_resp.broker_order_id,
            client_order_id=ord_resp.client_order_id,
            symbol=ord_resp.symbol,
            side=ord_resp.side,
            quantity=ord_resp.quantity,
            filled_quantity=0,
            average_price=Decimal("0.00"),
            status="CANCELLED",
            rejection_reason="CANCELLED_BY_USER",
            timestamp_utc=datetime.now(timezone.utc),
        )
        return True
    return False

  def get_order_status(self, broker_order_id: str) -> BrokerOrderResponse | None:
    return self._orders.get(broker_order_id)

  def get_positions(self) -> list[BrokerPosition]:
    if self.ledger is None:
      return []
    res: list[BrokerPosition] = []
    # If ledger has open positions
    open_positions = self.ledger.get_open_positions() if hasattr(self.ledger, "get_open_positions") else {}
    for sym, pos in open_positions.items():
      qty = getattr(pos, "quantity", 0)
      avg_p = getattr(pos, "entry_price", Decimal("0.00"))
      curr_p = getattr(pos, "current_price", avg_p)
      pnl = getattr(pos, "unrealized_pnl", Decimal("0.00"))
      res.append(
          BrokerPosition(
              symbol=sym,
              quantity=qty,
              average_price=to_decimal(avg_p),
              current_price=to_decimal(curr_p),
              unrealized_pnl=to_decimal(pnl),
              product="MIS",
          )
      )
    return res

  def get_margins(self) -> BrokerMarginInfo:
    if self.ledger is None:
      return BrokerMarginInfo(
          available_cash=Decimal("100000.00"),
          used_margin=Decimal("0.00"),
          total_equity=Decimal("100000.00"),
      )
    cash = self.ledger.get_cash_balance()
    deployed = self.ledger.get_deployed_exposure()
    unrealized = self.ledger.get_daily_unrealized_pnl()
    total = cash + deployed + unrealized
    # MIS margin is 20% of deployed exposure
    used_margin = round_paise(deployed * Decimal("0.20"))
    return BrokerMarginInfo(
        available_cash=cash,
        used_margin=used_margin,
        total_equity=total,
    )


class MockAngelSmartAPIGateway(BrokerGateway):
  """Simulated Angel One SmartAPI Protocol Gateway for multi-venue testing."""

  def __init__(self, name: str = "ANGEL_SMARTAPI_MOCK"):
    super().__init__(name=name)
    self._connected = True
    self._orders: dict[str, BrokerOrderResponse] = {}
    self._positions: dict[str, BrokerPosition] = {}
    self._cash: Decimal = Decimal("100000.00")
    self._used_margin: Decimal = Decimal("0.00")

  def connect(self) -> bool:
    assert_paper_mode()
    self._connected = True
    return True

  def disconnect(self) -> None:
    self._connected = False

  def is_connected(self) -> bool:
    return self._connected

  def get_status(self) -> GatewayConnectionStatus:
    return GatewayConnectionStatus(
        gateway_name=self.name,
        protocol="REST",
        connected=self._connected,
        latency_ms=12.50,
        last_heartbeat_utc=datetime.now(timezone.utc),
    )

  def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResponse:
    assert_paper_mode()
    now = datetime.now(timezone.utc)
    broker_id = f"ANGEL_{uuid.uuid4().hex[:10]}"

    if request.quantity <= 0:
      resp = BrokerOrderResponse(
          broker_order_id=broker_id,
          client_order_id=request.idempotency_key,
          symbol=request.symbol,
          side=request.side,
          quantity=request.quantity,
          filled_quantity=0,
          average_price=Decimal("0.00"),
          status="REJECTED",
          rejection_reason="INVALID_ORDER_QUANTITY",
          timestamp_utc=now,
      )
      self._orders[broker_id] = resp
      return resp

    fill_price = request.price or Decimal("1000.00")
    resp = BrokerOrderResponse(
        broker_order_id=broker_id,
        client_order_id=request.idempotency_key,
        symbol=request.symbol,
        side=request.side,
        quantity=request.quantity,
        filled_quantity=request.quantity,
        average_price=fill_price,
        status="FILLED",
        rejection_reason=None,
        timestamp_utc=now,
    )
    self._orders[broker_id] = resp

    # Update simulated positions and margins
    if request.side.upper() == "BUY":
      existing = self._positions.get(request.symbol)
      if existing:
        new_qty = existing.quantity + request.quantity
        new_avg = round_price((existing.average_price * existing.quantity + fill_price * request.quantity) / Decimal(new_qty))
        self._positions[request.symbol] = BrokerPosition(
            symbol=request.symbol,
            quantity=new_qty,
            average_price=new_avg,
            current_price=fill_price,
            unrealized_pnl=Decimal("0.00"),
        )
      else:
        self._positions[request.symbol] = BrokerPosition(
            symbol=request.symbol,
            quantity=request.quantity,
            average_price=fill_price,
            current_price=fill_price,
            unrealized_pnl=Decimal("0.00"),
        )
      # Margin deduction (20% MIS)
      margin_needed = round_paise(Decimal(request.quantity) * fill_price * Decimal("0.20"))
      self._used_margin += margin_needed
      self._cash = max(Decimal("0.00"), self._cash - margin_needed)

    return resp

  def cancel_order(self, broker_order_id: str) -> bool:
    if broker_order_id in self._orders:
      ord_resp = self._orders[broker_order_id]
      if ord_resp.status in ("SUBMITTED", "ACCEPTED"):
        self._orders[broker_order_id] = BrokerOrderResponse(
            broker_order_id=ord_resp.broker_order_id,
            client_order_id=ord_resp.client_order_id,
            symbol=ord_resp.symbol,
            side=ord_resp.side,
            quantity=ord_resp.quantity,
            filled_quantity=0,
            average_price=Decimal("0.00"),
            status="CANCELLED",
            rejection_reason="CANCELLED_BY_BROKER",
            timestamp_utc=datetime.now(timezone.utc),
        )
        return True
    return False

  def get_order_status(self, broker_order_id: str) -> BrokerOrderResponse | None:
    return self._orders.get(broker_order_id)

  def get_positions(self) -> list[BrokerPosition]:
    return list(self._positions.values())

  def get_margins(self) -> BrokerMarginInfo:
    return BrokerMarginInfo(
        available_cash=self._cash,
        used_margin=self._used_margin,
        total_equity=self._cash + self._used_margin,
    )

  def inject_mock_position(self, pos: BrokerPosition) -> None:
    """Helper to inject synthetic discrepancy for testing."""
    self._positions[pos.symbol] = pos

  def clear_mock_positions(self) -> None:
    self._positions.clear()
    self._used_margin = Decimal("0.00")


class SmartOrderRouter:
  """Institutional Multi-Broker Smart Order Router (SOR)."""

  def __init__(self, default_gateway: str = "PAPER_INTERNAL"):
    assert_paper_mode()
    self.gateways: dict[str, BrokerGateway] = {}
    self.default_gateway = default_gateway

  def register_gateway(self, gateway: BrokerGateway) -> None:
    self.gateways[gateway.name] = gateway

  def get_gateway(self, name: str | None = None) -> BrokerGateway:
    target = name or self.default_gateway
    if target not in self.gateways:
      raise KeyError(f"Gateway '{target}' not registered in SOR. Available: {list(self.gateways.keys())}")
    return self.gateways[target]

  def route_order(
      self,
      request: BrokerOrderRequest,
      preferred_gateway: str | None = None,
  ) -> BrokerOrderResponse:
    """Selects best connected gateway and routes order."""
    assert_paper_mode()
    if preferred_gateway and preferred_gateway in self.gateways:
      gw = self.gateways[preferred_gateway]
      if gw.is_connected():
        return gw.place_order(request)

    # Fallback to default or lowest latency connected gateway
    connected = [gw for gw in self.gateways.values() if gw.is_connected()]
    if not connected:
      raise RuntimeError("No connected broker gateways available in SOR!")

    best_gw = min(connected, key=lambda g: g.get_status().latency_ms)
    return best_gw.place_order(request)
