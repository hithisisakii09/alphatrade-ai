"""Multi-Broker Execution Gateway & Smart Order Router (SOR) Abstraction (Milestone M24 + Live Angel One Gateway).

Conforms strictly to institutional execution architecture:
1. Protocol-agnostic abstract BrokerGateway interface (Order placement, cancellation, positions, margins).
2. Pure Decimal financial arithmetic with zero binary float drift.
3. Strict fail-closed paper mode enforcement (assert_paper_mode()).
4. Multi-factor safety authorization for live execution (assert_live_mode_authorized()).
5. Concrete PaperBrokerGateway adapter integrating internal PaperBroker and PortfolioLedger.
6. Concrete MockAngelSmartAPIGateway simulating external OpenAPI broker trade streams.
7. Concrete LiveAngelSmartAPIGateway communicating with Angel One SmartAPI endpoints.
8. Multi-venue SmartOrderRouter (SOR) selecting optimal execution paths.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
import json
import logging
import os
import time
from typing import Any
import uuid
import httpx

from app.core.clock import IST, to_ist
from app.core.execution_guard import assert_live_mode_authorized, assert_paper_mode
from app.core.money import round_paise, round_price, to_decimal
from app.data.providers import ANGEL_SYMBOL_TOKENS, _RateLimiter, generate_totp

logger = logging.getLogger(__name__)


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
  product_type: str = "MIS"  # Cash Intraday MIS or NFO INTRADAY
  tag: str | None = None
  exchange: str = "NSE"  # "NSE" or "NFO"
  symbol_token: str | None = None


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
    if "LIVE" in name.upper():
      assert_live_mode_authorized()
    else:
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
    self._positions[pos.symbol] = pos

  def clear_mock_positions(self) -> None:
    self._positions.clear()
    self._used_margin = Decimal("0.00")


class LiveAngelSmartAPIGateway(BrokerGateway):
  """Live Production Execution Gateway for Angel One SmartAPI (Order Routing & RMS Ledger).

  Requires explicit two-factor authorization:
  - ALPHATRADE_EXECUTION_MODE='LIVE' or 'LIVE_SANDBOX'
  - ALPHATRADE_LIVE_CONFIRMATION='I_ACCEPT_FINANCIAL_RISK'
  """

  BASE_URL = "https://apiconnect.angelone.in"
  LOGIN_URL = "https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword"
  PLACE_ORDER_URL = "https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/placeOrder"
  CANCEL_ORDER_URL = "https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/cancelOrder"
  ORDER_BOOK_URL = "https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/getOrderBook"
  POSITIONS_URL = "https://apiconnect.angelone.in/rest/secure/angelbroking/portfolio/v1/getPosition"
  RMS_URL = "https://apiconnect.angelone.in/rest/secure/angelbroking/user/v1/getRMS"

  def __init__(
      self,
      api_key: str | None = None,
      client_code: str | None = None,
      totp_secret: str | None = None,
      password: str | None = None,
      jwt_token: str | None = None,
      name: str = "ANGEL_LIVE",
      max_order_quantity: int | None = None,
      http_client: httpx.Client | None = None,
  ):
    super().__init__(name=name)
    self.api_key = api_key or os.environ.get("ANGEL_API_KEY")
    self.client_code = client_code or os.environ.get("ANGEL_CLIENT_CODE")
    self.totp_secret = totp_secret or os.environ.get("ANGEL_TOTP_SECRET")
    self.password = password or os.environ.get("ANGEL_PASSWORD")
    self.jwt_token = jwt_token or os.environ.get("ANGEL_JWT_TOKEN")
    self.feed_token: str | None = None
    self.refresh_token: str | None = None
    self.max_order_quantity = max_order_quantity or int(os.environ.get("ALPHATRADE_LIVE_MAX_QUANTITY", "100"))
    self.fno_paper_mode_only = os.environ.get("ALPHATRADE_FNO_PAPER_ONLY", "true").lower() in ("true", "1", "yes")
    self._paper_fno_orders: dict[str, BrokerOrderResponse] = {}
    self._rate_limiter = _RateLimiter(min_interval_sec=0.35)
    self._client = http_client or httpx.Client(timeout=15.0)
    self._connected = False
    self.cache_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        ".cache",
    )
    if not self.jwt_token:
      self._load_cached_session()
    if self.jwt_token:
      self._connected = True

  def _get_client(self) -> httpx.Client:
    if not hasattr(self, "_client") or self._client is None or self._client.is_closed:
      self._client = httpx.Client(timeout=15.0)
    return self._client

  def close(self) -> None:
    if hasattr(self, "_client") and self._client and not self._client.is_closed:
      self._client.close()

  def __del__(self) -> None:
    self.close()

  def _load_cached_session(self) -> None:
    session_file = os.path.join(self.cache_dir, "angel_session.json")
    if os.path.exists(session_file):
      try:
        with open(session_file, "r") as f:
          data = json.load(f)
        today_ist = to_ist(datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        if data.get("session_date") == today_ist and data.get("client_code") == self.client_code:
          self.jwt_token = data.get("jwt_token")
          self.feed_token = data.get("feed_token")
          self.refresh_token = data.get("refresh_token")
          if self.jwt_token:
            self._connected = True
      except Exception:
        pass

  def _save_cached_session(self) -> None:
    os.makedirs(self.cache_dir, exist_ok=True)
    session_file = os.path.join(self.cache_dir, "angel_session.json")
    today_ist = to_ist(datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    payload = {
        "session_date": today_ist,
        "client_code": self.client_code,
        "jwt_token": self.jwt_token,
        "feed_token": self.feed_token,
        "refresh_token": self.refresh_token,
        "saved_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    try:
      with open(session_file, "w") as f:
        json.dump(payload, f, indent=2)
    except Exception:
      pass

  def _get_auth_headers(self) -> dict[str, str]:
    if not self.jwt_token:
      raise RuntimeError("Angel One session token missing. Must call connect() first.")
    return {
        "Authorization": f"Bearer {self.jwt_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": os.environ.get("ANGEL_CLIENT_PUBLIC_IP", "49.37.153.251"),
        "X-MACAddress": "fe80::216e:6507:4b83:30c6",
        "X-PrivateKey": self.api_key or "",
    }

  def connect(self) -> bool:
    assert_live_mode_authorized()
    if self.jwt_token and self._connected:
      return True
    if not self.client_code or not self.api_key or not self.totp_secret or not self.password:
      raise RuntimeError("Incomplete Angel One live broker credentials in environment.")

    totp = generate_totp(self.totp_secret)
    payload = {
        "clientcode": self.client_code,
        "password": self.password,
        "totp": totp,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": os.environ.get("ANGEL_CLIENT_PUBLIC_IP", "49.37.153.251"),
        "X-MACAddress": "fe80::216e:6507:4b83:30c6",
        "X-PrivateKey": self.api_key,
    }
    self._rate_limiter.wait()
    client = self._get_client()
    resp = client.post(self.LOGIN_URL, json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("status"):
      raise RuntimeError(f"Angel One login failed: {data.get('message', 'Auth rejection')}")
    toks = data.get("data", {})
    self.jwt_token = toks.get("jwtToken")
    self.feed_token = toks.get("feedToken")
    self.refresh_token = toks.get("refreshToken")
    self._connected = True
    self._save_cached_session()
    return True

  def disconnect(self) -> None:
    self._connected = False
    self.jwt_token = None
    self.close()

  def is_connected(self) -> bool:
    return self._connected and bool(self.jwt_token)

  def get_status(self) -> GatewayConnectionStatus:
    start_t = time.perf_counter()
    connected = self.is_connected()
    latency_ms = (time.perf_counter() - start_t) * 1000.0
    return GatewayConnectionStatus(
        gateway_name=self.name,
        protocol="REST",
        connected=connected,
        latency_ms=max(1.0, round(latency_ms, 2)),
        last_heartbeat_utc=datetime.now(timezone.utc),
    )

  def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResponse:
    assert_live_mode_authorized()
    now = datetime.now(timezone.utc)

    if request.quantity <= 0:
      return BrokerOrderResponse(
          broker_order_id="",
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

    if request.quantity > self.max_order_quantity:
      return BrokerOrderResponse(
          broker_order_id="",
          client_order_id=request.idempotency_key,
          symbol=request.symbol,
          side=request.side,
          quantity=request.quantity,
          filled_quantity=0,
          average_price=Decimal("0.00"),
          status="REJECTED",
          rejection_reason=f"ORDER_QTY_{request.quantity}_EXCEEDS_LIVE_SAFETY_CAP_{self.max_order_quantity}",
          timestamp_utc=now,
      )

    is_nfo = request.exchange == "NFO" or (
        request.symbol.upper().startswith("NIFTY") and any(x in request.symbol.upper() for x in ("CE", "PE", "FUT"))
    )

    # HYBRID ROUTING POLICY: Regular trading uses live Angel One wallet (5x MIS).
    # F&O trading strictly executes in paper mode to preserve live capital during testing.
    if is_nfo and self.fno_paper_mode_only:
      paper_broker_id = f"PAPER_FNO_{uuid.uuid4().hex[:12].upper()}"
      fill_price = request.price or Decimal("10.00")
      logger.info(
          "F&O HYBRID ROUTING: Preserving Angel One live wallet. Routing %s %s qty=%s to PAPER TRADING sandbox.",
          request.side,
          request.symbol,
          request.quantity,
      )
      resp = BrokerOrderResponse(
          broker_order_id=paper_broker_id,
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
      self._paper_fno_orders[paper_broker_id] = resp
      return resp

    exchange = "NFO" if is_nfo else "NSE"

    symbol_token = request.symbol_token or ANGEL_SYMBOL_TOKENS.get(request.symbol.upper())
    if not symbol_token and is_nfo:
      try:
        from app.data.option_chain import AngelOptionChainResolver
        resolver = AngelOptionChainResolver()
        contract = resolver.get_contract_by_symbol(request.symbol)
        if contract:
          symbol_token = contract.token
      except Exception as exc:
        logger.warning("Failed resolving NFO option token for %s: %s", request.symbol, exc)

    if not symbol_token:
      return BrokerOrderResponse(
          broker_order_id="",
          client_order_id=request.idempotency_key,
          symbol=request.symbol,
          side=request.side,
          quantity=request.quantity,
          filled_quantity=0,
          average_price=Decimal("0.00"),
          status="REJECTED",
          rejection_reason=f"UNKNOWN_SYMBOL_TOKEN_FOR_{request.symbol}",
          timestamp_utc=now,
      )

    ordertype = "LIMIT" if (request.order_type == "LIMIT" or request.price is not None) else "MARKET"
    price_str = f"{request.price:.2f}" if request.price is not None else "0"
    product = "INTRADAY" if request.product_type in ("MIS", "INTRADAY") else ("CARRYFORWARD" if is_nfo else "DELIVERY")

    if is_nfo:
      tradingsymbol = request.symbol.upper()
    elif request.symbol.upper() == "TATAMOTORS":
      tradingsymbol = "TMPV-EQ"
    else:
      tradingsymbol = f"{request.symbol.upper()}-EQ"

    payload = {
        "variety": "NORMAL",
        "tradingsymbol": tradingsymbol,
        "symboltoken": symbol_token,
        "transactiontype": request.side.upper(),
        "exchange": exchange,
        "ordertype": ordertype,
        "producttype": product,
        "duration": "DAY",
        "price": price_str,
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(request.quantity),
    }

    if not self.is_connected():
      self.connect()

    self._rate_limiter.wait()
    client = self._get_client()
    try:
      resp = client.post(self.PLACE_ORDER_URL, json=payload, headers=self._get_auth_headers())
      # FIX: Auto-reconnect on 401 Unauthorized (expired JWT) and retry once
      if resp.status_code == 401:
        logger.warning("Angel One JWT expired (401). Re-authenticating with fresh TOTP and retrying order...")
        self._connected = False
        self.jwt_token = None
        self.connect()
        self._rate_limiter.wait()
        resp = client.post(self.PLACE_ORDER_URL, json=payload, headers=self._get_auth_headers())
      resp.raise_for_status()
      data = resp.json()
    except Exception as e:
      logger.error(f"Angel One place_order HTTP failure for {request.symbol}: {e}")
      return BrokerOrderResponse(
          broker_order_id="",
          client_order_id=request.idempotency_key,
          symbol=request.symbol,
          side=request.side,
          quantity=request.quantity,
          filled_quantity=0,
          average_price=Decimal("0.00"),
          status="REJECTED",
          rejection_reason=f"HTTP_REQUEST_FAILED: {e}",
          timestamp_utc=now,
      )

    if data.get("status"):
      orderid = data.get("data", {}).get("orderid", "")
      logger.info(f"Angel One order ACCEPTED: symbol={request.symbol} side={request.side} qty={request.quantity} broker_order_id={orderid}")
      
      # Immediately verify if exchange / RMS rejected the order post-acceptance (e.g. Insufficient Funds)
      if orderid:
        try:
          import time
          time.sleep(0.35)
          verif = self.get_order_status(str(orderid))
          if verif:
            verif.client_order_id = request.idempotency_key
            if verif.status == "REJECTED":
              logger.error(f"Angel One order {orderid} was REJECTED post-submission by RMS: {verif.rejection_reason}")
              return verif
            elif verif.status == "FILLED":
              return verif
        except Exception as check_exc:
          logger.warning(f"Could not verify immediate order status for {orderid}: {check_exc}")

      return BrokerOrderResponse(
          broker_order_id=str(orderid),
          client_order_id=request.idempotency_key,
          symbol=request.symbol,
          side=request.side,
          quantity=request.quantity,
          filled_quantity=0,
          average_price=request.price or Decimal("0.00"),
          status="ACCEPTED",
          rejection_reason=None,
          timestamp_utc=now,
      )
    else:
      reason = data.get("message") or data.get("errorcode") or "BROKER_REJECTED"
      logger.error(f"Angel One order REJECTED: symbol={request.symbol} side={request.side} qty={request.quantity} reason={reason} raw={data}")
      return BrokerOrderResponse(
          broker_order_id="",
          client_order_id=request.idempotency_key,
          symbol=request.symbol,
          side=request.side,
          quantity=request.quantity,
          filled_quantity=0,
          average_price=Decimal("0.00"),
          status="REJECTED",
          rejection_reason=str(reason),
          timestamp_utc=now,
      )

  def cancel_order(self, broker_order_id: str) -> bool:
    assert_live_mode_authorized()
    if str(broker_order_id).startswith("PAPER_FNO_"):
      if str(broker_order_id) in self._paper_fno_orders:
        ord_resp = self._paper_fno_orders[str(broker_order_id)]
        self._paper_fno_orders[str(broker_order_id)] = BrokerOrderResponse(
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
    if not self.is_connected():
      self.connect()
    payload = {
        "variety": "NORMAL",
        "orderid": str(broker_order_id),
    }
    self._rate_limiter.wait()
    client = self._get_client()
    try:
      resp = client.post(self.CANCEL_ORDER_URL, json=payload, headers=self._get_auth_headers())
      resp.raise_for_status()
      data = resp.json()
      return bool(data.get("status"))
    except Exception:
      return False

  def get_order_status(self, broker_order_id: str) -> BrokerOrderResponse | None:
    assert_live_mode_authorized()
    if str(broker_order_id).startswith("PAPER_FNO_"):
      return self._paper_fno_orders.get(str(broker_order_id))
    if not self.is_connected():
      self.connect()
    self._rate_limiter.wait()
    client = self._get_client()
    try:
      resp = client.get(self.ORDER_BOOK_URL, headers=self._get_auth_headers())
      resp.raise_for_status()
      if not resp.text.strip():
        return None
      data = resp.json()
    except Exception:
      return None

    orders_list = data.get("data") or []
    if not isinstance(orders_list, list):
      return None
    for item in orders_list:
      if str(item.get("orderid")) == str(broker_order_id):
        raw_status = (item.get("status") or "").lower()
        if raw_status in ("complete", "filled"):
          stat = "FILLED"
        elif raw_status in ("rejected", "cancelled"):
          stat = raw_status.upper()
        else:
          stat = "ACCEPTED"

        qty = int(item.get("quantity") or 0)
        filled = int(item.get("filledshares") or 0)
        avg_price = to_decimal(item.get("averageprice") or 0)
        sym = (item.get("tradingsymbol") or "").replace("-EQ", "")

        return BrokerOrderResponse(
            broker_order_id=str(broker_order_id),
            client_order_id=item.get("clientorderid", ""),
            symbol=sym,
            side=item.get("transactiontype", "BUY"),
            quantity=qty,
            filled_quantity=filled,
            average_price=avg_price,
            status=stat,
            rejection_reason=item.get("text"),
            timestamp_utc=datetime.now(timezone.utc),
        )
    return None

  def get_positions(self) -> list[BrokerPosition]:
    assert_live_mode_authorized()
    if not self.is_connected():
      self.connect()
    self._rate_limiter.wait()
    client = self._get_client()
    try:
      resp = client.get(self.POSITIONS_URL, headers=self._get_auth_headers())
      resp.raise_for_status()
      if not resp.text.strip():
        return []
      data = resp.json()
    except Exception:
      return []

    pos_list = data.get("data") or []
    if not isinstance(pos_list, list):
      return []
    results: list[BrokerPosition] = []
    for p in pos_list:
      sym = (p.get("tradingsymbol") or "").replace("-EQ", "")
      qty = int(p.get("netqty") or 0)
      avg_p = to_decimal(p.get("netprice") or 0)
      ltp = to_decimal(p.get("ltp") or avg_p)
      pnl = to_decimal(p.get("pnl") or 0)
      prod = p.get("producttype", "MIS")
      results.append(
          BrokerPosition(
              symbol=sym,
              quantity=qty,
              average_price=avg_p,
              current_price=ltp,
              unrealized_pnl=pnl,
              product=prod,
          )
      )
    return results

  def get_margins(self) -> BrokerMarginInfo:
    assert_live_mode_authorized()
    if not self.is_connected():
      self.connect()
    self._rate_limiter.wait()
    client = self._get_client()
    try:
      resp = client.get(self.RMS_URL, headers=self._get_auth_headers())
      resp.raise_for_status()
      data = resp.json()
    except Exception as e:
      raise RuntimeError(f"Angel One getRMS query failed: {e}") from e

    rms = data.get("data") or {}
    avail_cash = round_paise(to_decimal(rms.get("availablecash") or 0))
    exp = to_decimal(rms.get("utilisedexposure") or 0)
    deb = to_decimal(rms.get("utiliseddebits") or 0)
    used_margin = round_paise(exp + deb)
    total_eq = round_paise(to_decimal(rms.get("net") or (avail_cash + used_margin)))
    collateral = round_paise(to_decimal(rms.get("collateral") or 0))

    return BrokerMarginInfo(
        available_cash=avail_cash,
        used_margin=used_margin,
        total_equity=total_eq,
        collateral=collateral,
    )


class SmartOrderRouter:
  """Institutional Multi-Broker Smart Order Router (SOR)."""

  def __init__(self, default_gateway: str = "PAPER_INTERNAL"):
    if "LIVE" in default_gateway.upper():
      assert_live_mode_authorized()
    else:
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
    target = preferred_gateway or self.default_gateway
    if "LIVE" in target.upper():
      assert_live_mode_authorized()
    else:
      assert_paper_mode()

    if preferred_gateway and preferred_gateway in self.gateways:
      gw = self.gateways[preferred_gateway]
      if gw.is_connected():
        return gw.place_order(request)

    connected = [gw for gw in self.gateways.values() if gw.is_connected()]
    if not connected:
      raise RuntimeError("No connected broker gateways available in SOR!")

    best_gw = min(connected, key=lambda g: g.get_status().latency_ms)
    return best_gw.place_order(request)
