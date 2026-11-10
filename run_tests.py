"""Unified test runner executing all unit, integration, backtest, and safety test functions."""

from pathlib import Path
import sys
import tempfile
import traceback

from tests.unit import test_core_and_indicators as t_core
from tests.unit import test_risk_and_costs as t_risk
from tests.integration import test_broker_and_recovery as t_broker
from tests.backtest import test_backtester_and_lookahead as t_bt
from tests.safety import test_live_guard_and_edge_cases as t_safe


class _MonkeyPatch:
  def __init__(self):
    import os
    self._orig = dict(os.environ)

  def setenv(self, k: str, v: str) -> None:
    import os
    os.environ[k] = v

  def restore(self) -> None:
    import os
    os.environ.clear()
    os.environ.update(self._orig)


def run_all() -> int:
  suites = [
      ("unit/test_core_and_indicators.py", [
          t_core.test_decimal_rejects_binary_float,
          t_core.test_holiday_and_special_session_calendar,
          t_core.test_validator_catches_duplicates_stale_and_unadjusted_splits,
          t_core.test_indicators_vwap_ema_atr_orb_rvol,
      ]),
      ("unit/test_risk_and_costs.py", [
          t_risk.test_exact_legacy_8_20_10_worked_example,
          t_risk.test_v2_intraday_exit_levels_and_pessimistic_intrabar_priority,
          t_risk.test_gap_below_stop_uses_conservative_bar_open,
      ]),
      ("integration/test_broker_and_recovery.py", [
          t_broker.test_idempotency_partial_fills_and_restart_recovery,
          lambda: t_broker.test_cross_process_restart_persistence(Path(tempfile.mkdtemp())),
      ]),
      ("backtest/test_backtester_and_lookahead.py", [
          t_bt.test_deliberate_lookahead_bug_caught,
          lambda: t_bt.test_reproducible_end_to_end_benchmark_and_api_routes(Path(tempfile.mkdtemp())),
      ]),
      ("safety/test_live_guard_and_edge_cases.py", [
          lambda: _run_with_monkeypatch(t_safe.test_live_mode_rejected_at_startup_and_by_guard),
          t_safe.test_rejected_orders_do_not_count_as_completed_entries_and_kill_switch_edges,
          t_safe.test_all_11_readonly_fastapi_endpoints,
      ]),
  ]

  passed = 0
  failed = 0
  for suite_name, funcs in suites:
    for fn in funcs:
      name = getattr(fn, "__name__", "test_fn")
      try:
        fn()
        print(f"[PASS] {suite_name}::{name}")
        passed += 1
      except Exception:
        print(f"[FAIL] {suite_name}::{name}")
        traceback.print_exc()
        failed += 1

  print(f"\n=== TEST SUMMARY: {passed} passed, {failed} failed ===")
  return 0 if failed == 0 else 1


def _run_with_monkeypatch(fn):
  mp = _MonkeyPatch()
  try:
    fn(mp)
  finally:
    mp.restore()


if __name__ == "__main__":
  sys.exit(run_all())
