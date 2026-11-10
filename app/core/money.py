"""Decimal financial arithmetic helpers — strictly forbids binary float inputs."""

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Union

MoneyInput = Union[Decimal, int, str]

FOUR_DP = Decimal("0.0001")
TWO_DP = Decimal("0.01")


def to_decimal(val: MoneyInput) -> Decimal:
  """Converts int, str, or Decimal to Decimal. Rejects binary float to prevent precision leakage."""
  if isinstance(val, float):
    raise TypeError(
        f"Binary float {val!r} is prohibited in financial calculations; use Decimal or str."
    )
  if isinstance(val, Decimal):
    return val
  if isinstance(val, (int, str)):
    return Decimal(str(val))
  raise TypeError(f"Unsupported monetary type: {type(val).__name__}")


def round_paise(val: MoneyInput) -> Decimal:
  """Rounds a monetary value to 2 decimal places (paise) using ROUND_HALF_UP."""
  return to_decimal(val).quantize(TWO_DP, rounding=ROUND_HALF_UP)


def round_price(val: MoneyInput) -> Decimal:
  """Rounds a price or rate to 4 decimal places."""
  return to_decimal(val).quantize(FOUR_DP, rounding=ROUND_HALF_UP)


def round_to_tick(price: MoneyInput, tick_size: MoneyInput = Decimal("0.05"), direction: str = "NEAREST") -> Decimal:
  """Aligns a price to the instrument's NSE tick size (default INR 0.05)."""
  p = to_decimal(price)
  t = to_decimal(tick_size)
  if t <= Decimal("0"):
    raise ValueError("tick_size must be positive")
  steps = p / t
  if direction == "DOWN":
    quantized_steps = steps.to_integral_value(rounding=ROUND_DOWN)
  elif direction == "UP":
    from decimal import ROUND_CEILING
    quantized_steps = steps.to_integral_value(rounding=ROUND_CEILING)
  else:
    quantized_steps = steps.to_integral_value(rounding=ROUND_HALF_UP)
  return (quantized_steps * t).quantize(TWO_DP, rounding=ROUND_HALF_UP)
