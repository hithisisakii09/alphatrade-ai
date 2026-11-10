"""PAPER-only execution guard — fails closed if LIVE mode is ever requested."""

import os


class LiveTradingProhibitedError(RuntimeError):
  """Raised when any component attempts to initialize or execute in LIVE mode."""


def assert_paper_mode(mode: str | None = None) -> str:
  """Verifies that the execution mode is strictly PAPER. Fails closed otherwise."""
  effective_mode = (mode or os.environ.get("ALPHATRADE_EXECUTION_MODE", "PAPER")).strip().upper()
  if effective_mode != "PAPER":
    raise LiveTradingProhibitedError(
        f"CRITICAL SAFETY VIOLATION: Execution mode '{effective_mode}' is prohibited. "
        "AlphaTrade AI MVP operates strictly in PAPER mode and fails closed on LIVE."
    )
  return "PAPER"
