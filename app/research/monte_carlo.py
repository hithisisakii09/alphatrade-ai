"""Milestone M15: Non-Parametric Monte Carlo Bootstrap Resampling & Risk of Ruin Simulator."""

from dataclasses import dataclass, field
from decimal import Decimal
import random
from typing import Any

from app.core.money import round_paise, round_price, to_decimal


@dataclass(frozen=True)
class MonteCarloSimulationResult:
  """Standardized output of non-parametric bootstrap Monte Carlo simulation."""

  total_iterations: int
  sample_trade_count: int
  starting_capital_inr: Decimal
  ruin_drawdown_threshold_pct: Decimal
  ruin_probability_pct: Decimal
  drawdown_5pct_prob: Decimal
  drawdown_10pct_prob: Decimal
  drawdown_15pct_prob: Decimal
  median_max_drawdown_pct: Decimal
  p95_max_drawdown_pct: Decimal
  p99_max_drawdown_pct: Decimal
  median_ending_equity_inr: Decimal
  p05_ending_equity_inr: Decimal
  p95_ending_equity_inr: Decimal
  sample_paths: dict[str, list[float]] = field(default_factory=dict)

  def to_dict(self) -> dict[str, Any]:
    return {
        "total_iterations": self.total_iterations,
        "sample_trade_count": self.sample_trade_count,
        "starting_capital_inr": str(self.starting_capital_inr),
        "ruin_drawdown_threshold_pct": str(self.ruin_drawdown_threshold_pct),
        "ruin_probability_pct": str(self.ruin_probability_pct),
        "drawdown_5pct_prob": str(self.drawdown_5pct_prob),
        "drawdown_10pct_prob": str(self.drawdown_10pct_prob),
        "drawdown_15pct_prob": str(self.drawdown_15pct_prob),
        "median_max_drawdown_pct": str(self.median_max_drawdown_pct),
        "p95_max_drawdown_pct": str(self.p95_max_drawdown_pct),
        "p99_max_drawdown_pct": str(self.p99_max_drawdown_pct),
        "median_ending_equity_inr": str(self.median_ending_equity_inr),
        "p05_ending_equity_inr": str(self.p05_ending_equity_inr),
        "p95_ending_equity_inr": str(self.p95_ending_equity_inr),
        "sample_paths": self.sample_paths,
    }


def run_monte_carlo_resampling(
    trade_pnls: list[Decimal],
    starting_capital_inr: Decimal = Decimal("100000.00"),
    iterations: int = 2000,
    ruin_drawdown_pct: Decimal = Decimal("0.10"),
    seed: int | None = 42,
) -> MonteCarloSimulationResult:
  """Runs bootstrap resampling on trade PnLs to evaluate sequence risk and probability of ruin.

  Args:
      trade_pnls: List of net realized trade PnL in INR.
      starting_capital_inr: Reference capital in INR.
      iterations: Number of Monte Carlo permutations (default: 2000).
      ruin_drawdown_pct: Drawdown threshold for ruin (e.g. 0.10 = 10% drawdown).
      seed: Random seed for deterministic reproducibility.

  Returns:
      MonteCarloSimulationResult with percentile distributions and ruin probabilities.
  """
  start_cap = to_decimal(starting_capital_inr)
  ruin_thresh = to_decimal(ruin_drawdown_pct)

  if not trade_pnls:
    return MonteCarloSimulationResult(
        total_iterations=iterations,
        sample_trade_count=0,
        starting_capital_inr=start_cap,
        ruin_drawdown_threshold_pct=ruin_thresh,
        ruin_probability_pct=Decimal("0.00"),
        drawdown_5pct_prob=Decimal("0.00"),
        drawdown_10pct_prob=Decimal("0.00"),
        drawdown_15pct_prob=Decimal("0.00"),
        median_max_drawdown_pct=Decimal("0.00"),
        p95_max_drawdown_pct=Decimal("0.00"),
        p99_max_drawdown_pct=Decimal("0.00"),
        median_ending_equity_inr=start_cap,
        p05_ending_equity_inr=start_cap,
        p95_ending_equity_inr=start_cap,
        sample_paths={},
    )

  rng = random.Random(seed)
  pnls = [to_decimal(p) for p in trade_pnls]
  n_trades = len(pnls)

  max_drawdowns: list[Decimal] = []
  ending_equities: list[Decimal] = []
  ruin_count = 0
  dd_5_count = 0
  dd_10_count = 0
  dd_15_count = 0

  all_paths: list[list[float]] = []

  for _ in range(iterations):
    # Sample with replacement
    sampled_pnls = [rng.choice(pnls) for _ in range(n_trades)]
    equity = start_cap
    peak = start_cap
    max_dd = Decimal("0.00")

    path = [float(start_cap)]
    for p in sampled_pnls:
      equity += p
      if equity > peak:
        peak = equity
      dd = (peak - equity) / peak if peak > Decimal("0") else Decimal("0.00")
      if dd > max_dd:
        max_dd = dd
      path.append(float(equity))

    max_drawdowns.append(max_dd)
    ending_equities.append(equity)
    all_paths.append(path)

    if max_dd >= ruin_thresh:
      ruin_count += 1
    if max_dd >= Decimal("0.05"):
      dd_5_count += 1
    if max_dd >= Decimal("0.10"):
      dd_10_count += 1
    if max_dd >= Decimal("0.15"):
      dd_15_count += 1

  max_drawdowns.sort()
  ending_equities.sort()

  def percentile(sorted_list: list[Decimal], pct: float) -> Decimal:
    idx = int(len(sorted_list) * pct)
    idx = max(0, min(len(sorted_list) - 1, idx))
    return sorted_list[idx]

  iter_dec = Decimal(str(iterations))
  ruin_prob = round_price((Decimal(ruin_count) / iter_dec) * Decimal("100"))
  d5_prob = round_price((Decimal(dd_5_count) / iter_dec) * Decimal("100"))
  d10_prob = round_price((Decimal(dd_10_count) / iter_dec) * Decimal("100"))
  d15_prob = round_price((Decimal(dd_15_count) / iter_dec) * Decimal("100"))

  med_dd = round_price(percentile(max_drawdowns, 0.50) * Decimal("100"))
  p95_dd = round_price(percentile(max_drawdowns, 0.95) * Decimal("100"))
  p99_dd = round_price(percentile(max_drawdowns, 0.99) * Decimal("100"))

  med_eq = round_paise(percentile(ending_equities, 0.50))
  p05_eq = round_paise(percentile(ending_equities, 0.05))
  p95_eq = round_paise(percentile(ending_equities, 0.95))

  # Extract representative paths (worst, median, best ending equity)
  sorted_by_end = sorted(all_paths, key=lambda p: p[-1])
  sample_paths = {
      "worst_5th_pct": sorted_by_end[int(len(sorted_by_end) * 0.05)],
      "median": sorted_by_end[int(len(sorted_by_end) * 0.50)],
      "best_95th_pct": sorted_by_end[int(len(sorted_by_end) * 0.95)],
  }

  return MonteCarloSimulationResult(
      total_iterations=iterations,
      sample_trade_count=n_trades,
      starting_capital_inr=start_cap,
      ruin_drawdown_threshold_pct=ruin_thresh,
      ruin_probability_pct=ruin_prob,
      drawdown_5pct_prob=d5_prob,
      drawdown_10pct_prob=d10_prob,
      drawdown_15pct_prob=d15_prob,
      median_max_drawdown_pct=med_dd,
      p95_max_drawdown_pct=p95_dd,
      p99_max_drawdown_pct=p99_dd,
      median_ending_equity_inr=med_eq,
      p05_ending_equity_inr=p05_eq,
      p95_ending_equity_inr=p95_eq,
      sample_paths=sample_paths,
  )

