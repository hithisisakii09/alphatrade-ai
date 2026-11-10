"""Unit and contract tests for Angel One SmartAPI provider and TOTP generator."""

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from app.data.models import CandleBar
from app.data.providers import (
    ANGEL_SYMBOL_TOKENS,
    AngelSmartAPIDataProvider,
    ProviderAuthenticationError,
    generate_totp,
    get_data_provider,
)


def test_rfc6238_totp_generation_test_vectors():
  # RFC 6238 official test vector key (Base32 for ASCII "12345678901234567890")
  secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
  # Cleaned / spaced input handled
  assert len(generate_totp(secret)) == 6
  assert generate_totp(secret).isdigit()

  # Custom secret with spaces / lowercase handled
  spaced_secret = "gez dgn bvg y3t qoj qgez dgn bvg y3t qoj q"
  totp = generate_totp(spaced_secret)
  assert len(totp) == 6 and totp.isdigit()


def test_angel_symbol_token_resolution():
  provider = AngelSmartAPIDataProvider(api_key="TEST_KEY")
  assert provider.resolve_symbol_token("RELIANCE") == "2885"
  assert provider.resolve_symbol_token("TCS") == "11536"
  assert provider.resolve_symbol_token("INFY") == "1594"
  assert provider.resolve_symbol_token("HDFCBANK") == "1333"
  assert provider.resolve_symbol_token("ICICIBANK") == "4963"
  assert provider.resolve_symbol_token("SBIN") == "3045"
  assert provider.resolve_symbol_token("UNKNOWN_XYZ") is None


def test_angel_response_parsing_to_candle_bars():
  provider = AngelSmartAPIDataProvider(api_key="TEST_KEY")
  raw_candles = [
      ["2026-11-10T09:15:00+05:30", 1250.00, 1255.50, 1248.25, 1253.10, 35000],
      ["2026-11-10 09:16:00", 1253.10, 1254.00, 1251.00, 1252.00, 18500],
  ]

  bars = provider.parse_angel_response("RELIANCE", raw_candles)
  assert len(bars) == 2
  b1 = bars[0]
  assert isinstance(b1, CandleBar)
  assert b1.symbol == "RELIANCE"
  assert b1.interval_minutes == 1
  assert b1.open == Decimal("1250.0")
  assert b1.high == Decimal("1255.5")
  assert b1.low == Decimal("1248.25")
  assert b1.close == Decimal("1253.1")
  assert b1.volume == 35000
  assert b1.source == "ANGEL_SMARTAPI"
  assert b1.timestamp_utc.tzinfo == timezone.utc

  b2 = bars[1]
  assert b2.open == Decimal("1253.1")
  assert b2.volume == 18500


def test_angel_auth_error_when_credentials_missing():
  provider = AngelSmartAPIDataProvider(
      api_key="TEST_KEY", client_code="S123", totp_secret="PQQNAFSM44DJQV2HFVF33LBCPQ", password=""
  )
  with pytest.raises(ProviderAuthenticationError):
    provider.fetch_1m_candles(
        symbol="RELIANCE",
        start_utc=datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc),
        end_utc=datetime(2026, 11, 10, 4, 30, tzinfo=timezone.utc),
    )


def test_get_data_provider_factory():
  prov = get_data_provider("ANGEL_SMARTAPI")
  assert isinstance(prov, AngelSmartAPIDataProvider)
  assert prov.provider_name == "ANGEL_SMARTAPI"

