"""High-performance in-memory live caching layer for market candles and indicator states."""

from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any

from app.core.clock import to_ist
from app.data.models import CandleBar
from app.market.indicators import OpeningRangeState, compute_opening_range, compute_session_vwap

logger = logging.getLogger(__name__)


class LiveCandleCache:
  """Thread-safe in-memory cache for live intraday candles and derived indicators.
  
  Prevents redundant API roundtrips by tracking the latest timestamp per symbol
  and only requesting incremental 1m bars from the market data provider.
  """

  def __init__(self, orb_minutes: int = 15):
    self.orb_minutes = orb_minutes
    self._session_date: str | None = None
    self._bars_by_symbol: dict[str, list[CandleBar]] = {}
    self._orb_by_symbol: dict[str, OpeningRangeState] = {}
    self._vwap_by_symbol: dict[str, Decimal] = {}
    self._hits = 0
    self._misses = 0

  def check_session_rollover(self, current_session_date: str) -> None:
    """Clears in-memory candle cache if the market date rolls over to a new session."""
    if self._session_date != current_session_date:
      logger.info(f"LiveCandleCache rolling over from session {self._session_date} to {current_session_date}")
      self._session_date = current_session_date
      self._bars_by_symbol.clear()
      self._orb_by_symbol.clear()
      self._vwap_by_symbol.clear()
      self._hits = 0
      self._misses = 0

  def get_bars(self, symbol: str) -> list[CandleBar]:
    """Returns cached 1m candle bars for the symbol."""
    if symbol in self._bars_by_symbol:
      self._hits += 1
      return list(self._bars_by_symbol[symbol])
    self._misses += 1
    return []

  def get_last_timestamp(self, symbol: str) -> datetime | None:
    """Returns the most recent UTC timestamp cached for this symbol."""
    bars = self._bars_by_symbol.get(symbol)
    if bars:
      return bars[-1].timestamp_utc
    return None

  def update_bars(self, symbol: str, new_bars: list[CandleBar]) -> int:
    """Appends new unique 1-minute bars, keeps chronological order, and updates indicators."""
    if not new_bars:
      return 0

    if symbol not in self._bars_by_symbol:
      self._bars_by_symbol[symbol] = []

    existing_bars = self._bars_by_symbol[symbol]
    existing_ts = {b.timestamp_utc for b in existing_bars}

    added_count = 0
    for b in new_bars:
      if b.timestamp_utc not in existing_ts:
        existing_bars.append(b)
        existing_ts.add(b.timestamp_utc)
        added_count += 1

    if added_count > 0:
      existing_bars.sort(key=lambda b: b.timestamp_utc)
      # Recompute or update cached indicators
      self._orb_by_symbol[symbol] = compute_opening_range(existing_bars, self.orb_minutes)
      self._vwap_by_symbol[symbol] = compute_session_vwap(existing_bars)

    return added_count

  def get_orb_state(self, symbol: str) -> OpeningRangeState | None:
    """Returns the cached 15m Opening Range state for the symbol."""
    return self._orb_by_symbol.get(symbol)

  def get_vwap(self, symbol: str) -> Decimal | None:
    """Returns the cached session VWAP for the symbol."""
    return self._vwap_by_symbol.get(symbol)

  def get_latest_price(self, symbol: str) -> Decimal | None:
    """Returns the latest close price for the symbol."""
    bars = self._bars_by_symbol.get(symbol)
    if bars:
      return bars[-1].close
    return None

  def get_cache_stats(self) -> dict[str, Any]:
    """Returns cache diagnostic metrics."""
    total_bars = sum(len(b) for b in self._bars_by_symbol.values())
    symbols_cached = len(self._bars_by_symbol)
    total_reqs = self._hits + self._misses
    hit_rate = (self._hits / total_reqs * 100) if total_reqs > 0 else 0.0
    return {
        "session_date": self._session_date,
        "symbols_cached": symbols_cached,
        "total_bars_cached": total_bars,
        "cache_hits": self._hits,
        "cache_misses": self._misses,
        "hit_rate_pct": round(hit_rate, 2),
    }
