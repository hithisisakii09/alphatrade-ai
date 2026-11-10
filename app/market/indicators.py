"""Deterministic, zero-look-ahead indicator engine: Session VWAP, EMA, Wilder ATR(14), Time-of-Day RVOL, and Opening Range."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from statistics import median
from app.core.clock import to_ist
from app.core.money import round_price, to_decimal
from app.data.models import CandleBar


@dataclass(frozen=True)
class OpeningRangeState:
  symbol: str
  session_date: str
  orb_high: Decimal
  orb_low: Decimal
  orb_volume: int
  is_complete: bool


@dataclass(frozen=True)
class IndicatorSnapshot:
  symbol: str
  timestamp_utc: datetime
  vwap: Decimal
  ema_9: Decimal | None
  ema_20: Decimal | None
  atr_14: Decimal | None
  rvol: Decimal
  orb_high: Decimal | None
  orb_low: Decimal | None


def compute_session_vwap(session_bars: list[CandleBar]) -> Decimal:
  """Calculates session-aware cumulative VWAP using Typical Price = (H + L + C) / 3."""
  if not session_bars:
    raise ValueError("Cannot compute VWAP on empty session bars")
  cum_pv = Decimal("0")
  cum_vol = 0
  three = Decimal("3")
  for b in session_bars:
    if b.volume < 0:
      raise ValueError("Negative volume in VWAP calculation")
    typical_price = (b.high + b.low + b.close) / three
    cum_pv += typical_price * Decimal(b.volume)
    cum_vol += b.volume
  if cum_vol == 0:
    return round_price(session_bars[-1].close)
  return round_price(cum_pv / Decimal(cum_vol))


def compute_ema_series(closes: list[Decimal], period: int) -> list[Decimal | None]:
  """Computes EMA with explicit SMA warm-up seed. Returns None before period is reached."""
  if period <= 0:
    raise ValueError("EMA period must be > 0")
  out: list[Decimal | None] = []
  multiplier = Decimal("2") / Decimal(period + 1)
  ema_prev: Decimal | None = None
  for idx, c_raw in enumerate(closes):
    c = to_decimal(c_raw)
    if idx + 1 < period:
      out.append(None)
    elif idx + 1 == period:
      seed = sum(to_decimal(x) for x in closes[:period]) / Decimal(period)
      ema_prev = round_price(seed)
      out.append(ema_prev)
    else:
      assert ema_prev is not None
      ema_curr = round_price((c - ema_prev) * multiplier + ema_prev)
      ema_prev = ema_curr
      out.append(ema_curr)
  return out


def compute_atr_series(bars: list[CandleBar], period: int = 14) -> list[Decimal | None]:
  """Computes Wilder's ATR(period) series without look-ahead."""
  if period <= 0:
    raise ValueError("ATR period must be > 0")
  trs: list[Decimal] = []
  out: list[Decimal | None] = []
  atr_prev: Decimal | None = None
  for idx, b in enumerate(bars):
    if idx == 0:
      tr = b.high - b.low
    else:
      prev_c = bars[idx - 1].close
      tr = max(b.high - b.low, abs(b.high - prev_c), abs(b.low - prev_c))
    trs.append(tr)
    if idx + 1 < period:
      out.append(None)
    elif idx + 1 == period:
      atr_prev = round_price(sum(trs[:period]) / Decimal(period))
      out.append(atr_prev)
    else:
      assert atr_prev is not None
      atr_curr = round_price(((atr_prev * Decimal(period - 1)) + tr) / Decimal(period))
      atr_prev = atr_curr
      out.append(atr_curr)
  return out


def compute_opening_range(session_bars_1m: list[CandleBar], orb_minutes: int = 15) -> OpeningRangeState | None:
  """Computes the Opening Range (09:15 to 09:15 + orb_minutes IST) from 1-minute bars."""
  if not session_bars_1m:
    return None
  orb_bars: list[CandleBar] = []
  session_date = ""
  for b in session_bars_1m:
    dt_ist = to_ist(b.timestamp_utc)
    session_date = dt_ist.date().isoformat()
    mins_from_open = (dt_ist.hour * 60 + dt_ist.minute) - (9 * 60 + 15)
    if 0 <= mins_from_open < orb_minutes:
      orb_bars.append(b)
  if not orb_bars:
    return None
  is_complete = len(orb_bars) >= orb_minutes
  return OpeningRangeState(
      symbol=session_bars_1m[0].symbol,
      session_date=session_date,
      orb_high=max(b.high for b in orb_bars),
      orb_low=min(b.low for b in orb_bars),
      orb_volume=sum(b.volume for b in orb_bars),
      is_complete=is_complete,
  )


def compute_time_of_day_rvol(
    current_cum_volume: int,
    historical_same_minute_cum_volumes: list[int],
) -> Decimal:
  """Computes Relative Volume (RVOL) against historical cumulative volumes at the exact same IST minute."""
  valid_hist = [v for v in historical_same_minute_cum_volumes if v > 0]
  if not valid_hist:
    return Decimal("1.0000")
  med_vol = Decimal(str(median(valid_hist)))
  if med_vol <= Decimal("0"):
    return Decimal("1.0000")
  return round_price(Decimal(current_cum_volume) / med_vol)
