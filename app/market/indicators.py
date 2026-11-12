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


def compute_higher_timeframe_ema(
    bars_1m: list[CandleBar],
    htf_minutes: int = 60,
    period: int = 50,
) -> Decimal | None:
  """Aggregates 1m bars into higher timeframe bars (e.g. 60m / 1h) and returns the latest EMA(period)."""
  from app.data.validator import aggregate_1m_to_nm
  if not bars_1m:
    return None
  htf_bars = aggregate_1m_to_nm(bars_1m, htf_minutes)
  if len(htf_bars) < period:
    if len(htf_bars) >= 3:
      return round_price(sum(b.close for b in htf_bars) / Decimal(len(htf_bars)))
    return None
  closes = [b.close for b in htf_bars]
  ema_series = compute_ema_series(closes, period)
  return ema_series[-1]


def check_macro_trend_alignment(
    current_price: Decimal,
    htf_ema: Decimal | None,
    direction: str = "LONG",
) -> tuple[bool, str]:
  """Verifies whether the current breakout aligns with the higher timeframe macro trend."""
  if htf_ema is None:
    return True, "HTF_EMA_UNAVAILABLE_PERMISSIVE"
  curr = to_decimal(current_price)
  ema = to_decimal(htf_ema)
  if direction.upper() == "LONG":
    if curr >= ema:
      return True, f"MACRO_UPTREND_ALIGNED: Price {curr} >= 1h EMA {ema}"
    return False, f"COUNTER_TREND_FILTERED: Price {curr} < 1h EMA {ema}"
  else:
    if curr <= ema:
      return True, f"MACRO_DOWNTREND_ALIGNED: Price {curr} <= 1h EMA {ema}"
    return False, f"COUNTER_TREND_FILTERED: Price {curr} > 1h EMA {ema}"


def check_two_day_high_breakout(
    current_price: Decimal,
    prev_day_high: Decimal | None,
) -> tuple[bool, str]:
  """Checks if the breakout is confirmed by surpassing the previous session's high."""
  if prev_day_high is None:
    return True, "PREV_DAY_HIGH_UNAVAILABLE_PERMISSIVE"
  curr = to_decimal(current_price)
  pdh = to_decimal(prev_day_high)
  if curr > pdh:
    return True, f"TWO_DAY_HIGH_CONFIRMED: Price {curr} > Prev High {pdh}"
  return False, f"TWO_DAY_HIGH_REJECTED: Price {curr} <= Prev High {pdh}"


def compute_session_vwap_std(session_bars: list[CandleBar], vwap: Decimal) -> Decimal:
  """Calculates volume-weighted standard deviation around the session VWAP."""
  if not session_bars:
    return Decimal("0.00")
  cum_vol = sum(b.volume for b in session_bars)
  if cum_vol <= 0:
    return Decimal("0.00")
  three = Decimal("3")
  cum_sq_diff = Decimal("0")
  v = to_decimal(vwap)
  for b in session_bars:
    tp = (b.high + b.low + b.close) / three
    diff = tp - v
    cum_sq_diff += Decimal(b.volume) * (diff * diff)
  variance = cum_sq_diff / Decimal(cum_vol)
  return round_price(variance.sqrt()) if variance > Decimal("0") else Decimal("0.00")


def check_vwap_breakout_exhaustion(
    current_price: Decimal,
    vwap: Decimal,
    vwap_std: Decimal | None,
    max_multiplier: Decimal = Decimal("2.5"),
) -> tuple[bool, str]:
  """Rejects breakouts that occur when price is statistically over-extended above session VWAP."""
  if vwap_std is None or vwap_std <= Decimal("0"):
    return True, "VWAP_STD_UNAVAILABLE_PERMISSIVE"
  curr = to_decimal(current_price)
  v = to_decimal(vwap)
  std = to_decimal(vwap_std)
  mult = to_decimal(max_multiplier)
  cap = round_price(v + (mult * std))
  if curr > cap:
    return False, f"EXHAUSTION_BAND_EXCEEDED: Price {curr} > Upper {mult}σ Band {cap} (VWAP={v}, Std={std})"
  return True, f"VWAP_BAND_OK: Price {curr} <= Upper {mult}σ Band {cap}"
