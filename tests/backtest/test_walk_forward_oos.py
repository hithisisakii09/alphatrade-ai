"""Milestone 7 Test: Chronological In-Sample/Out-of-Sample Split, >=100 OOS Trades & Slippage Matrix."""

from decimal import Decimal
import pytest

from app.research.walk_forward import (
    generate_multi_day_dataset,
    run_100_trade_out_of_sample_study,
    split_bars_chronologically,
)


def test_walk_forward_chronological_split_integrity():
  """Verifies that chronological splitting enforces zero look-ahead and strict temporal separation."""
  bars_by_symbol, _ = generate_multi_day_dataset(["RELIANCE"], total_days=10, seed=123)
  reliance_bars = bars_by_symbol["RELIANCE"]
  assert len(reliance_bars) > 0

  is_bars, oos_bars = split_bars_chronologically(reliance_bars, is_ratio=0.70)
  assert len(is_bars) + len(oos_bars) == len(reliance_bars)
  assert len(is_bars) == int(len(reliance_bars) * 0.70)

  # Check strict temporal monotonicity: max IS timestamp must be < min OOS timestamp
  max_is_ts = max(b.timestamp_utc for b in is_bars)
  min_oos_ts = min(b.timestamp_utc for b in oos_bars)
  assert max_is_ts < min_oos_ts, "Look-ahead leakage: IS bar timestamp overlaps or exceeds OOS bar timestamp"


def test_100_trade_out_of_sample_and_slippage_stress_matrix():
  """Verifies >= 100 closed OOS trades, positive net expectancy, and resilience across 1x, 2x, and 3x slippage."""
  res = run_100_trade_out_of_sample_study(db_url="sqlite:///:memory:")

  # 1. Statistical validity: must have >= 100 closed out-of-sample trades
  assert res.is_statistically_valid is True
  assert res.out_of_sample_summary.total_trades >= 100
  assert res.evidence_classification in ("ELIGIBLE_FOR_STATISTICAL_EVALUATION", "POSITIVE_EVIDENCE")
  assert res.data_provenance == "SYNTHETIC_STRESS_TEST"

  # 2. Positive net expectancy and favorable win rate
  assert res.out_of_sample_summary.net_expectancy_r > Decimal("0.0")
  assert res.out_of_sample_summary.net_expectancy_inr > Decimal("0.0")
  assert res.out_of_sample_summary.win_rate_pct >= Decimal("50.0")
  assert res.out_of_sample_summary.profit_factor >= Decimal("2.0")

  # 3. Slippage Sensitivity Stress Matrix (1x, 2x, 3x)
  assert len(res.slippage_stress_matrix) == 3
  for label, summary in res.slippage_stress_matrix.items():
    assert summary.total_trades == res.out_of_sample_summary.total_trades
    assert summary.net_expectancy_r > Decimal("0.0"), f"Slippage failure under {label}"
    assert summary.net_expectancy_inr > Decimal("0.0"), f"Negative net INR under {label}"
    assert summary.profit_factor > Decimal("1.5"), f"Profit factor collapsed under {label}"

