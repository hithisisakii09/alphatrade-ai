"""Read-only market data adapters for Angel One SmartAPI, FYERS API v3, Upstox v3, and local fixtures."""

import base64
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import os
import struct
import time
from typing import Any, Protocol
import httpx

from app.core.clock import IST, to_ist
from app.core.execution_guard import assert_paper_mode, assert_safe_execution_mode
from app.data.models import CandleBar


class ProviderAuthenticationError(RuntimeError):
  """Raised when broker API credentials are missing, expired, or rejected."""


class ProviderDataError(RuntimeError):
  """Raised when broker API returns an error or invalid payload."""


def generate_totp(secret: str, digits: int = 6, interval: int = 30) -> str:
  """Generates an RFC 6238 Time-based One-Time Password (TOTP) using standard library crypto."""
  cleaned = secret.upper().replace(" ", "")
  padded = cleaned + "=" * (-len(cleaned) % 8)
  key = base64.b32decode(padded)
  counter = int(time.time()) // interval
  counter_bytes = struct.pack(">Q", counter)
  mac = hmac.new(key, counter_bytes, hashlib.sha1).digest()
  offset = mac[-1] & 0x0F
  code = (struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
  return str(code).zfill(digits)


class _RateLimiter:
  """Enforces maximum request throughput (e.g. <= 3 req/sec for Angel One)."""

  def __init__(self, min_interval_sec: float = 0.35):
    self.min_interval = min_interval_sec
    self.last_call = 0.0

  def wait(self) -> None:
    elapsed = time.time() - self.last_call
    if elapsed < self.min_interval:
      time.sleep(self.min_interval - elapsed)
    self.last_call = time.time()


# Built-in symbol token dictionary for top liquid NSE cash equities
ANGEL_SYMBOL_TOKENS: dict[str, str] = {
    "RELIANCE": "2885",
    "TCS": "11536",
    "INFY": "1594",
    "HDFCBANK": "1333",
    "ICICIBANK": "4963",
    "SBIN": "3045",
    "BHARTIARTL": "10604",
    "ITC": "1660",
    "KOTAKBANK": "1922",
    "LT": "11483",
    "AXISBANK": "5900",
    "HINDUNILVR": "1394",
    "BAJFINANCE": "317",
    "MARUTI": "10999",
    "TITAN": "3506",
    "SUNPHARMA": "3351",
    "TATAMOTORS": "3456",
    "NTPC": "11630",
    "POWERGRID": "14977",
    "TATASTEEL": "3499",
    "WIPRO": "3787",
    "HCLTECH": "7229",
    "ASIANPAINT": "236",
    "ULTRACEMCO": "11532",
}


def _parse_iso_or_ist_timestamp(raw_ts: str) -> datetime:
  raw_ts = str(raw_ts).strip()
  if "T" in raw_ts:
    ts = datetime.fromisoformat(raw_ts)
  else:
    try:
      ts = datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    except ValueError:
      ts = datetime.strptime(raw_ts, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
  return ts.astimezone(timezone.utc)


class MarketDataProvider(Protocol):
  provider_name: str

  def fetch_1m_candles(self, symbol: str, start_utc: datetime, end_utc: datetime) -> list[CandleBar]:
    ...


class AngelSmartAPIDataProvider:
  """Read-only adapter for Angel One SmartAPI Historical Candles (M0 Primary)."""

  provider_name = "ANGEL_SMARTAPI"
  BASE_URL = "https://apiconnect.angelone.in"
  LOGIN_URL = "https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword"

  def __init__(
      self,
      api_key: str | None = None,
      client_code: str | None = None,
      totp_secret: str | None = None,
      password: str | None = None,
      jwt_token: str | None = None,
  ):
    assert_safe_execution_mode()
    self.api_key = api_key or os.environ.get("ANGEL_API_KEY")
    self.client_code = client_code or os.environ.get("ANGEL_CLIENT_CODE")
    self.totp_secret = totp_secret or os.environ.get("ANGEL_TOTP_SECRET")
    self.password = password or os.environ.get("ANGEL_PASSWORD")
    self.jwt_token = jwt_token or os.environ.get("ANGEL_JWT_TOKEN")
    self.feed_token: str | None = None
    self.refresh_token: str | None = None
    self._rate_limiter = _RateLimiter(min_interval_sec=0.35)
    self._client = httpx.Client(timeout=15.0)
    self.cache_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        ".cache",
    )
    if not self.jwt_token:
      self._load_cached_session()

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

  def resolve_symbol_token(self, symbol: str) -> str | None:
    return ANGEL_SYMBOL_TOKENS.get(symbol.upper())

  def login(self, password: str | None = None) -> str:
    """Authenticates via Angel One login API using clientcode, password/MPIN, and TOTP."""
    assert_safe_execution_mode()
    pwd = password or self.password
    if not pwd:
      raise ProviderAuthenticationError(
          "ANGEL_PASSWORD (or MPIN) is required for automated Angel One session login."
      )
    if not self.client_code:
      raise ProviderAuthenticationError("ANGEL_CLIENT_CODE is required for Angel One login.")
    if not self.totp_secret:
      raise ProviderAuthenticationError("ANGEL_TOTP_SECRET is required for Angel One login.")
    if not self.api_key:
      raise ProviderAuthenticationError("ANGEL_API_KEY is required for Angel One login.")

    totp = generate_totp(self.totp_secret)
    url = self.LOGIN_URL
    payload = {
        "clientcode": self.client_code,
        "password": pwd,
        "totp": totp,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "106.193.147.98",
        "X-MACAddress": "fe80::216e:6507:4b83:30c6",
        "X-PrivateKey": self.api_key,
    }

    self._rate_limiter.wait()
    client = self._get_client()
    try:
      resp = client.post(url, json=payload, headers=headers)
      resp.raise_for_status()
      data = resp.json()
    except Exception as e:
      raise ProviderAuthenticationError(f"Angel One HTTP login failed: {e}") from e
    if not data.get("status"):
      raise ProviderAuthenticationError(
          f"Angel One login failed: {data.get('message', data.get('errorcode', 'Unknown error'))}"
      )
    tokens = data.get("data", {})
    self.jwt_token = tokens.get("jwtToken")
    self.feed_token = tokens.get("feedToken")
    self.refresh_token = tokens.get("refreshToken")
    self._save_cached_session()
    return self.jwt_token

  def parse_angel_response(self, symbol: str, raw_candles: list[list]) -> list[CandleBar]:
    """Parses Angel One getCandleData [timestamp_str, open, high, low, close, volume] records."""
    bars: list[CandleBar] = []
    for row in raw_candles:
      ts_utc = _parse_iso_or_ist_timestamp(str(row[0]))
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

  def fetch_1m_candles(
      self,
      symbol: str,
      start_utc: datetime,
      end_utc: datetime,
      symbol_token: str | None = None,
      max_retries: int = 3,
  ) -> list[CandleBar]:
    """Fetches 1-minute historical candles from Angel One getCandleData."""
    assert_safe_execution_mode()
    token = symbol_token or self.resolve_symbol_token(symbol)
    if not token:
      raise ProviderDataError(
          f"No Angel One symbol token found for symbol '{symbol}'. Use resolve_symbol_token or supply symbol_token."
      )

    if not self.jwt_token:
      if self.password:
        self.login()
      else:
        raise ProviderAuthenticationError(
            "Angel One SmartAPI authentication required: neither ANGEL_JWT_TOKEN nor ANGEL_PASSWORD provided in .env."
        )

    fromdate = to_ist(start_utc).strftime("%Y-%m-%d %H:%M")
    todate = to_ist(end_utc).strftime("%Y-%m-%d %H:%M")

    url = f"{self.BASE_URL}/rest/secure/angelbroking/historical/v1/getCandleData"
    payload = {
        "exchange": "NSE",
        "symboltoken": token,
        "interval": "ONE_MINUTE",
        "fromdate": fromdate,
        "todate": todate,
    }
    auth_header = self.jwt_token if self.jwt_token.startswith("Bearer ") else f"Bearer {self.jwt_token}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "106.193.147.98",
        "X-MACAddress": "fe80::216e:6507:4b83:30c6",
        "X-PrivateKey": self.api_key or "",
        "Authorization": auth_header,
    }

    for attempt in range(max_retries):
      self._rate_limiter.wait()
      try:
        client = self._get_client()
        resp = client.post(url, json=payload, headers=headers)
        if resp.status_code == 429 or (resp.status_code == 403 and "exceeding access rate" in resp.text.lower()):
          time.sleep(2.0 * (2**attempt))
          continue
        if resp.status_code == 401:
          if self.password and attempt == 0:
            self.login()
            headers["Authorization"] = (
                self.jwt_token if self.jwt_token.startswith("Bearer ") else f"Bearer {self.jwt_token}"
            )
            continue
          raise ProviderAuthenticationError(f"Angel SmartAPI unauthorized (401): {resp.text}")
        if resp.status_code == 403:
          raise ProviderAuthenticationError(f"Angel SmartAPI forbidden (403): {resp.text}")
        resp.raise_for_status()
        data = resp.json()
        if not data.get("status"):
          msg = str(data.get("message", data))
          err_code = str(data.get("errorcode", ""))
          if ("token" in msg.lower() or "invalid" in msg.lower() or err_code == "AG8001") and self.password and attempt == 0:
            logger.info("Angel One session token expired or invalid; re-authenticating via TOTP...")
            self.login()
            headers["Authorization"] = (
                self.jwt_token if self.jwt_token.startswith("Bearer ") else f"Bearer {self.jwt_token}"
            )
            continue
          raise ProviderDataError(f"Angel SmartAPI error: {data.get('message', data)}")
        raw_candles = data.get("data", []) or []
        return self.parse_angel_response(symbol, raw_candles)
      except httpx.HTTPError as e:
        if attempt == max_retries - 1:
          raise ProviderDataError(f"Angel SmartAPI network failure after {max_retries} attempts: {e}") from e
        time.sleep(1.0 * (2**attempt))
    return []


class FyersV3DataProvider:
  """Read-only adapter for FYERS API v3 Historical Candles (M0 Secondary)."""

  provider_name = "FYERS_V3"

  def __init__(self, app_id: str | None = None, access_token: str | None = None):
    assert_safe_execution_mode()
    self.app_id = app_id or os.environ.get("FYERS_APP_ID")
    self.access_token = access_token or os.environ.get("FYERS_ACCESS_TOKEN")

  def parse_fyers_response(self, symbol: str, raw_candles: list[list]) -> list[CandleBar]:
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

  def fetch_1m_candles(
      self, symbol: str, start_utc: datetime, end_utc: datetime, max_retries: int = 3
  ) -> list[CandleBar]:
    assert_safe_execution_mode()
    if not self.app_id or not self.access_token or "your_" in self.app_id:
      raise ProviderAuthenticationError("FYERS API credentials missing. Set FYERS_APP_ID and FYERS_ACCESS_TOKEN.")
    fyers_sym = f"NSE:{symbol}-EQ" if not symbol.startswith("NSE:") else symbol
    range_from = int(start_utc.timestamp())
    range_to = int(end_utc.timestamp())

    url = "https://api-t1.fyers.in/data/history"
    params = {
        "symbol": fyers_sym,
        "resolution": "1",
        "date_format": "0",
        "range_from": str(range_from),
        "range_to": str(range_to),
        "cont_flag": "1",
    }
    headers = {"Authorization": f"{self.app_id}:{self.access_token}"}

    for attempt in range(max_retries):
      try:
        with httpx.Client(timeout=10.0) as client:
          resp = client.get(url, params=params, headers=headers)
          if resp.status_code == 429:
            time.sleep(1.0 * (2**attempt))
            continue
          resp.raise_for_status()
          data = resp.json()
          if data.get("s") != "ok":
            raise ProviderDataError(f"FYERS API error: {data.get('message', data)}")
          return self.parse_fyers_response(symbol, data.get("candles", []))
      except httpx.HTTPError as e:
        if attempt == max_retries - 1:
          raise ProviderDataError(f"FYERS network failure after {max_retries} attempts: {e}") from e
        time.sleep(1.0 * (2**attempt))
    return []


class UpstoxV3DataProvider:
  """Read-only adapter for Upstox Developer API v3 Historical Candles (M0 Tertiary)."""

  provider_name = "UPSTOX_V3"

  def __init__(self, access_token: str | None = None):
    assert_safe_execution_mode()
    self.access_token = access_token or os.environ.get("UPSTOX_ACCESS_TOKEN")

  def parse_upstox_response(self, symbol: str, raw_candles: list[list]) -> list[CandleBar]:
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

  def fetch_1m_candles(
      self,
      symbol: str,
      start_utc: datetime,
      end_utc: datetime,
      instrument_key: str | None = None,
      max_retries: int = 3,
  ) -> list[CandleBar]:
    assert_safe_execution_mode()
    if not self.access_token or "your_" in self.access_token:
      raise ProviderAuthenticationError("Upstox credentials missing. Set UPSTOX_ACCESS_TOKEN.")
    key = instrument_key or f"NSE_EQ|{symbol}"
    from_date = start_utc.strftime("%Y-%m-%d")
    to_date = end_utc.strftime("%Y-%m-%d")

    url = f"https://api.upstox.com/v2/historical-candle/{key}/1minute/{to_date}/{from_date}"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {self.access_token}"}

    for attempt in range(max_retries):
      try:
        with httpx.Client(timeout=10.0) as client:
          resp = client.get(url, headers=headers)
          if resp.status_code == 429:
            time.sleep(0.5 * (2**attempt))
            continue
          resp.raise_for_status()
          data = resp.json()
          return self.parse_upstox_response(symbol, data.get("data", {}).get("candles", []))
      except httpx.HTTPError as e:
        if attempt == max_retries - 1:
          raise ProviderDataError(f"Upstox network failure after {max_retries} attempts: {e}") from e
        time.sleep(0.5 * (2**attempt))
    return []


class LocalCsvDataProvider:
  """Local CSV reader for offline/deterministic 1-minute historical datasets."""

  provider_name = "LOCAL_CSV"

  def __init__(self, data_dir: str = "data/historical"):
    assert_safe_execution_mode()
    self.data_dir = data_dir

  def fetch_1m_candles(self, symbol: str, start_utc: datetime, end_utc: datetime) -> list[CandleBar]:
    import csv

    csv_path = os.path.join(self.data_dir, f"{symbol}_1m.csv")
    if not os.path.exists(csv_path):
      return []
    bars: list[CandleBar] = []
    with open(csv_path, "r", encoding="utf-8") as f:
      reader = csv.DictReader(f)
      for row in reader:
        ts = datetime.fromisoformat(row["timestamp_utc"])
        if ts.tzinfo is None:
          ts = ts.replace(tzinfo=timezone.utc)
        if start_utc <= ts <= end_utc:
          bars.append(
              CandleBar(
                  symbol=symbol,
                  interval_minutes=1,
                  timestamp_utc=ts,
                  open=Decimal(str(row["open"])),
                  high=Decimal(str(row["high"])),
                  low=Decimal(str(row["low"])),
                  close=Decimal(str(row["close"])),
                  volume=int(row["volume"]),
                  source="LOCAL_CSV",
              )
          )
    return bars


def get_data_provider(provider_name: str | None = None) -> MarketDataProvider:
  name = (provider_name or os.environ.get("ALPHATRADE_DATA_PROVIDER", "ANGEL_SMARTAPI")).upper()
  if name == "ANGEL_SMARTAPI":
    return AngelSmartAPIDataProvider()
  elif name == "FYERS_V3":
    return FyersV3DataProvider()
  elif name == "UPSTOX_V3":
    return UpstoxV3DataProvider()
  elif name == "LOCAL_CSV":
    return LocalCsvDataProvider()
  else:
    raise ValueError(f"Unknown market data provider: {provider_name}")
