"""Milestone M8.1 Stage 1 Tests: Research Evidence Classification, Provenance & Confidence Intervals."""

from decimal import Decimal
import os
import pytest

from app.backtest.engine import BacktestTradeRecord
from app.core.execution_guard import LiveTradingProhibitedError, assert_paper_mode
from app.research.walk_forward import (
    WalkForwardEvaluationResult,
    classify_evidence,
    compute_confidence_intervals,
    generate_research_report_markdown,
    run_100_trade_out_of_sample_study,
    run_real_historical_walk_forward,
)


def test_classify_evidence_sample_size_thresholds():
  """Verifies that sample size < 100 is classified as INCONCLUSIVE and >= 100 as ELIGIBLE."""
  assert classify_evidence(0) == "INCONCLUSIVE_SAMPLE_SIZE_BELOW_100"
  assert classify_evidence(1) == "INCONCLUSIVE_SAMPLE_SIZE_BELOW_100"
  assert classify_evidence(99) == "INCONCLUSIVE_SAMPLE_SIZE_BELOW_100"
  assert classify_evidence(100) == "ELIGIBLE_FOR_STATISTICAL_EVALUATION"
  assert classify_evidence(250) == "ELIGIBLE_FOR_STATISTICAL_EVALUATION"


def test_compute_confidence_intervals_insufficient_sample():
  """Verifies that trades with N < 2 safely flag inconclusive confidence interval status."""
  # Empty trades
  ci_zero = compute_confidence_intervals([])
  assert ci_zero["status"] == "INCONCLUSIVE_SAMPLE_SIZE_BELOW_100"
  assert ci_zero["sample_size"] == 0

  # Single trade
  single_trade = [
      BacktestTradeRecord(
          run_id="TEST_RUN",
          symbol="RELIANCE",
          session_date="2026-11-09",
          quantity=10,
          entry_price=Decimal("1300.00"),
          exit_price=Decimal("1310.00"),
          gross_pnl_inr=Decimal("100.00"),
          total_fees_inr=Decimal("10.00"),
          net_pnl_inr=Decimal("90.00"),
          r_multiple_net=Decimal("1.25"),
          holding_minutes=15,
          exit_reason="PROFIT_TARGET",
      )
  ]
  ci_one = compute_confidence_intervals(single_trade)
  assert ci_one["status"] == "INCONCLUSIVE_SAMPLE_SIZE_BELOW_100"
  assert ci_one["sample_size"] == 1


def test_compute_confidence_intervals_valid_sample():
  """Verifies parametric 95% confidence interval and standard error formulas for N >= 2."""
  trades = [
      BacktestTradeRecord(
          run_id="TEST_RUN",
          symbol="RELIANCE",
          session_date="2026-11-09",
          quantity=10,
          entry_price=Decimal("1300.00"),
          exit_price=Decimal("1310.00") if i % 2 == 0 else Decimal("1290.00"),
          gross_pnl_inr=Decimal("100.00") if i % 2 == 0 else Decimal("-100.00"),
          total_fees_inr=Decimal("10.00"),
          net_pnl_inr=Decimal("90.00") if i % 2 == 0 else Decimal("-110.00"),
          r_multiple_net=Decimal("1.00") if i % 2 == 0 else Decimal("-1.00"),
          holding_minutes=15,
          exit_reason="TEST",
      )
      for i in range(10)
  ]
  ci = compute_confidence_intervals(trades)
  assert ci["status"] == "COMPUTED"
  assert ci["sample_size"] == 10
  assert ci["win_rate_sample_pct"] == 50.0
  assert ci["win_rate_standard_error_pct"] > 0
  assert ci["win_rate_95_ci_pct"][0] <= ci["win_rate_95_ci_pct"][1]
  assert -1.0 <= ci["expectancy_r_95_ci"][0] <= ci["expectancy_r_95_ci"][1] <= 1.0


def test_real_historical_walk_forward_provenance_and_classification():
  """Verifies that run_real_historical_walk_forward reads authentic SQLite data and classifies N < 100 correctly."""
  res = run_real_historical_walk_forward(db_url="sqlite:///./alphatrade_v2.db", symbols=["HDFCBANK"])

  assert res.data_provenance == "REAL_HISTORICAL_SQLITE"
  assert res.evidence_classification == "INCONCLUSIVE_SAMPLE_SIZE_BELOW_100"
  assert res.is_statistically_valid is False
  assert res.total_candles > 0
  assert "HDFCBANK" in res.symbols

  # Verify report dictionary completeness
  rep = res.to_report_dict()
  assert rep["data_provenance"] == "REAL_HISTORICAL_SQLITE"
  assert rep["evidence_classification"] == "INCONCLUSIVE_SAMPLE_SIZE_BELOW_100"
  assert rep["is_statistically_valid"] is False
  assert rep["total_candles"] > 0
  assert "date_range" in rep
  assert "confidence_intervals" in rep
  assert "in_sample_performance" in rep
  assert "out_of_sample_performance" in rep

  # Verify Markdown formatting
  md = generate_research_report_markdown(res)
  assert "REAL_HISTORICAL_SQLITE" in md
  assert "INCONCLUSIVE_SAMPLE_SIZE_BELOW_100" in md
  assert "INSUFFICIENT SAMPLE SIZE (N < 100)" in md


def test_synthetic_stress_test_provenance_cannot_be_confused_with_historical():
  """Verifies that synthetic benchmark results explicitly carry SYNTHETIC_STRESS_TEST provenance."""
  synth_res = run_100_trade_out_of_sample_study(db_url="sqlite:///:memory:")
  assert synth_res.data_provenance == "SYNTHETIC_STRESS_TEST"
  assert synth_res.evidence_classification == "ELIGIBLE_FOR_STATISTICAL_EVALUATION"
  assert synth_res.is_statistically_valid is True
  assert synth_res.out_of_sample_summary.total_trades >= 100

  # Provenances must be distinct
  hist_res = run_real_historical_walk_forward(db_url="sqlite:///./alphatrade_v2.db", symbols=["HDFCBANK"])
  assert synth_res.data_provenance != hist_res.data_provenance
  assert hist_res.data_provenance == "REAL_HISTORICAL_SQLITE"


def test_paper_safety_guard_enforced():
  """Verifies that research execution fails closed if ALPHATRADE_EXECUTION_MODE is set to LIVE."""
  orig_mode = os.environ.get("ALPHATRADE_EXECUTION_MODE")
  try:
    os.environ["ALPHATRADE_EXECUTION_MODE"] = "LIVE"
    with pytest.raises(LiveTradingProhibitedError):
      assert_paper_mode()
  finally:
    if orig_mode is not None:
      os.environ["ALPHATRADE_EXECUTION_MODE"] = orig_mode
    else:
      os.environ["ALPHATRADE_EXECUTION_MODE"] = "PAPER"

