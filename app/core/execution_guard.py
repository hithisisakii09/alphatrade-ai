"""Execution guards — enforces strict PAPER execution by default, with fail-closed two-factor authorization for LIVE execution."""

import os


class LiveTradingProhibitedError(RuntimeError):
  """Raised when any component attempts to initialize or execute in LIVE mode without proper authorization."""


class LiveTradingUnauthorizedError(RuntimeError):
  """Raised when live trading is attempted without strict two-factor environment authorization."""


def assert_paper_mode(mode: str | None = None) -> str:
  """Verifies that the execution mode is strictly PAPER. Fails closed otherwise."""
  effective_mode = (mode or os.environ.get("ALPHATRADE_EXECUTION_MODE", "PAPER")).strip().upper()
  if effective_mode != "PAPER":
    raise LiveTradingProhibitedError(
        f"CRITICAL SAFETY VIOLATION: Execution mode '{effective_mode}' is prohibited. "
        "AlphaTrade AI MVP operates strictly in PAPER mode and fails closed on LIVE."
    )
  return "PAPER"


def assert_live_mode_authorized(mode: str | None = None) -> str:
  """Verifies that live trading is explicitly authorized with two-factor environment confirmation.

  Requires:
  1. ALPHATRADE_EXECUTION_MODE in ('LIVE', 'LIVE_SANDBOX')
  2. ALPHATRADE_LIVE_CONFIRMATION == 'I_ACCEPT_FINANCIAL_RISK'
  """
  effective_mode = (mode or os.environ.get("ALPHATRADE_EXECUTION_MODE", "PAPER")).strip().upper()
  if effective_mode not in ("LIVE", "LIVE_SANDBOX"):
    raise LiveTradingUnauthorizedError(
        f"CRITICAL LIVE GATEWAY REJECTION: Execution mode is '{effective_mode}'. "
        "Live broker execution requires ALPHATRADE_EXECUTION_MODE='LIVE' or 'LIVE_SANDBOX'."
    )
  confirmation = os.environ.get("ALPHATRADE_LIVE_CONFIRMATION", "").strip()
  if confirmation != "I_ACCEPT_FINANCIAL_RISK":
    raise LiveTradingUnauthorizedError(
        "CRITICAL LIVE GATEWAY REJECTION: Missing two-factor financial safety acknowledgment. "
        "Set ALPHATRADE_LIVE_CONFIRMATION='I_ACCEPT_FINANCIAL_RISK' to authorize live broker order routing."
    )
  return effective_mode


def is_live_mode_authorized() -> bool:
  """Returns True only if live mode has satisfied all two-factor authorization conditions."""
  try:
    assert_live_mode_authorized()
    return True
  except Exception:
    return False


def assert_safe_execution_mode(mode: str | None = None) -> str:
  """Verifies that the execution mode is valid: PAPER (by default) or LIVE (if two-factor authorized)."""
  if is_live_mode_authorized():
    return assert_live_mode_authorized(mode)
  return assert_paper_mode(mode)
