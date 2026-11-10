"""Candle ingestion validator and 1m -> 5m / 15m deterministic bar aggregator."""

from datetime import datetime, timezone
from decimal import Decimal
from app.core.clock import ensure_utc, to_ist
from app.data.models import CandleBar, DataQualityEvent


class CandleIngestionValidator:
  """Validates 1m OHLCV candles, detects duplicates/out-of-order/stale/split anomalies."""

  def __init__(self, max_overnight_jump_pct: Decimal = Decimal("0.15"), max_staleness_seconds: int = 90):
    self.max_overnight_jump_pct = max_overnight_jump_pct
    self.max_staleness_seconds = max_staleness_seconds
    self.last_timestamp_by_symbol: dict[str, datetime] = {}
    self.last_close_by_symbol: dict[str, tuple[str, Decimal]] = {}
    self.quarantined_symbols: set[str] = set()
    self.events: list[DataQualityEvent] = []

  def validate_bar(
      self,
      bar: CandleBar,
      reference_clock_utc: datetime | None = None,
  ) -> tuple[bool, list[DataQualityEvent]]:
    bar_events: list[DataQualityEvent] = []
    sym = bar.symbol
    ts = ensure_utc(bar.timestamp_utc)

    # 1. OHLCV sanity checks
    if bar.open <= Decimal("0") or bar.high <= Decimal("0") or bar.low <= Decimal("0") or bar.close <= Decimal("0"):
      ev = DataQualityEvent(
          symbol=sym, timestamp_utc=ts, event_type="NON_POSITIVE_PRICE", details="OHLC values must be > 0"
      )
      bar_events.append(ev)
    if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(bar.open, bar.close, bar.high):
      ev = DataQualityEvent(
          symbol=sym,
          timestamp_utc=ts,
          event_type="INVALID_OHLC_ENVELOPE",
          details=f"O={bar.open} H={bar.high} L={bar.low} C={bar.close} violates High/Low envelope",
      )
      bar_events.append(ev)
    if bar.volume < 0:
      ev = DataQualityEvent(
          symbol=sym, timestamp_utc=ts, event_type="NEGATIVE_VOLUME", details=f"Volume={bar.volume} < 0"
      )
      bar_events.append(ev)

    # 2. Duplicate and out-of-order timestamp detection
    prev_ts = self.last_timestamp_by_symbol.get(sym)
    if prev_ts is not None:
      if ts == prev_ts:
        bar_events.append(
            DataQualityEvent(
                symbol=sym, timestamp_utc=ts, event_type="DUPLICATE_CANDLE", details=f"Duplicate timestamp {ts.isoformat()}"
            )
        )
      elif ts < prev_ts:
        bar_events.append(
            DataQualityEvent(
                symbol=sym,
                timestamp_utc=ts,
                event_type="OUT_OF_ORDER_CANDLE",
                details=f"Timestamp {ts.isoformat()} < previous {prev_ts.isoformat()}",
            )
        )

    # 3. Stale feed detection when reference clock is provided
    if reference_clock_utc is not None:
      ref_utc = ensure_utc(reference_clock_utc)
      delay_sec = (ref_utc - ts).total_seconds()
      if delay_sec > self.max_staleness_seconds:
        bar_events.append(
            DataQualityEvent(
                symbol=sym,
                timestamp_utc=ts,
                event_type="STALE_MARKET_DATA",
                details=f"Candle age {delay_sec:.1f}s exceeds max {self.max_staleness_seconds}s",
            )
        )

    # 4. Overnight >15% corporate-action / split jump quarantine
    session_date = to_ist(ts).date().isoformat()
    prev_session_info = self.last_close_by_symbol.get(sym)
    if prev_session_info is not None:
      prev_date, prev_close = prev_session_info
      if session_date != prev_date and prev_close > Decimal("0"):
        jump_pct = abs(bar.open - prev_close) / prev_close
        if jump_pct > self.max_overnight_jump_pct:
          self.quarantined_symbols.add(sym)
          bar_events.append(
              DataQualityEvent(
                  symbol=sym,
                  timestamp_utc=ts,
                  event_type="SUSPECTED_UNADJUSTED_CORPORATE_ACTION",
                  details=(
                      f"Overnight open {bar.open} jumped {jump_pct * 100:.2f}% from prior close {prev_close}; "
                      "symbol quarantined pending split/bonus verification."
                  ),
              )
          )

    if bar_events:
      self.events.extend(bar_events)
      return False, bar_events

    self.last_timestamp_by_symbol[sym] = ts
    self.last_close_by_symbol[sym] = (session_date, bar.close)
    return True, []


def aggregate_1m_to_nm(bars_1m: list[CandleBar], n_minutes: int = 5) -> list[CandleBar]:
  """Aggregates validated 1-minute bars into n-minute bars without look-ahead."""
  if not bars_1m:
    return []
  grouped: dict[tuple[str, datetime], list[CandleBar]] = {}
  for b in bars_1m:
    dt_ist = to_ist(b.timestamp_utc)
    # Floor minute to n_minutes boundary aligned from 09:15 IST
    minutes_since_open = (dt_ist.hour * 60 + dt_ist.minute) - (9 * 60 + 15)
    bucket_idx = minutes_since_open // n_minutes
    bucket_start_min = (9 * 60 + 15) + bucket_idx * n_minutes
    bh, bm = divmod(bucket_start_min, 60)
    bucket_ist = dt_ist.replace(hour=bh, minute=bm, second=0, microsecond=0)
    bucket_utc = bucket_ist.astimezone(timezone.utc)
    grouped.setdefault((b.symbol, bucket_utc), []).append(b)

  aggregated: list[CandleBar] = []
  for (sym, b_utc), chunk in sorted(grouped.items(), key=lambda x: x[0][1]):
    chunk_sorted = sorted(chunk, key=lambda c: c.timestamp_utc)
    if len(chunk_sorted) < n_minutes:
      # Incomplete bar — do not emit premature close
      continue
    aggregated.append(
        CandleBar(
            symbol=sym,
            interval_minutes=n_minutes,
            timestamp_utc=b_utc,
            open=chunk_sorted[0].open,
            high=max(c.high for c in chunk_sorted),
            low=min(c.low for c in chunk_sorted),
            close=chunk_sorted[-1].close,
            volume=sum(c.volume for c in chunk_sorted),
            source=chunk_sorted[0].source,
        )
    )
  return aggregated
