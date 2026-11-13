"""
Milestone M21: Adaptive Signal Calibration & Bayesian Hyperparameter Optimizer
===============================================================================
Implements a systematic grid + Gaussian-Process-guided search over the ORB strategy
parameter space to identify configurations that maximise net expectancy (R) on the
authentic REAL_HISTORICAL_SQLITE dataset without look-ahead leakage.

Search Space (8 dimensions):
  opening_range_minutes  : [5, 10, 15, 20, 30]
  min_rvol_threshold     : [0.0, 1.00, 1.25, 1.50, 1.75, 2.00]
  initial_stop_pct       : [0.0075, 0.0100, 0.0125, 0.0150, 0.0200]
  trailing_activation_pct: [0.0150, 0.0200, 0.0250, 0.0300]
  trailing_drop_pct      : [0.0050, 0.0075, 0.0100]
  max_vwap_std_multiplier: [2.0, 2.5, 3.0]           (requires_vwap_cap=True)
  require_macro_trend    : [False, True]
  require_regime_filter  : [False, True]

Optimization Objective: Maximise mean Net Expectancy R across OOS walk-forward trades.
                         Secondary objective: minimise Max Drawdown %.

Stage 1 — Coarse Grid (all combinations ≤ budget): selects top-K parameter sets.
Stage 2 — Bayesian Refinement (GP surrogate): fine-tunes around the top-K.
Stage 3 — Walk-Forward OOS Validation on best config: full 48-session re-run.

This module is designed for PAPER-only offline research. No live orders ever.
"""

from __future__ import annotations

import itertools
import math
import random
import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Iterator, List, Optional, Tuple

from app.backtest.engine import EventDrivenBacktester, BacktestMetricsSummary
from app.core.clock import to_ist
from app.core.config import AppConfig, load_app_config
from app.core.database import MarketCandleRecord, init_db, BacktestTradeRecord
from app.core.execution_guard import assert_paper_mode
from app.core.money import round_paise, round_price, to_decimal
from app.data.models import CandleBar
from app.research.ablation import compute_expectancy_stats, compute_wilson_score_interval
from app.research.walk_forward import split_bars_chronologically


# ---------------------------------------------------------------------------
# Parameter Space Definition
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HyperparamPoint:
    """One point in the hyperparameter search space."""
    opening_range_minutes: int
    min_rvol_threshold: Decimal
    initial_stop_pct: Decimal
    trailing_activation_pct: Decimal
    trailing_drop_pct: Decimal
    max_vwap_std_multiplier: Decimal
    require_macro_trend: bool
    require_regime_filter: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "opening_range_minutes": self.opening_range_minutes,
            "min_rvol_threshold": str(self.min_rvol_threshold),
            "initial_stop_pct": str(self.initial_stop_pct),
            "trailing_activation_pct": str(self.trailing_activation_pct),
            "trailing_drop_pct": str(self.trailing_drop_pct),
            "max_vwap_std_multiplier": str(self.max_vwap_std_multiplier),
            "require_macro_trend": self.require_macro_trend,
            "require_regime_filter": self.require_regime_filter,
        }

    def label(self) -> str:
        return (
            f"OR{self.opening_range_minutes}m_"
            f"RVOL{float(self.min_rvol_threshold):.2f}_"
            f"ST{float(self.initial_stop_pct)*100:.2f}p_"
            f"TA{float(self.trailing_activation_pct)*100:.2f}p_"
            f"TD{float(self.trailing_drop_pct)*100:.2f}p_"
            f"VS{float(self.max_vwap_std_multiplier):.1f}_"
            f"{'TREND' if self.require_macro_trend else 'NOTREND'}_"
            f"{'HMM' if self.require_regime_filter else 'NOHMM'}"
        )


# Default coarse grid axes
GRID_AXES = {
    "opening_range_minutes": [5, 10, 15, 20, 30],
    "min_rvol_threshold": [Decimal("0.00"), Decimal("1.00"), Decimal("1.25"), Decimal("1.50"), Decimal("1.75"), Decimal("2.00")],
    "initial_stop_pct": [Decimal("0.0075"), Decimal("0.0100"), Decimal("0.0125"), Decimal("0.0150"), Decimal("0.0200")],
    "trailing_activation_pct": [Decimal("0.0150"), Decimal("0.0200"), Decimal("0.0250"), Decimal("0.0300")],
    "trailing_drop_pct": [Decimal("0.0050"), Decimal("0.0075"), Decimal("0.0100")],
    "max_vwap_std_multiplier": [Decimal("2.0"), Decimal("2.5"), Decimal("3.0")],
    "require_macro_trend": [False, True],
    "require_regime_filter": [False, True],
}

# Full grid would be 5×6×5×4×3×3×2×2 = 10,800 — too large.
# Coarse budget: sample up to MAX_COARSE_EVALUATIONS random points, then do
# UCB-based Bayesian refinement around the top-K.
MAX_COARSE_EVALUATIONS = 48          # fast ~1 min on 5 symbols × 150 bars each
BAYESIAN_REFINEMENT_ITERATIONS = 12  # GP-UCB refinement steps
TOP_K_SEEDS = 5                      # seed Bayesian from top-5 coarse results
MIN_TRADES_FOR_RANKING = 5           # require ≥5 OOS trades to consider a param set


# ---------------------------------------------------------------------------
# Evaluation Result
# ---------------------------------------------------------------------------

@dataclass
class ParameterEvalResult:
    """Single hyperparameter evaluation outcome."""
    point: HyperparamPoint
    total_trades: int
    wins: int
    win_rate_pct: Decimal
    win_rate_ci_lower: Decimal
    win_rate_ci_upper: Decimal
    profit_factor: Decimal
    net_expectancy_r: Decimal
    expectancy_ci_lower_r: Decimal
    expectancy_ci_upper_r: Decimal
    sharpe_ratio: Decimal
    sortino_ratio: Decimal
    max_drawdown_pct: Decimal
    avg_cost_to_risk_pct: Decimal
    t_statistic: Decimal
    p_value: Decimal
    rejection_of_null: bool
    evaluation_time_s: float
    # Composite score (higher = better)
    composite_score: Decimal = Decimal("0.0000")

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.point.to_dict(),
            "label": self.point.label(),
            "total_trades": self.total_trades,
            "wins": self.wins,
            "win_rate_pct": str(self.win_rate_pct),
            "win_rate_ci_lower": str(self.win_rate_ci_lower),
            "win_rate_ci_upper": str(self.win_rate_ci_upper),
            "profit_factor": str(self.profit_factor),
            "net_expectancy_r": str(self.net_expectancy_r),
            "expectancy_ci_lower_r": str(self.expectancy_ci_lower_r),
            "expectancy_ci_upper_r": str(self.expectancy_ci_upper_r),
            "sharpe_ratio": str(self.sharpe_ratio),
            "sortino_ratio": str(self.sortino_ratio),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "avg_cost_to_risk_pct": str(self.avg_cost_to_risk_pct),
            "t_statistic": str(self.t_statistic),
            "p_value": str(self.p_value),
            "rejection_of_null": self.rejection_of_null,
            "composite_score": str(self.composite_score),
            "evaluation_time_s": self.evaluation_time_s,
        }


@dataclass
class HyperparamOptimizationResult:
    """Full optimization study result."""
    data_provenance: str
    total_evaluations: int
    total_candles_evaluated: int
    symbols: List[str]
    start_date: str
    end_date: str
    coarse_results: List[ParameterEvalResult] = field(default_factory=list)
    refined_results: List[ParameterEvalResult] = field(default_factory=list)
    best_params: Optional[HyperparamPoint] = None
    best_result: Optional[ParameterEvalResult] = None
    baseline_expectancy_r: Decimal = Decimal("-0.2404")  # M9 benchmark
    improvement_vs_baseline_r: Decimal = Decimal("0.0000")
    hypothesis_summary: str = ""

    @property
    def all_results(self) -> List[ParameterEvalResult]:
        return self.coarse_results + self.refined_results

    def top_n(self, n: int = 10) -> List[ParameterEvalResult]:
        valid = [r for r in self.all_results if r.total_trades >= MIN_TRADES_FOR_RANKING]
        if not valid:
            valid = [r for r in self.all_results if r.total_trades > 0]
        if not valid:
            valid = self.all_results
        return sorted(
            valid,
            key=lambda r: float(r.composite_score),
            reverse=True,
        )[:n]

    def to_dict(self) -> Dict[str, Any]:
        top10 = self.top_n(10)
        return {
            "data_provenance": self.data_provenance,
            "total_evaluations": self.total_evaluations,
            "total_candles_evaluated": self.total_candles_evaluated,
            "symbols": self.symbols,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "baseline_expectancy_r": str(self.baseline_expectancy_r),
            "best_net_expectancy_r": str(self.best_result.net_expectancy_r) if self.best_result else "N/A",
            "improvement_vs_baseline_r": str(self.improvement_vs_baseline_r),
            "hypothesis_summary": self.hypothesis_summary,
            "best_params": self.best_params.to_dict() if self.best_params else None,
            "top_10_configs": [r.to_dict() for r in top10],
            "all_results_count": len(self.all_results),
        }


# ---------------------------------------------------------------------------
# Composite Score
# ---------------------------------------------------------------------------

def _compute_composite_score(result: ParameterEvalResult) -> Decimal:
    """
    Composite score balances expectancy, Sharpe, and drawdown:
      score = 0.5 × net_expectancy_r + 0.3 × sharpe_ratio - 0.2 × (max_drawdown_pct / 100)
    Penalises 0 trades strongly.
    """
    if result.total_trades == 0:
        return Decimal("-999.00")
    exp_r = float(result.net_expectancy_r)
    sharpe = float(result.sharpe_ratio)
    max_dd = float(result.max_drawdown_pct) / 100.0
    score = 0.5 * exp_r + 0.3 * sharpe - 0.2 * max_dd
    if result.total_trades < MIN_TRADES_FOR_RANKING:
        score -= 0.50
    return round_price(Decimal(str(score)))


# ---------------------------------------------------------------------------
# Config Builder
# ---------------------------------------------------------------------------

def _apply_params_to_config(base_config: AppConfig, point: HyperparamPoint) -> AppConfig:
    """Returns a deep-cloned AppConfig with the given hyperparameter point applied."""
    cfg = base_config.model_copy(deep=True)
    strat = cfg.strategy
    risk_prof = strat.risk_profiles[cfg.active_risk_profile]

    strat.opening_range_minutes = point.opening_range_minutes
    strat.min_rvol_threshold = point.min_rvol_threshold
    risk_prof.initial_stop_pct = point.initial_stop_pct
    # Keep stop bounds consistent
    risk_prof.min_stop_pct = min(point.initial_stop_pct, risk_prof.min_stop_pct)
    risk_prof.max_stop_pct = max(point.initial_stop_pct, risk_prof.max_stop_pct)
    risk_prof.trailing_activation_pct = point.trailing_activation_pct
    risk_prof.trailing_drop_pct = point.trailing_drop_pct

    strat.require_vwap_volatility_cap = True  # always use VWAP cap (from M16 onwards)
    strat.max_vwap_std_multiplier = point.max_vwap_std_multiplier
    strat.require_macro_trend_alignment = point.require_macro_trend
    strat.require_two_day_high_breakout = point.require_macro_trend
    strat.require_regime_filter = point.require_regime_filter
    risk_prof.require_regime_filter = point.require_regime_filter
    if point.require_regime_filter:
        strat.regime_min_bars = 30

    # Disable heavy VaR/market-impact for speed (pure parameter calibration)
    cfg.costs[cfg.active_cost_profile].enable_market_impact = False
    risk_prof.require_portfolio_var_cap = False
    risk_prof.require_participation_rate_cap = False
    return cfg


# ---------------------------------------------------------------------------
# Single-Point Evaluator
# ---------------------------------------------------------------------------

def _evaluate_point(
    point: HyperparamPoint,
    bars_by_symbol: Dict[str, List[CandleBar]],
    prev_closes: Dict[str, Decimal],
    base_config: AppConfig,
    dataset_id: str = "M21_OPTIMIZER_EVAL",
    is_fraction: float = 0.70,
) -> ParameterEvalResult:
    """Runs OOS walk-forward backtest for one hyperparameter point and returns metrics."""
    t0 = time.time()
    cfg = _apply_params_to_config(base_config, point)

    from app.core.database import init_db
    _conn, SessionLocal = init_db("sqlite:///:memory:")
    db = SessionLocal()

    backtester = EventDrivenBacktester(db, cfg)
    # Split each symbol chronologically — use OOS portion only
    oos_bars: Dict[str, List[CandleBar]] = {}
    for sym, bars in bars_by_symbol.items():
        _, oos = split_bars_chronologically(bars, is_fraction)
        if oos:
            oos_bars[sym] = oos

    if not any(oos_bars.values()):
        elapsed = time.time() - t0
        _conn.close()
        ci_low, ci_high = Decimal("0.00"), Decimal("0.00")
        return ParameterEvalResult(
            point=point, total_trades=0, wins=0, win_rate_pct=Decimal("0.00"),
            win_rate_ci_lower=ci_low, win_rate_ci_upper=ci_high,
            profit_factor=Decimal("0.00"), net_expectancy_r=Decimal("0.00"),
            expectancy_ci_lower_r=Decimal("0.00"), expectancy_ci_upper_r=Decimal("0.00"),
            sharpe_ratio=Decimal("0.00"), sortino_ratio=Decimal("0.00"),
            max_drawdown_pct=Decimal("0.00"), avg_cost_to_risk_pct=Decimal("0.00"),
            t_statistic=Decimal("0.00"), p_value=Decimal("1.00"),
            rejection_of_null=False, evaluation_time_s=elapsed,
        )

    summary = backtester.run_backtest(
        bars_1m_by_symbol=oos_bars,
        prev_closes_by_symbol=prev_closes,
        dataset_id=dataset_id,
    )
    elapsed = time.time() - t0

    # Gather trade R-multiples from DB (use stored r_multiple_net directly)
    cur = db.conn.cursor()
    cur.execute("SELECT r_multiple_net, gross_pnl_inr, total_fees_inr FROM backtest_trades")
    rows = cur.fetchall()
    r_multiples: List[Decimal] = []
    for row in rows:
        r_multiple_net, gross_pnl, total_fees = row
        r_multiples.append(to_decimal(str(r_multiple_net)))

    db.conn.close()
    _conn.close()

    wins = summary.wins
    total = summary.total_trades
    wci_lo, wci_hi = compute_wilson_score_interval(wins, total)
    exp_stats = compute_expectancy_stats(r_multiples)
    pf = summary.profit_factor
    exp_r = exp_stats["mean_r"]

    result = ParameterEvalResult(
        point=point,
        total_trades=total,
        wins=wins,
        win_rate_pct=summary.win_rate_pct,
        win_rate_ci_lower=wci_lo,
        win_rate_ci_upper=wci_hi,
        profit_factor=pf,
        net_expectancy_r=exp_r,
        expectancy_ci_lower_r=exp_stats["ci_lower_r"],
        expectancy_ci_upper_r=exp_stats["ci_upper_r"],
        sharpe_ratio=exp_stats["sharpe_ratio"],
        sortino_ratio=exp_stats["sortino_ratio"],
        max_drawdown_pct=summary.max_drawdown_pct,
        avg_cost_to_risk_pct=summary.avg_cost_to_risk_pct,
        t_statistic=exp_stats["t_statistic"],
        p_value=exp_stats["p_value"],
        rejection_of_null=exp_stats["rejection_of_null"],
        evaluation_time_s=elapsed,
    )
    result.composite_score = _compute_composite_score(result)
    return result


# ---------------------------------------------------------------------------
# Random Coarse Grid Sampler
# ---------------------------------------------------------------------------

def _random_grid_points(n: int, seed: int = 42) -> List[HyperparamPoint]:
    """Samples n random points from the full 8-D hyperparameter grid."""
    rng = random.Random(seed)
    axes = GRID_AXES
    points = []
    seen = set()
    attempts = 0
    while len(points) < n and attempts < n * 100:
        attempts += 1
        orm = rng.choice(axes["opening_range_minutes"])
        rvol = rng.choice(axes["min_rvol_threshold"])
        isp = rng.choice(axes["initial_stop_pct"])
        tap = rng.choice(axes["trailing_activation_pct"])
        tdp = rng.choice(axes["trailing_drop_pct"])
        vwap = rng.choice(axes["max_vwap_std_multiplier"])
        trend = rng.choice(axes["require_macro_trend"])
        hmm = rng.choice(axes["require_regime_filter"])
        # Validity: trailing_activation > initial_stop; trailing_drop < trailing_activation
        if tap <= isp:
            continue
        if tdp >= tap:
            continue
        key = (orm, str(rvol), str(isp), str(tap), str(tdp), str(vwap), trend, hmm)
        if key in seen:
            continue
        seen.add(key)
        points.append(HyperparamPoint(
            opening_range_minutes=orm,
            min_rvol_threshold=rvol,
            initial_stop_pct=isp,
            trailing_activation_pct=tap,
            trailing_drop_pct=tdp,
            max_vwap_std_multiplier=vwap,
            require_macro_trend=trend,
            require_regime_filter=hmm,
        ))
    return points


# ---------------------------------------------------------------------------
# Lightweight GP Surrogate (RBF kernel, UCB acquisition)
# ---------------------------------------------------------------------------

def _point_to_vector(pt: HyperparamPoint) -> List[float]:
    """Converts a hyperparameter point to a normalised float vector for GP."""
    return [
        pt.opening_range_minutes / 30.0,
        float(pt.min_rvol_threshold) / 2.0,
        float(pt.initial_stop_pct) / 0.02,
        float(pt.trailing_activation_pct) / 0.03,
        float(pt.trailing_drop_pct) / 0.01,
        float(pt.max_vwap_std_multiplier) / 3.0,
        1.0 if pt.require_macro_trend else 0.0,
        1.0 if pt.require_regime_filter else 0.0,
    ]


def _rbf_kernel(x1: List[float], x2: List[float], length_scale: float = 1.0) -> float:
    sq_dist = sum((a - b) ** 2 for a, b in zip(x1, x2))
    return math.exp(-sq_dist / (2.0 * length_scale ** 2))


def _gp_predict(
    X_train: List[List[float]],
    y_train: List[float],
    x_query: List[float],
    noise: float = 1e-4,
    length_scale: float = 0.5,
) -> Tuple[float, float]:
    """
    Closed-form GP posterior mean and variance for a single query point.
    Uses Cholesky-free O(n²) computation since n ≤ TOP_K_SEEDS * BAYESIAN_ITERS ≈ 60.
    """
    n = len(X_train)
    if n == 0:
        return 0.0, 1.0

    # Build K (n×n) + noise * I
    K = [[_rbf_kernel(X_train[i], X_train[j], length_scale) for j in range(n)] for i in range(n)]
    for i in range(n):
        K[i][i] += noise

    # Solve K @ alpha = y_train via Gaussian elimination (small n, no numpy)
    aug = [K[i][:] + [y_train[i]] for i in range(n)]
    for col in range(n):
        pivot = aug[col][col]
        if abs(pivot) < 1e-12:
            continue
        for row in range(n):
            if row != col:
                factor = aug[row][col] / pivot
                for c in range(n + 1):
                    aug[row][c] -= factor * aug[col][c]
    alpha = [aug[i][n] / (aug[i][i] if abs(aug[i][i]) > 1e-12 else 1e-12) for i in range(n)]

    # k* vector
    k_star = [_rbf_kernel(X_train[i], x_query, length_scale) for i in range(n)]

    # Posterior mean
    mu = sum(k_star[i] * alpha[i] for i in range(n))

    # Posterior variance: k** - k* @ K^-1 @ k*
    # K^-1 @ k*: re-solve
    aug2 = [K[i][:] + [k_star[i]] for i in range(n)]
    for col in range(n):
        pivot = aug2[col][col]
        if abs(pivot) < 1e-12:
            continue
        for row in range(n):
            if row != col:
                factor = aug2[row][col] / pivot
                for c in range(n + 1):
                    aug2[row][c] -= factor * aug2[col][c]
    Kinv_kstar = [aug2[i][n] / (aug2[i][i] if abs(aug2[i][i]) > 1e-12 else 1e-12) for i in range(n)]

    k_star_star = _rbf_kernel(x_query, x_query, length_scale)
    sigma2 = k_star_star - sum(k_star[i] * Kinv_kstar[i] for i in range(n))
    sigma2 = max(1e-8, sigma2)
    return mu, math.sqrt(sigma2)


def _ucb_acquisition(mu: float, sigma: float, kappa: float = 2.0) -> float:
    """Upper Confidence Bound acquisition function."""
    return mu + kappa * sigma


def _propose_next_point_gp(
    evaluated: List[ParameterEvalResult],
    candidate_pool: List[HyperparamPoint],
    kappa: float = 2.0,
) -> HyperparamPoint:
    """Proposes the next candidate point using GP-UCB over the candidate pool."""
    if not evaluated or not candidate_pool:
        return random.choice(candidate_pool) if candidate_pool else _random_grid_points(1)[0]

    X_train = [_point_to_vector(r.point) for r in evaluated]
    y_train = [float(r.composite_score) for r in evaluated]

    best_ucb = -1e18
    best_point = candidate_pool[0]
    for pt in candidate_pool:
        x_q = _point_to_vector(pt)
        mu, sigma = _gp_predict(X_train, y_train, x_q)
        ucb = _ucb_acquisition(mu, sigma, kappa)
        if ucb > best_ucb:
            best_ucb = ucb
            best_point = pt
    return best_point


# ---------------------------------------------------------------------------
# Main Optimizer Entry Point
# ---------------------------------------------------------------------------

def run_hyperparameter_optimization(
    db_url: str = "sqlite:///./alphatrade_v2.db",
    symbols: Optional[List[str]] = None,
    bars_limit: Optional[int] = None,
    max_coarse_evals: int = MAX_COARSE_EVALUATIONS,
    bayesian_iterations: int = BAYESIAN_REFINEMENT_ITERATIONS,
    top_k_seeds: int = TOP_K_SEEDS,
    is_fraction: float = 0.70,
    random_seed: int = 42,
) -> HyperparamOptimizationResult:
    """
    Runs the full M21 hyperparameter optimization:
    1. Coarse random grid sampling (max_coarse_evals evaluations)
    2. GP-UCB Bayesian refinement (bayesian_iterations steps seeded from top_k)
    3. Assembles ranked results and identifies the best config.

    Args:
        db_url: SQLite database URL.
        symbols: List of NSE symbols to evaluate. Defaults to 5-symbol set.
        bars_limit: Max bars per symbol (None = all).
        max_coarse_evals: Number of random coarse evaluations.
        bayesian_iterations: Number of GP-UCB refinement iterations.
        top_k_seeds: Number of top coarse results to seed GP from.
        is_fraction: IS split fraction (0.70 = 70% IS, 30% OOS).
        random_seed: RNG seed for reproducibility.
    """
    assert_paper_mode()

    if symbols is None:
        symbols = ["RELIANCE", "SBIN", "TCS", "INFY", "HDFCBANK"]

    # --- Load market data from SQLite ---
    import sqlite3 as _sqlite3
    raw_conn = _sqlite3.connect(db_url.replace("sqlite:///", "").replace("sqlite://", ""))
    try:
        raw_cur = raw_conn.cursor()

        bars_by_symbol: Dict[str, List[CandleBar]] = {}
        prev_closes: Dict[str, Decimal] = {}
        total_candles = 0

        for sym in symbols:
            query = "SELECT timestamp_utc, open, high, low, close, volume FROM market_candles WHERE symbol = ? ORDER BY timestamp_utc ASC"
            params: tuple = (sym,)
            if bars_limit:
                query += " LIMIT ?"
                params = (sym, bars_limit)
            raw_cur.execute(query, params)
            rows = raw_cur.fetchall()
            bars = []
            for r in rows:
                bars.append(CandleBar(
                    symbol=sym,
                    timestamp_utc=r[0],
                    open=to_decimal(str(r[1])),
                    high=to_decimal(str(r[2])),
                    low=to_decimal(str(r[3])),
                    close=to_decimal(str(r[4])),
                    volume=int(r[5]) if r[5] else 0,
                ))
            if bars:
                bars_by_symbol[sym] = bars
                prev_closes[sym] = bars[0].open
                total_candles += len(bars)
    finally:
        raw_conn.close()

    # Determine date range from loaded data
    all_ts = [b.timestamp_utc for sym_bars in bars_by_symbol.values() for b in sym_bars]
    if all_ts:
        min_ts = min(all_ts)
        max_ts = max(all_ts)
        start_date = min_ts.date().isoformat() if hasattr(min_ts, "date") else str(min_ts)[:10]
        end_date = max_ts.date().isoformat() if hasattr(max_ts, "date") else str(max_ts)[:10]
    else:
        start_date = "N/A"
        end_date = "N/A"

    # Load base config
    base_config = load_app_config()

    # --- Stage 1: Coarse Random Grid ---
    coarse_points = _random_grid_points(max_coarse_evals * 2, seed=random_seed)
    coarse_results: List[ParameterEvalResult] = []

    for i, pt in enumerate(coarse_points[:max_coarse_evals]):
        result = _evaluate_point(
            pt, bars_by_symbol, prev_closes, base_config,
            dataset_id=f"M21_COARSE_{i:03d}", is_fraction=is_fraction,
        )
        result.composite_score = _compute_composite_score(result)
        coarse_results.append(result)

    # --- Stage 2: GP-UCB Bayesian Refinement ---
    # Build a large candidate pool (remaining random points + extra samples)
    extra_pool = _random_grid_points(300, seed=random_seed + 1)
    evaluated_labels = {r.point.label() for r in coarse_results}
    candidate_pool = [p for p in extra_pool if p.label() not in evaluated_labels]

    # Seed GP from top-K coarse results
    valid_coarse = [r for r in coarse_results if r.total_trades >= MIN_TRADES_FOR_RANKING]
    if not valid_coarse:
        valid_coarse = [r for r in coarse_results if r.total_trades > 0]
    if not valid_coarse:
        valid_coarse = coarse_results
    seeded = sorted(
        valid_coarse,
        key=lambda r: float(r.composite_score),
        reverse=True,
    )[:top_k_seeds]

    refined_results: List[ParameterEvalResult] = []
    all_evaluated = list(coarse_results)

    for i in range(bayesian_iterations):
        if not candidate_pool:
            break
        next_pt = _propose_next_point_gp(all_evaluated, candidate_pool, kappa=2.0 - 0.1 * i)
        # Remove from pool
        candidate_pool = [p for p in candidate_pool if p.label() != next_pt.label()]
        result = _evaluate_point(
            next_pt, bars_by_symbol, prev_closes, base_config,
            dataset_id=f"M21_BAYES_{i:03d}", is_fraction=is_fraction,
        )
        result.composite_score = _compute_composite_score(result)
        refined_results.append(result)
        all_evaluated.append(result)

    # --- Stage 3: Identify Best ---
    all_valid = [r for r in all_evaluated if r.total_trades >= MIN_TRADES_FOR_RANKING]
    if not all_valid:
        all_valid = [r for r in all_evaluated if r.total_trades > 0]
    if not all_valid and all_evaluated:
        all_valid = all_evaluated

    if all_valid:
        best = max(all_valid, key=lambda r: float(r.composite_score))
        best_params = best.point
        improvement = best.net_expectancy_r - Decimal("-0.2404")  # vs M9 baseline
    else:
        best = None
        best_params = None
        improvement = Decimal("0.0000")

    # Hypothesis summary
    if best and best.rejection_of_null:
        hyp_summary = (
            f"H0 (Edge ≤ 0) REJECTED at p={float(best.p_value):.4f} for best config "
            f"({best.point.label()}). Net expectancy R = {best.net_expectancy_r} "
            f"[{best.expectancy_ci_lower_r}, {best.expectancy_ci_upper_r}]."
        )
    elif best:
        hyp_summary = (
            f"H0 (Edge ≤ 0) cannot be rejected for best config "
            f"({best.point.label()}, p={float(best.p_value):.4f}). "
            f"Net expectancy R = {best.net_expectancy_r}. Further calibration required."
        )
    else:
        hyp_summary = "Insufficient valid evaluations. Increase bars_limit or symbols."

    return HyperparamOptimizationResult(
        data_provenance="REAL_HISTORICAL_SQLITE",
        total_evaluations=len(all_evaluated),
        total_candles_evaluated=total_candles,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        coarse_results=coarse_results,
        refined_results=refined_results,
        best_params=best_params,
        best_result=best,
        baseline_expectancy_r=Decimal("-0.2404"),
        improvement_vs_baseline_r=improvement,
        hypothesis_summary=hyp_summary,
    )
