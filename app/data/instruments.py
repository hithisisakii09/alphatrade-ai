"""Instrument master loader and circuit price-band calculator."""

from decimal import Decimal
import json
import os
from app.core.money import round_to_tick, to_decimal
from app.data.models import InstrumentMeta


class InstrumentRegistry:
  def __init__(self, config_path: str | None = None):
    if config_path is None:
      base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
      config_path = os.path.join(base, "config/instruments/nse_nifty100.json")
    with open(config_path, "r") as f:
      raw = json.load(f)
    self.version: str = raw["version"]
    self.instruments: dict[str, InstrumentMeta] = {
        item["symbol"]: InstrumentMeta(**item) for item in raw["instruments"]
    }

  def get(self, symbol: str) -> InstrumentMeta | None:
    return self.instruments.get(symbol)

  def is_eligible(self, symbol: str, min_turnover_cr: Decimal = Decimal("25.0")) -> tuple[bool, str]:
    inst = self.get(symbol)
    if inst is None:
      return False, "SYMBOL_NOT_IN_MASTER"
    if not inst.eligible:
      return False, "SYMBOL_MARKED_INELIGIBLE"
    if inst.median_daily_turnover_cr < min_turnover_cr:
      return False, "INSUFFICIENT_LIQUIDITY_TURNOVER"
    return True, "OK"

  def compute_price_bands(self, symbol: str, prev_close: Decimal) -> tuple[Decimal, Decimal]:
    """Returns (lower_circuit, upper_circuit) rounded to tick size."""
    inst = self.get(symbol)
    band_pct = (inst.price_band_pct if inst else Decimal("10.0")) / Decimal("100")
    tick = inst.tick_size if inst else Decimal("0.05")
    pc = to_decimal(prev_close)
    lower = round_to_tick(pc * (Decimal("1") - band_pct), tick, direction="UP")
    upper = round_to_tick(pc * (Decimal("1") + band_pct), tick, direction="DOWN")
    return lower, upper
