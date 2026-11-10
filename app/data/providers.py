"""Read-only market data adapters for FYERS API v3, Upstox v3, Angel One SmartAPI, and local fixtures."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol
from app.core.execution_guard import assert_paper_mode
from app.data.models import CandleBar


class MarketDataProvider(Protocol):
  provider_name: str

  def fetch_1m_candles(self, symbol: str, start_utc: datetime, end_utc: datetime) -> list[CandleBar]:
    ...


class FyersV3DataProvider:
  """Read-only adapter for FYERS API v3 Historical Candles (pre-cleared M0 primary)."""

  provider_name = "FYERS_V3"

  def __init__(self, app_id: str | None = None, access_token: str | None = None):
    assert_paper_mode()
    self.app_id = app_id
    self.access_token = access_token

  def parse_fyers_response(self, symbol: str, raw_candles: list[list]) -> list[CandleBar]:
    """Parses FYERS v3 [epoch_sec, open, high, low, close, volume] records into validated CandleBar objects."""
    bars: list[CandleBar] = []
    for row in raw_candles:
      ts_utc = datetime.fromtimestamp(int(row[0]), tz=timezone.utc)
      bars.append(
          CandleBar(
              symbol=symbol,
              interval_minutes=1,
              timestamp_utc=ts_utc,
              open=Decimal(str(row[1])),
              high=Decimal(str(row[2])),
              low=Decimal(str(row[3])),
              close=Decimal(str(row[4])),
              volume=int(row[5]),
              source=self.provider_name,
          )
      )
    return bars


class UpstoxV3DataProvider:
  """Read-only adapter for Upstox Developer API v3 Historical Candles (pre-cleared M0 secondary)."""

  provider_name = "UPSTOX_V3"

  def __init__(self, access_token: str | None = None):
    assert_paper_mode()
    self.access_token = access_token

  def parse_upstox_response(self, symbol: str, raw_candles: list[list]) -> list[CandleBar]:
    """Parses Upstox v3 [iso_timestamp, open, high, low, close, volume, oi] records."""
    bars: list[CandleBar] = []
    for row in raw_candles:
      ts_utc = datetime.fromisoformat(str(row[0])).astimezone(timezone.utc)
      bars.append(
          CandleBar(
              symbol=symbol,
              interval_minutes=1,
              timestamp_utc=ts_utc,
              open=Decimal(str(row[1])),
              high=Decimal(str(row[2])),
              low=Decimal(str(row[3])),
              close=Decimal(str(row[4])),
              volume=int(row[5]),
              source=self.provider_name,
          )
      )
    return bars


class AngelSmartAPIDataProvider:
  """Read-only adapter for Angel One SmartAPI getCandleData (pre-cleared M0 tertiary)."""

  provider_name = "ANGEL_SMARTAPI"

  def __init__(self, api_key: str | None = None):
    assert_paper_mode()
    self.api_key = api_key
