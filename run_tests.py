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
from tests.unit import test_macro_trend_and_m13 as t_m13
from tests.unit import test_analytics_and_journal_m14 as t_m14
from tests.unit import test_monte_carlo_m15 as t_m15
from tests.unit import test_vwap_bands_and_m16 as t_m16
from tests.unit import test_var_and_cvar_m17 as t_m17
from tests.unit import test_market_impact_m18 as t_m18
from tests.unit import test_regime_m19 as t_m19
from tests.unit import test_augmented_study_m20 as t_m20
from tests.unit import test_hyperparameter_optimizer_m21 as t_m21
from tests.unit import test_drift_surveillance_m22 as t_m22
from tests.unit import test_capital_allocation_m23 as t_m23
from tests.unit import test_execution_gateway_m24 as t_m24
from tests.unit import test_algo_execution_m25 as t_m25
from tests.unit import test_streaming_m26 as t_m26
from tests.unit import test_meta_ensemble_m27 as t_m27
from tests.unit import test_disaster_recovery_m28 as t_m28
from tests.unit import test_compliance_m29 as t_m29
from tests.unit import test_preflight_m30 as t_m30
from tests.unit import test_live_gateway as t_live
from tests.unit import test_nano_5k_profile as t_nano
from tests.integration import test_broker_and_recovery as t_broker
from tests.integration import test_daily_runner as t_runner
from tests.integration import test_production_hardening as t_hard
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

  def delenv(self, k: str, raising: bool = True) -> None:
    import os
    if k in os.environ:
      del os.environ[k]
    elif raising:
      raise KeyError(k)

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
      ("unit/test_macro_trend_and_m13.py", [
          t_m13.test_higher_timeframe_ema_calculation,
          t_m13.test_macro_trend_alignment_filter,
          t_m13.test_two_day_high_breakout_filter,
          t_m13.test_adaptive_atr_trailing_stop_activation,
          t_m13.test_m13_end_to_end_strategy_rejection_of_counter_trend,
      ]),
      ("unit/test_analytics_and_journal_m14.py", [
          t_m14.test_compute_position_mae_mfe,
          t_m14.test_compute_trade_analytics_aggregation,
          t_m14.test_generate_trade_journal_csv,
          t_m14.test_m14_api_routes_integration,
      ]),
      ("unit/test_monte_carlo_m15.py", [
          t_m15.test_monte_carlo_empty_trade_pool,
          t_m15.test_monte_carlo_deterministic_seed,
          t_m15.test_monte_carlo_loss_pool_triggers_ruin,
          t_m15.test_monte_carlo_api_endpoint,
      ]),
      ("unit/test_vwap_bands_and_m16.py", [
          t_m16.test_compute_session_vwap_std_calculation,
          t_m16.test_check_vwap_breakout_exhaustion_filter,
          t_m16.test_m16_end_to_end_strategy_rejection_of_exhausted_breakout,
      ]),
      ("unit/test_var_and_cvar_m17.py", [
          t_m17.test_returns_and_series_statistics,
          t_m17.test_cross_asset_covariance_and_correlation,
          t_m17.test_cornish_fisher_quantile_expansion,
          t_m17.test_portfolio_var_cvar_diversification_benefit,
          t_m17.test_risk_engine_portfolio_var_gate,
          t_m17.test_var_api_endpoint,
      ]),
      ("unit/test_market_impact_m18.py", [
          t_m18.test_square_root_impact_calculation,
          t_m18.test_participation_rate_boundary_compliance,
          t_m18.test_nse_cost_engine_dynamic_impact_slippage,
          t_m18.test_risk_engine_participation_rate_limit_gate,
          t_m18.test_execution_analytics_engine_historical_aggregation,
          t_m18.test_api_execution_impact_endpoint,
      ]),
      ("unit/test_regime_m19.py", [
          t_m19.test_bull_regime_detected_on_rising_prices,
          t_m19.test_bear_regime_detected_on_falling_prices,
          t_m19.test_insufficient_bars_returns_neutral_fallback,
          t_m19.test_regime_sequence_length_matches_observations,
          t_m19.test_orb_engine_rejects_on_adverse_regime,
          t_m19.test_api_regime_endpoint,
      ]),
      ("unit/test_augmented_study_m20.py", [
          t_m20.test_wilson_score_interval_bounds,
          t_m20.test_expectancy_stats_and_hypothesis_test,
          t_m20.test_factor_ablation_model_configs,
          t_m20.test_factor_ablation_engine_execution,
          t_m20.test_ablation_marginal_deltas,
          t_m20.test_api_augmented_study_endpoint,
      ]),
      ("unit/test_hyperparameter_optimizer_m21.py", [
          t_m21.test_hyperparam_point_construction_and_label,
          t_m21.test_random_grid_sampler_constraints,
          t_m21.test_composite_score_function,
          t_m21.test_config_builder_applies_params,
          t_m21.test_gp_surrogate_predictions,
          t_m21.test_point_to_vector_normalisation,
          t_m21.test_optimizer_pipeline_tiny_dataset,
          t_m21.test_api_hyperparameter_optimization_endpoint,
      ]),
      ("unit/test_drift_surveillance_m22.py", [
          t_m22.test_two_sample_ks_identical_distributions,
          t_m22.test_two_sample_ks_shifted_distributions,
          t_m22.test_cusum_detector_stable_sequence,
          t_m22.test_cusum_detector_downward_drift_triggers_change_point,
          t_m22.test_drift_health_score_and_status_transitions,
          t_m22.test_fail_safe_guard_trips_risk_engine_kill_switch,
          t_m22.test_calibrated_research_profile_configuration,
          t_m22.test_api_drift_surveillance_endpoint,
      ]),
      ("unit/test_capital_allocation_m23.py", [
          t_m23.test_inverse_volatility_weights_calculation,
          t_m23.test_equal_risk_contribution_convergence,
          t_m23.test_weight_capping_and_redistribution,
          t_m23.test_diversification_ratio_properties,
          t_m23.test_pre_trade_margin_stress_scenarios,
          t_m23.test_risk_engine_pre_trade_margin_stress_gate_rejection,
          t_m23.test_risk_engine_pre_trade_margin_stress_gate_approval,
          t_m23.test_m23_api_endpoints_integration,
      ]),
      ("unit/test_execution_gateway_m24.py", [
          t_m24.test_paper_broker_gateway_order_placement,
          t_m24.test_paper_broker_gateway_cancel_order,
          t_m24.test_mock_angel_gateway_position_tracking,
          t_m24.test_smart_order_router_selects_lowest_latency_gateway,
          t_m24.test_smart_order_router_prefers_requested_gateway,
          t_m24.test_shadow_reconciler_healthy_state,
          t_m24.test_shadow_reconciler_detects_orphan_position_and_trips_kill_switch,
          t_m24.test_m24_api_endpoints_integration,
      ]),
      ("unit/test_algo_execution_m25.py", [
          t_m25.test_twap_slice_generation_and_exact_quantity_conservation,
          t_m25.test_dynamic_vwap_slice_generation_and_exact_quantity_conservation,
          t_m25.test_microstructure_metrics_calculation,
          t_m25.test_microstructure_liquidity_gate_deferral_on_spread_widening,
          t_m25.test_tca_implementation_shortfall_decomposition,
          t_m25.test_algo_slicer_aborts_on_excessive_adverse_slippage,
          t_m25.test_algo_execution_coordinator_with_broker_gateway,
          t_m25.test_m25_api_endpoints_integration,
      ]),
      ("unit/test_streaming_m26.py", [
          t_m26.test_binary_packet_encoder_and_decoder_parity,
          t_m26.test_snapquote_mode3_depth_parsing,
          t_m26.test_live_order_book_depth_update_and_snapshot,
          t_m26.test_streaming_feed_handler_tick_dispatch_and_metrics,
          t_m26.test_streaming_tick_breakout_detector_fires_on_orh_cross,
          t_m26.test_streaming_tick_breakout_detector_rejects_wide_spread,
          t_m26.test_streaming_tick_breakout_detector_rejects_bear_regime,
          t_m26.test_m26_api_endpoints_integration,
      ]),
      ("unit/test_meta_ensemble_m27.py", [
          t_m27.test_base_strategy_interface_and_signal_serialization,
          t_m27.test_orb_trend_strategy_evaluation,
          t_m27.test_vwap_mean_reversion_oversold_signal,
          t_m27.test_pairs_stat_arb_spread_zscore_and_signal,
          t_m27.test_meta_ensemble_dynamic_weights_computation,
          t_m27.test_meta_ensemble_signal_fusion_and_conflict_resolution,
          t_m27.test_meta_ensemble_fail_closed_paper_mode_guard,
          t_m27.test_m27_api_endpoints_integration,
      ]),
      ("unit/test_disaster_recovery_m28.py", [
          t_m28.test_snapshot_creation_and_sha256_integrity,
          t_m28.test_snapshot_load_and_verify_positions,
          t_m28.test_snapshot_integrity_violation_raises_error,
          t_m28.test_rehydration_with_no_snapshot_returns_healthy,
          t_m28.test_rehydration_restores_positions_and_algos,
          t_m28.test_tick_replayer_deterministic_match,
          lambda: _run_with_monkeypatch(t_m28.test_fail_closed_paper_mode_guard),
          t_m28.test_m28_api_endpoints_integration,
      ]),
      ("unit/test_compliance_m29.py", [
          t_m29.test_worm_logger_genesis_and_chaining,
          t_m29.test_worm_logger_tamper_detection_breaks_chain,
          t_m29.test_worm_logger_sebi_fields_and_microsecond_precision,
          t_m29.test_fix_drop_copy_message_formatting_and_checksum,
          t_m29.test_fix_drop_copy_feed_broadcast_and_buffering,
          t_m29.test_worm_log_file_persistence_and_reload,
          lambda: _run_with_monkeypatch(t_m29.test_compliance_fail_closed_paper_mode_guard),
          t_m29.test_m29_api_endpoints_integration,
      ]),
      ("unit/test_preflight_m30.py", [
          t_m30.test_preflight_engine_passes_all_checks_in_standard_env,
          lambda: _run_with_monkeypatch(t_m30.test_preflight_fails_when_live_mode_attempted),
          t_m30.test_preflight_detects_corrupted_worm_log,
          t_m30.test_preflight_detects_float_drift,
          t_m30.test_preflight_margin_stress_headroom_evaluation,
          t_m30.test_preflight_meta_ensemble_weight_normalization,
          t_m30.test_preflight_algo_slicer_validation,
          t_m30.test_m30_api_endpoints_integration,
      ]),
      ("unit/test_live_gateway.py", [
          lambda: _run_with_monkeypatch(t_live.test_live_gateway_fails_closed_in_paper_mode),
          lambda: _run_with_monkeypatch(t_live.test_live_gateway_fails_closed_without_confirmation),
          lambda: _run_with_monkeypatch(t_live.test_live_gateway_authorized_with_proper_two_factor_env),
          lambda: _run_with_monkeypatch(t_live.test_live_gateway_order_quantity_safety_cap),
          lambda: _run_with_monkeypatch(t_live.test_live_gateway_mocked_order_placement_and_cancel),
          lambda: _run_with_monkeypatch(t_live.test_smart_order_router_routes_to_live_gateway),
      ]),
      ("unit/test_nano_5k_profile.py", [
          t_nano.test_nano_5k_profile_loads,
          t_nano.test_nano_5k_stop_loss_exit_levels_tatasteel,
          t_nano.test_nano_5k_stop_triggers_on_5pct_drop,
          t_nano.test_nano_5k_no_exit_on_4pct_dip,
          t_nano.test_nano_5k_risk_engine_approves_tatasteel_entry,
          t_nano.test_nano_5k_risk_engine_rejects_on_daily_loss_limit,
          lambda: _run_with_monkeypatch(t_nano.test_nano_5k_runner_uses_low_price_universe),
          t_nano.test_nano_5k_profile_key_in_strategy_config,
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
      ("integration/test_production_hardening.py", [
          t_hard.test_wal_checkpoint_and_backup_creation_and_restore,
          t_hard.test_watchdog_health_and_dead_man_switch,
          t_hard.test_daily_runner_production_hardening_jobs,
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
        print(f"[PASS] {suite_name}::{name}", flush=True)
        passed += 1
      except Exception:
        print(f"[FAIL] {suite_name}::{name}", flush=True)
        traceback.print_exc()
        failed += 1

  print(f"\n=== TEST SUMMARY: {passed} passed, {failed} failed ===", flush=True)
  try:
    from app.api.routes import _daily_runner
    if _daily_runner and _daily_runner.is_running:
      _daily_runner.stop()
  except Exception:
    pass
  return 0 if failed == 0 else 1


def _run_with_monkeypatch(fn):
  mp = _MonkeyPatch()
  try:
    fn(mp)
  finally:
    mp.restore()


if __name__ == "__main__":
  sys.exit(run_all())
