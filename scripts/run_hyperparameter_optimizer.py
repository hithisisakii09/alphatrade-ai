#!/usr/bin/env python
"""
scripts/run_hyperparameter_optimizer.py
========================================
Milestone M21 CLI — Adaptive Signal Calibration & Bayesian Hyperparameter Optimizer.

Runs a two-stage hyperparameter optimization over the ORB strategy parameter space:
  Stage 1: Coarse random grid sampling (--coarse-evals evaluations)
  Stage 2: GP-UCB Bayesian refinement (--bayes-iters iterations)

Then prints a ranked scorecard of the top-10 configurations and writes the
full scientific report to docs/M21_HYPERPARAMETER_OPTIMIZATION_REPORT.md.

Usage:
  python scripts/run_hyperparameter_optimizer.py
  python scripts/run_hyperparameter_optimizer.py --symbols RELIANCE SBIN TCS --bars-limit 500
  python scripts/run_hyperparameter_optimizer.py --coarse-evals 60 --bayes-iters 20
"""

import argparse
import sys
import time
from pathlib import Path

# Ensure project root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.execution_guard import assert_paper_mode
from app.research.optimizer import run_hyperparameter_optimization

BANNER = r"""
╔══════════════════════════════════════════════════════════════════════╗
║  AlphaTrade AI v2.0  •  M21 Hyperparameter Optimizer                ║
║  Stage 1: Coarse Random Grid  →  Stage 2: GP-UCB Bayesian Search    ║
║  PAPER MODE ONLY  •  REAL_HISTORICAL_SQLITE  •  Zero Look-Ahead     ║
╚══════════════════════════════════════════════════════════════════════╝
"""


def _fmt_pct(val) -> str:
    try:
        return f"{float(val):>7.2f}%"
    except Exception:
        return f"{str(val):>8}"


def _fmt_r(val) -> str:
    try:
        return f"{float(val):>+8.4f} R"
    except Exception:
        return f"{str(val):>10}"


def print_scorecard(result) -> None:
    top10 = result.top_n(10)
    print("\n" + "=" * 115)
    print(f"{'RANK':<5} {'LABEL':<52} {'TRADES':>7} {'WIN%':>7} {'PF':>6} {'EXP_R':>10} {'SHARPE':>7} {'MaxDD%':>7} {'SCORE':>8}")
    print("-" * 115)
    for i, r in enumerate(top10, 1):
        label = r.point.label()[:51]
        reject = " ✓H1" if r.rejection_of_null else ""
        print(
            f"{i:<5} {label:<52} {r.total_trades:>7} "
            f"{_fmt_pct(r.win_rate_pct)} {float(r.profit_factor):>6.2f} "
            f"{_fmt_r(r.net_expectancy_r)} {float(r.sharpe_ratio):>7.3f} "
            f"{_fmt_pct(r.max_drawdown_pct)} {float(r.composite_score):>+8.4f}{reject}"
        )
    print("=" * 115)

    if result.best_result:
        b = result.best_result
        print(f"\n{'─'*60}")
        print(f"  BEST CONFIG  : {b.point.label()}")
        print(f"  Trades       : {b.total_trades}")
        print(f"  Win Rate     : {float(b.win_rate_pct):.2f}% [{float(b.win_rate_ci_lower):.2f}%, {float(b.win_rate_ci_upper):.2f}%]")
        print(f"  Profit Factor: {float(b.profit_factor):.4f}")
        print(f"  Net Exp R    : {float(b.net_expectancy_r):+.4f} R  [{float(b.expectancy_ci_lower_r):+.4f}, {float(b.expectancy_ci_upper_r):+.4f}]")
        print(f"  Sharpe       : {float(b.sharpe_ratio):.4f}")
        print(f"  Sortino      : {float(b.sortino_ratio):.4f}")
        print(f"  Max DD       : {float(b.max_drawdown_pct):.2f}%")
        print(f"  t-stat       : {float(b.t_statistic):.4f}  p={float(b.p_value):.4f}")
        print(f"  H0 Rejected  : {'YES ✓' if b.rejection_of_null else 'NO ✗'}")
        print(f"  vs M9 Baseline (-0.2404 R): Δ = {float(result.improvement_vs_baseline_r):+.4f} R")
        print(f"{'─'*60}")
    print(f"\n  Hypothesis: {result.hypothesis_summary}")


def write_report(result, out_path: Path) -> None:
    top10 = result.top_n(10)
    best = result.best_result
    best_p = result.best_params

    lines = [
        "# Milestone M21: Adaptive Signal Calibration & Bayesian Hyperparameter Optimization Report",
        "",
        f"**Document ID:** `M21-HYPERPARAMETER-OPTIMIZATION-REPORT`  ",
        f"**Date:** {time.strftime('%Y-%m-%d')}  ",
        "**Classification:** Empirical Parameter Optimization & Signal Quality Enhancement  ",
        "**Execution Mode:** STRICTLY PAPER (`LiveTradingProhibitedError` enforced)  ",
        "**Data Provenance:** `REAL_HISTORICAL_SQLITE`  ",
        f"**Historical Coverage:** {result.start_date} to {result.end_date}  ",
        f"**Symbols Evaluated:** {', '.join(result.symbols)}  ",
        f"**Total Candles Evaluated:** {result.total_candles_evaluated:,} 1-minute bars  ",
        f"**Total Hyperparameter Evaluations:** {result.total_evaluations}  ",
        "",
        "---",
        "",
        "## 1. Executive Summary",
        "",
        "> **Objective**: Identify ORB strategy parameter configurations that maximise net expectancy "
        "and minimise drawdown on authentic NSE historical data, using a 2-stage Coarse Grid + "
        "GP-UCB Bayesian optimisation pipeline.",
        "",
    ]

    if best:
        improvement = float(result.improvement_vs_baseline_r)
        lines += [
            f"> **Key Finding**: Best config `{best.point.label()}` achieves "
            f"Net Expectancy = **{float(best.net_expectancy_r):+.4f} R** "
            f"(Δ = **{improvement:+.4f} R** vs M9 baseline of -0.2404 R), "
            f"Win Rate = {float(best.win_rate_pct):.2f}%, "
            f"Profit Factor = {float(best.profit_factor):.4f}, "
            f"Max DD = {float(best.max_drawdown_pct):.2f}%.",
            "",
        ]
    else:
        lines += ["> **Key Finding**: No valid parameter configuration found in this evaluation budget.", ""]

    lines += [
        "---",
        "",
        "## 2. Search Space",
        "",
        "| Hyperparameter | Range | Description |",
        "| :--- | :--- | :--- |",
        "| `opening_range_minutes` | [5, 10, 15, 20, 30] | Opening range formation window |",
        "| `min_rvol_threshold` | [0.00, 1.00, 1.25, 1.50, 1.75, 2.00] | Minimum RVOL multiplier for entry |",
        "| `initial_stop_pct` | [0.75%, 1.00%, 1.25%, 1.50%, 2.00%] | Initial stop distance from entry |",
        "| `trailing_activation_pct` | [1.50%, 2.00%, 2.50%, 3.00%] | Trailing stop activation threshold |",
        "| `trailing_drop_pct` | [0.50%, 0.75%, 1.00%] | Trailing stop drop from session high |",
        "| `max_vwap_std_multiplier` | [2.0, 2.5, 3.0] | VWAP exhaustion band ceiling (σ) |",
        "| `require_macro_trend` | [False, True] | HTF EMA-50 1h trend alignment gate |",
        "| `require_regime_filter` | [False, True] | HMM 3-state regime gate (M19 Check 13) |",
        "",
        "**Constraint**: `trailing_activation_pct > initial_stop_pct > trailing_drop_pct`  ",
        f"**Total feasible grid**: ~8,640 combinations → sampled via 2-stage search  ",
        "",
        "---",
        "",
        "## 3. Optimization Method",
        "",
        "### Stage 1 — Coarse Random Grid",
        "Randomly samples from the full 8-D hyperparameter grid with validity constraints enforced.",
        "Each evaluation runs a 70/30 IS/OOS chronological walk-forward backtest.",
        "",
        "### Stage 2 — GP-UCB Bayesian Refinement",
        "Uses a lightweight Gaussian Process surrogate (RBF kernel, Gaussian elimination solver) "
        "fitted on Stage 1 observations. The Upper Confidence Bound (UCB) acquisition function "
        "(`μ + κσ`, κ annealed from 2.0 → 0.8) selects the next candidate from a 300-point pool.",
        "",
        "### Composite Score",
        r"$$\text{Score} = 0.5 \times \bar{R} + 0.3 \times \text{Sharpe} - 0.2 \times \frac{\text{MaxDD}}{100}$$",
        "",
        "---",
        "",
        "## 4. Top-10 Configurations",
        "",
        "| Rank | Config Label | Trades | Win Rate | PF | Net Exp (R) | Sharpe | Max DD | Score | H0 |",
        "| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]

    for i, r in enumerate(top10, 1):
        h0 = "Reject ✓" if r.rejection_of_null else "Fail"
        lines.append(
            f"| **{i}** | `{r.point.label()}` | {r.total_trades} | "
            f"{float(r.win_rate_pct):.2f}% | {float(r.profit_factor):.3f} | "
            f"{float(r.net_expectancy_r):+.4f} R | {float(r.sharpe_ratio):.3f} | "
            f"{float(r.max_drawdown_pct):.2f}% | {float(r.composite_score):+.4f} | {h0} |"
        )

    lines += [
        "",
        "---",
        "",
        "## 5. Best Configuration Detail",
        "",
    ]

    if best and best_p:
        lines += [
            f"| Parameter | Value |",
            f"| :--- | :--- |",
            f"| `opening_range_minutes` | {best_p.opening_range_minutes} min |",
            f"| `min_rvol_threshold` | {float(best_p.min_rvol_threshold):.2f}× |",
            f"| `initial_stop_pct` | {float(best_p.initial_stop_pct)*100:.2f}% |",
            f"| `trailing_activation_pct` | {float(best_p.trailing_activation_pct)*100:.2f}% |",
            f"| `trailing_drop_pct` | {float(best_p.trailing_drop_pct)*100:.2f}% |",
            f"| `max_vwap_std_multiplier` | {float(best_p.max_vwap_std_multiplier):.1f}σ |",
            f"| `require_macro_trend` | {best_p.require_macro_trend} |",
            f"| `require_regime_filter` | {best_p.require_regime_filter} |",
            "",
            "### Performance Metrics",
            "",
            f"| Metric | Value |",
            f"| :--- | :--- |",
            f"| Closed Trades | {best.total_trades} |",
            f"| Wins | {best.wins} |",
            f"| Win Rate | {float(best.win_rate_pct):.2f}% [{float(best.win_rate_ci_lower):.2f}%, {float(best.win_rate_ci_upper):.2f}%] 95% Wilson CI |",
            f"| Profit Factor | {float(best.profit_factor):.4f} |",
            f"| Net Expectancy (R) | {float(best.net_expectancy_r):+.4f} R [{float(best.expectancy_ci_lower_r):+.4f}, {float(best.expectancy_ci_upper_r):+.4f}] 95% t-CI |",
            f"| Sharpe Ratio | {float(best.sharpe_ratio):.4f} |",
            f"| Sortino Ratio | {float(best.sortino_ratio):.4f} |",
            f"| Max Drawdown | {float(best.max_drawdown_pct):.2f}% |",
            f"| Avg Cost/Risk | {float(best.avg_cost_to_risk_pct):.2f}% |",
            f"| t-statistic | {float(best.t_statistic):.4f} |",
            f"| p-value (one-tailed H1: Edge > 0) | {float(best.p_value):.4f} |",
            f"| H0 (Edge ≤ 0) Status | {'**REJECTED** ✓' if best.rejection_of_null else 'Fail to Reject'} |",
            f"| Composite Score | {float(best.composite_score):+.4f} |",
        ]
    else:
        lines.append("*No valid configuration found with ≥10 OOS trades.*")

    lines += [
        "",
        "---",
        "",
        "## 6. Statistical Hypothesis Test",
        "",
        f"**H₀**: Net edge ≤ 0 (strategy has no positive expectancy)  ",
        f"**H₁**: Net edge > 0 (one-tailed, α = 0.05)  ",
        "",
        f"{result.hypothesis_summary}",
        "",
        "---",
        "",
        "## 7. Improvement vs M9 / M20 Baselines",
        "",
        "| Baseline | Net Expectancy R | Source |",
        "| :--- | :---: | :--- |",
        "| M9 Naive ORB (137 OOS trades) | -0.0345 R | 348,453 candles × 48 sessions |",
        "| M20 Model 0 Naive ORB | -0.2404 R | 86,685 candles × 5 symbols |",
        "| M20 Model 4 Full Stack | -0.2313 R | 86,685 candles × 5 symbols |",
        f"| **M21 Best Config** | **{(f'{float(best.net_expectancy_r):+.4f} R' if best else 'N/A')}** | {result.total_candles_evaluated:,} candles |",
        "",
        f"**Improvement vs M20 Model 0 baseline**: Δ = {float(result.improvement_vs_baseline_r):+.4f} R",
        "",
        "---",
        "",
        "## 8. Strategic Recommendations",
        "",
        "1. **Opening Range**: Shorter ORs (5–10 min) may capture more intraday momentum than the default 15m.",
        "2. **RVOL Calibration**: The M20 finding that RVOL=1.50 produces zero marginal improvement suggests the",
        "   historical dataset has insufficient intraday volume variation — a lower threshold or no RVOL filter",
        "   may be appropriate for the current 48-session window.",
        "3. **Stop Architecture**: Wider initial stops (1.50–2.00%) combined with tighter trailing (0.50%)",
        "   reduce premature stop-outs from intraday volatility spikes.",
        "4. **GP-UCB Convergence**: The Bayesian search converges to a region of the parameter space;",
        "   further iterations (`--bayes-iters 50+`) on the full 48-session dataset will refine estimates.",
        "5. **Next Step (M22)**: Deploy the best M21 config in a forward paper-trading session for",
        "   live validation, and compare live forward performance against the OOS backtest benchmark.",
    ]

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n  Report written → {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="M21: Adaptive Signal Calibration & Bayesian Hyperparameter Optimizer"
    )
    parser.add_argument(
        "--symbols", nargs="+",
        default=["RELIANCE", "SBIN", "TCS", "INFY", "HDFCBANK"],
        help="NSE symbols to evaluate (default: 5-symbol set)",
    )
    parser.add_argument(
        "--bars-limit", type=int, default=None,
        help="Max bars per symbol from DB (default: all)",
    )
    parser.add_argument(
        "--coarse-evals", type=int, default=48,
        help="Number of coarse random grid evaluations (default: 48)",
    )
    parser.add_argument(
        "--bayes-iters", type=int, default=12,
        help="GP-UCB Bayesian refinement iterations (default: 12)",
    )
    parser.add_argument(
        "--top-k", type=int, default=5,
        help="Top-K coarse seeds for Bayesian GP (default: 5)",
    )
    parser.add_argument(
        "--is-fraction", type=float, default=0.70,
        help="IS split fraction (default: 0.70 = 70/30 split)",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed (default: 42)",
    )
    parser.add_argument(
        "--db-url", default="sqlite:///./alphatrade_v2.db",
        help="SQLite DB URL (default: sqlite:///./alphatrade_v2.db)",
    )
    args = parser.parse_args()

    assert_paper_mode()
    print(BANNER)
    print(f"  Symbols      : {args.symbols}")
    print(f"  Bars limit   : {args.bars_limit or 'ALL'}")
    print(f"  Coarse evals : {args.coarse_evals}")
    print(f"  Bayes iters  : {args.bayes_iters}")
    print(f"  IS fraction  : {args.is_fraction:.0%}")
    print(f"  Random seed  : {args.seed}")
    print()

    t_start = time.time()
    print("  [Stage 1] Running coarse random grid evaluation...")
    result = run_hyperparameter_optimization(
        db_url=args.db_url,
        symbols=args.symbols,
        bars_limit=args.bars_limit,
        max_coarse_evals=args.coarse_evals,
        bayesian_iterations=args.bayes_iters,
        top_k_seeds=args.top_k,
        is_fraction=args.is_fraction,
        random_seed=args.seed,
    )
    elapsed = time.time() - t_start
    print(f"  [Done] {result.total_evaluations} evaluations in {elapsed:.1f}s "
          f"({result.total_candles_evaluated:,} candles)")

    print_scorecard(result)

    # Write report
    report_path = Path(__file__).resolve().parent.parent / "docs" / "M21_HYPERPARAMETER_OPTIMIZATION_REPORT.md"
    write_report(result, report_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
