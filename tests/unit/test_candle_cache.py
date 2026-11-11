"""Unit tests for LiveCandleCache in-memory performance and incremental updates."""

from datetime import datetime, timezone
from decimal import Decimal

from app.core.clock import ist_datetime
from app.data.cache import LiveCandleCache
from app.data.models import CandleBar


def _make_bar(symbol: str, hour: int, minute: int, price: Decimal, vol: int = 100) -> CandleBar:
  ts = ist_datetime(2026, 11, 11, hour, minute)
  return CandleBar(
      symbol=symbol,
      interval_minutes=1,
      timestamp_utc=ts,
      open=price,
      high=price + Decimal("1.00"),
      low=price - Decimal("1.00"),
      close=price,
      volume=vol,
      source="TEST",
  )


def test_live_candle_cache_lifecycle_and_deduplication():
  cache = LiveCandleCache(orb_minutes=15)
  cache.check_session_rollover("2026-11-11")

  # 1. Miss on empty cache
  assert cache.get_bars("RELIANCE") == []
  assert cache.get_last_timestamp("RELIANCE") is None

  # 2. Add first batch (09:15 to 09:20)
  bars_batch1 = [_make_bar("RELIANCE", 9, 15 + m, Decimal(f"100{m}.00")) for m in range(5)]
  added = cache.update_bars("RELIANCE", bars_batch1)
  assert added == 5
  assert len(cache.get_bars("RELIANCE")) == 5
  assert cache.get_latest_price("RELIANCE") == Decimal("1004.00")
  assert cache.get_last_timestamp("RELIANCE") == bars_batch1[-1].timestamp_utc

  # 3. Add overlapping batch with duplicates
  bars_batch2 = [
      bars_batch1[-1],  # Duplicate
      _make_bar("RELIANCE", 9, 20, Decimal("1005.00")),
      _make_bar("RELIANCE", 9, 21, Decimal("1006.00")),
  ]
  added2 = cache.update_bars("RELIANCE", bars_batch2)
  assert added2 == 2  # Only 2 new bars should be added
  assert len(cache.get_bars("RELIANCE")) == 7

  # 4. Check cached VWAP and Opening Range calculations
  vwap = cache.get_vwap("RELIANCE")
  assert vwap is not None
  assert vwap > Decimal("1000.00")

  # 5. Check cache statistics
  stats = cache.get_cache_stats()
  assert stats["symbols_cached"] == 1
  assert stats["total_bars_cached"] == 7
  assert stats["cache_hits"] >= 1
  assert stats["hit_rate_pct"] > Decimal("50.0")

  # 6. Session rollover clears cache
  cache.check_session_rollover("2026-11-12")
  stats2 = cache.get_cache_stats()
  assert stats2["session_date"] == "2026-11-12"
  assert stats2["symbols_cached"] == 0
  assert stats2["total_bars_cached"] == 0
