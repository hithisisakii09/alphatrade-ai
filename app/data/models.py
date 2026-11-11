"""Pydantic data contracts for market candles, instrument metadata, and quality events."""

from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, field_validator
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

  @field_validator("open", "high", "low", "close", mode="before")
  @classmethod
  def validate_decimal_no_float(cls, v):
    return to_decimal(v)

  @field_validator("timestamp_utc", mode="before")
  @classmethod
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

  @field_validator("tick_size", "price_band_pct", "median_daily_turnover_cr", mode="before")
  @classmethod
  def validate_dec(cls, v):
    return to_decimal(v)


class DataQualityEvent(BaseModel):
  symbol: str
  timestamp_utc: datetime
  event_type: str
  severity: str = "ERROR"
  details: str
