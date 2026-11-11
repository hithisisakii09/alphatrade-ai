"""Unified test runner executing all unit, integration, backtest, and safety test functions."""

from pathlib import Path
import sys
import tempfile
import traceback

from tests.unit import test_core_and_indicators as t_core
from tests.unit import test_candle_cache as t_cache
from tests.unit import test_risk_and_costs as t_risk
from tests.unit import test_angel_provider as t_angel
from tests.unit import test_notifications as t_notify
from tests.unit import test_volatility_and_regime as t_vol
from tests.integration import test_broker_and_recovery as t_broker
from tests.integration import test_daily_runner as t_runner
from tests.backtest import test_backtester_and_lookahead as t_bt
from tests.backtest import test_walk_forward_oos as t_oos
from tests.backtest import test_research_provenance as t_prov
from tests.backtest import test_multi_session_backtest as t_multi
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
      ("unit/test_candle_cache.py", [
          t_cache.test_live_candle_cache_lifecycle_and_deduplication,
      ]),
      ("unit/test_risk_and_costs.py", [
          t_risk.test_exact_legacy_8_20_10_worked_example,
          t_risk.test_v2_intraday_exit_levels_and_pessimistic_intrabar_priority,
          t_risk.test_gap_below_stop_uses_conservative_bar_open,
      ]),
      ("unit/test_angel_provider.py", [
          t_angel.test_rfc6238_totp_generation_test_vectors,
          t_angel.test_angel_symbol_token_resolution,
          t_angel.test_angel_response_parsing_to_candle_bars,
          t_angel.test_angel_auth_error_when_credentials_missing,
          t_angel.test_get_data_provider_factory,
          lambda: t_angel.test_angel_session_caching(Path(tempfile.mkdtemp())),
      ]),
      ("unit/test_notifications.py", [
          t_notify.test_notification_service_configuration,
          t_notify.test_message_formatting_integrity,
          t_notify.test_send_message_sync_mocked,
          t_notify.test_send_message_background_returns_immediately,
      ]),
      ("unit/test_volatility_and_regime.py", [
          t_vol.test_sector_concentration_guard,
          t_vol.test_atr_adjusted_volatility_sizing,
          t_vol.test_rvol_threshold_filtering,
      ]),
      ("integration/test_broker_and_recovery.py", [
          t_broker.test_idempotency_partial_fills_and_restart_recovery,
          lambda: t_broker.test_cross_process_restart_persistence(Path(tempfile.mkdtemp())),
      ]),
      ("integration/test_daily_runner.py", [
          t_runner.test_scheduled_daily_runner_jobs_and_lifecycle,
          t_runner.test_runner_status_fastapi_endpoint,
          lambda: t_runner.test_intraday_tick_breakout_execution_and_eod_liquidation(Path(tempfile.mkdtemp())),
          lambda: t_runner.test_tick_latency_profiling_and_telemetry_summary(Path(tempfile.mkdtemp())),
      ]),
      ("backtest/test_backtester_and_lookahead.py", [
          t_bt.test_deliberate_lookahead_bug_caught,
          lambda: t_bt.test_reproducible_end_to_end_benchmark_and_api_routes(Path(tempfile.mkdtemp())),
      ]),
      ("backtest/test_walk_forward_oos.py", [
          t_oos.test_walk_forward_chronological_split_integrity,
          t_oos.test_100_trade_out_of_sample_and_slippage_stress_matrix,
      ]),
      ("backtest/test_research_provenance.py", [
          t_prov.test_classify_evidence_sample_size_thresholds,
          t_prov.test_compute_confidence_intervals_insufficient_sample,
          t_prov.test_compute_confidence_intervals_valid_sample,
          t_prov.test_real_historical_walk_forward_provenance_and_classification,
          t_prov.test_synthetic_stress_test_provenance_cannot_be_confused_with_historical,
          t_prov.test_paper_safety_guard_enforced,
      ]),
      ("backtest/test_multi_session_backtest.py", [
          t_multi.test_two_consecutive_sessions_updates_prev_close_and_circuit_bands,
          t_multi.test_multiple_symbols_independent_previous_closes,
          t_multi.test_missing_session_data_uses_last_available_close,
          t_multi.test_incomplete_session_rollover,
          t_multi.test_overnight_unadjusted_split_gap_quarantines_symbol,
          t_multi.test_first_session_fallback_when_prev_close_not_provided,
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
