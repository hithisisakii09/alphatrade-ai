"""Pydantic data contracts for market candles, instrument metadata, and quality events (compatible with Pydantic v1 & v2)."""

from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, validator
from app.core.clock import ensure_utc
from app.core.money import to_decimal


class CandleBar(BaseModel):
  symbol: str
  interval_minutes: int = 1
  timestamp_utc: datetime
  open: Decimal
  high: Decimal
  low: Decimal
  close: Decimal
  volume: int
  source: str = "BROKER_API"

  @validator("open", "high", "low", "close", pre=True)
  def validate_decimal_no_float(cls, v):
    return to_decimal(v)

  @validator("timestamp_utc", pre=True)
  def validate_tz(cls, v):
    if isinstance(v, str):
      v = datetime.fromisoformat(v)
    return ensure_utc(v)


class InstrumentMeta(BaseModel):
  symbol: str
  exchange: str = "NSE"
  isin: str
  tick_size: Decimal = Decimal("0.05")
  price_band_pct: Decimal = Decimal("10.0")
  is_fno: bool = True
  median_daily_turnover_cr: Decimal = Decimal("100.0")
  eligible: bool = True
  prev_close: Decimal | None = None

  @validator("tick_size", "price_band_pct", "median_daily_turnover_cr", pre=True)
  def validate_dec(cls, v):
    return to_decimal(v)


class DataQualityEvent(BaseModel):
  symbol: str
  timestamp_utc: datetime
  event_type: str
  severity: str = "ERROR"
  details: str
