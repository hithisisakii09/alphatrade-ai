"""Unit and contract tests for LiveAngelSmartAPIGateway and live authorization safety interlocks."""

from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import httpx
import pytest

from app.core.execution_guard import (
    LiveTradingProhibitedError,
    LiveTradingUnauthorizedError,
    assert_live_mode_authorized,
    assert_paper_mode,
    is_live_mode_authorized,
)
from app.execution.gateway import (
    BrokerMarginInfo,
    BrokerOrderRequest,
    BrokerOrderResponse,
    BrokerPosition,
    LiveAngelSmartAPIGateway,
    SmartOrderRouter,
)


def test_live_gateway_fails_closed_in_paper_mode(monkeypatch):
  """Without live environment authorization, LiveAngelSmartAPIGateway must refuse to instantiate."""
  monkeypatch.setenv("ALPHATRADE_EXECUTION_MODE", "PAPER")
  if hasattr(monkeypatch, "delenv"):
    monkeypatch.delenv("ALPHATRADE_LIVE_CONFIRMATION", raising=False)
  else:
    os.environ.pop("ALPHATRADE_LIVE_CONFIRMATION", None)

  assert not is_live_mode_authorized()
  with pytest.raises(LiveTradingUnauthorizedError) as exc_info:
    LiveAngelSmartAPIGateway(api_key="TEST", client_code="S123")
  assert "LIVE GATEWAY REJECTION" in str(exc_info.value)


def test_live_gateway_fails_closed_without_confirmation(monkeypatch):
  """Setting execution mode to LIVE without the explicit risk acknowledgment must fail closed."""
  monkeypatch.setenv("ALPHATRADE_EXECUTION_MODE", "LIVE")
  if hasattr(monkeypatch, "delenv"):
    monkeypatch.delenv("ALPHATRADE_LIVE_CONFIRMATION", raising=False)
  else:
    os.environ.pop("ALPHATRADE_LIVE_CONFIRMATION", None)

  assert not is_live_mode_authorized()
  with pytest.raises(LiveTradingUnauthorizedError) as exc_info:
    assert_live_mode_authorized()
  assert "Missing two-factor financial safety acknowledgment" in str(exc_info.value)

  with pytest.raises(LiveTradingUnauthorizedError):
    LiveAngelSmartAPIGateway(api_key="TEST", client_code="S123")


def test_live_gateway_authorized_with_proper_two_factor_env(monkeypatch):
  """When both mode and confirmation match institutional requirements, authorization succeeds."""
  monkeypatch.setenv("ALPHATRADE_EXECUTION_MODE", "LIVE")
  monkeypatch.setenv("ALPHATRADE_LIVE_CONFIRMATION", "I_ACCEPT_FINANCIAL_RISK")

  assert is_live_mode_authorized()
  assert assert_live_mode_authorized() == "LIVE"


def test_live_gateway_order_quantity_safety_cap(monkeypatch):
  """Orders exceeding max_order_quantity cap must be rejected fail-closed before network dispatch."""
  monkeypatch.setenv("ALPHATRADE_EXECUTION_MODE", "LIVE")
  monkeypatch.setenv("ALPHATRADE_LIVE_CONFIRMATION", "I_ACCEPT_FINANCIAL_RISK")

  gateway = LiveAngelSmartAPIGateway(
      api_key="TEST",
      client_code="S123",
      totp_secret="TESTSECRET",
      password="1234",
      jwt_token="MOCK_JWT",
      max_order_quantity=5,
  )

  # 1. Zero quantity rejected
  req_zero = BrokerOrderRequest(symbol="RELIANCE", side="BUY", quantity=0)
  resp_zero = gateway.place_order(req_zero)
  assert resp_zero.status == "REJECTED"
  assert resp_zero.rejection_reason == "QUANTITY_MUST_BE_POSITIVE"

  # 2. Exceeding safety cap rejected
  req_large = BrokerOrderRequest(symbol="RELIANCE", side="BUY", quantity=10, price=Decimal("2500.00"))
  resp_large = gateway.place_order(req_large)
  assert resp_large.status == "REJECTED"
  assert "EXCEEDS_LIVE_SAFETY_CAP" in (resp_large.rejection_reason or "")

  # 3. Unknown symbol token rejected
  req_unknown = BrokerOrderRequest(symbol="UNKNOWN_XYZ", side="BUY", quantity=1, price=Decimal("100.00"))
  resp_unknown = gateway.place_order(req_unknown)
  assert resp_unknown.status == "REJECTED"
  assert "UNKNOWN_SYMBOL_TOKEN" in (resp_unknown.rejection_reason or "")


def test_live_gateway_mocked_order_placement_and_cancel(monkeypatch):
  """Verifies successful order placement, status tracking, and cancellation with Angel One API responses."""
  monkeypatch.setenv("ALPHATRADE_EXECUTION_MODE", "LIVE")
  monkeypatch.setenv("ALPHATRADE_LIVE_CONFIRMATION", "I_ACCEPT_FINANCIAL_RISK")

  # Mock HTTP client responses
  def mock_handler(request: httpx.Request) -> httpx.Response:
    url_str = str(request.url)
    if "placeOrder" in url_str:
      body = json.loads(request.content.decode("utf-8"))
      assert body["tradingsymbol"] == "SBIN-EQ"
      assert body["symboltoken"] == "3045"
      assert body["quantity"] == "1"
      assert body["transactiontype"] == "BUY"
      return httpx.Response(
          200,
          json={
              "status": True,
              "message": "SUCCESS",
              "errorcode": "",
              "data": {"orderid": "ANGEL_LIVE_12345678", "uniqueorderid": "UQ_123"},
          },
      )
    elif "cancelOrder" in url_str:
      body = json.loads(request.content.decode("utf-8"))
      assert body["orderid"] == "ANGEL_LIVE_12345678"
      return httpx.Response(200, json={"status": True, "message": "SUCCESS", "errorcode": "", "data": {}})
    elif "getOrderBook" in url_str:
      return httpx.Response(
          200,
          json={
              "status": True,
              "message": "SUCCESS",
              "errorcode": "",
              "data": [
                  {
                      "orderid": "ANGEL_LIVE_12345678",
                      "clientorderid": "CL_001",
                      "tradingsymbol": "SBIN-EQ",
                      "transactiontype": "BUY",
                      "quantity": "1",
                      "filledshares": "1",
                      "averageprice": "825.50",
                      "status": "complete",
                      "text": "",
                  }
              ],
          },
      )
    elif "getPosition" in url_str:
      return httpx.Response(
          200,
          json={
              "status": True,
              "message": "SUCCESS",
              "errorcode": "",
              "data": [
                  {
                      "tradingsymbol": "SBIN-EQ",
                      "netqty": "1",
                      "netprice": "825.50",
                      "ltp": "830.00",
                      "pnl": "4.50",
                      "producttype": "INTRADAY",
                  }
              ],
          },
      )
    elif "getRMS" in url_str:
      return httpx.Response(
          200,
          json={
              "status": True,
              "message": "SUCCESS",
              "errorcode": "",
              "data": {
                  "availablecash": "49500.25",
                  "utilisedexposure": "2500.00",
                  "utiliseddebits": "0.00",
                  "net": "52000.25",
                  "collateral": "0.00",
              },
          },
      )
    return httpx.Response(404, json={"status": False, "message": "Not Found"})

  transport = httpx.MockTransport(mock_handler)
  client = httpx.Client(transport=transport)

  gateway = LiveAngelSmartAPIGateway(
      api_key="TEST_API_KEY",
      client_code="S123456",
      totp_secret="PQQNAFSM44DJQV2HFVF33LBCPQ",
      password="1234",
      jwt_token="VALID_MOCK_JWT",
      http_client=client,
      max_order_quantity=10,
  )

  # 1. Place 1-share limit order
  req = BrokerOrderRequest(
      symbol="SBIN",
      side="BUY",
      quantity=1,
      order_type="LIMIT",
      price=Decimal("825.50"),
      idempotency_key="IDEMP_TEST_001",
  )
  resp = gateway.place_order(req)
  assert resp.status in ("ACCEPTED", "FILLED")
  assert resp.broker_order_id == "ANGEL_LIVE_12345678"
  assert resp.client_order_id == "IDEMP_TEST_001"
  assert resp.quantity == 1

  # 2. Query order status
  order_stat = gateway.get_order_status("ANGEL_LIVE_12345678")
  assert order_stat is not None
  assert order_stat.status == "FILLED"
  assert order_stat.average_price == Decimal("825.50")
  assert order_stat.filled_quantity == 1

  # 3. Query positions
  positions = gateway.get_positions()
  assert len(positions) == 1
  pos = positions[0]
  assert pos.symbol == "SBIN"
  assert pos.quantity == 1
  assert pos.average_price == Decimal("825.50")
  assert pos.current_price == Decimal("830.00")
  assert pos.unrealized_pnl == Decimal("4.50")

  # 4. Query RMS margins
  margins = gateway.get_margins()
  assert isinstance(margins, BrokerMarginInfo)
  assert margins.available_cash == Decimal("49500.25")
  assert margins.used_margin == Decimal("2500.00")
  assert margins.total_equity == Decimal("52000.25")

  # 5. Cancel order
  assert gateway.cancel_order("ANGEL_LIVE_12345678") is True


def test_smart_order_router_routes_to_live_gateway(monkeypatch):
  """SmartOrderRouter correctly registers and routes to live gateway when authorized."""
  monkeypatch.setenv("ALPHATRADE_EXECUTION_MODE", "LIVE")
  monkeypatch.setenv("ALPHATRADE_LIVE_CONFIRMATION", "I_ACCEPT_FINANCIAL_RISK")

  def mock_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"status": True, "message": "SUCCESS", "errorcode": "", "data": {"orderid": "ROUTED_ORD_999"}},
    )

  client = httpx.Client(transport=httpx.MockTransport(mock_handler))
  live_gw = LiveAngelSmartAPIGateway(
      api_key="KEY",
      client_code="S1",
      jwt_token="JWT",
      http_client=client,
      name="ANGEL_LIVE",
  )

  sor = SmartOrderRouter(default_gateway="ANGEL_LIVE")
  sor.register_gateway(live_gw)

  req = BrokerOrderRequest(symbol="TCS", side="BUY", quantity=1, price=Decimal("3500.00"))
  routed_resp = sor.route_order(req)
  assert routed_resp.status == "ACCEPTED"
  assert routed_resp.broker_order_id == "ROUTED_ORD_999"
