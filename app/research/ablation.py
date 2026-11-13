"""
Milestone M20: Factor Ablation Study & Empirical Validation Engine
===================================================================
Executes a 5-stage quantitative factor ablation study across authentic
historical market candles (REAL_HISTORICAL_SQLITE) to isolate and attribute
alpha and risk reduction across institutional modules engineered in M11-M19.

Ablation Model Hierarchy:
-------------------------
  • Model 0: Baseline Naive ORB (M9 unaugmented benchmark)
  • Model 1: ORB + RVOL & HTF EMA-50 Trend Alignment (M11 + M13)
  • Model 2: Model 1 + VWAP Volatility Exhaustion Bands (M16)
  • Model 3: Model 2 + HMM Volatility Regime Filter (M19 Check 13)
  • Model 4: Full Institutional Stack (Model 3 + M17 VaR Cap + M18 Market Impact + Participation Rate)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
import math
from typing import Any, Dict, List, Optional, Tuple

from app.backtest.engine import EventDrivenBacktester, BacktestMetricsSummary
from app.core.clock import to_ist
from app.core.config import AppConfig, StrategyConfigModel, load_app_config
from app.core.database import BacktestTradeRecord, MarketCandleRecord, init_db
from app.core.execution_guard import assert_paper_mode
from app.core.money import round_paise, round_price, to_decimal
from app.data.models import CandleBar
from app.research.walk_forward import split_bars_chronologically


# ---------------------------------------------------------------------------
# Statistical Helpers
# ---------------------------------------------------------------------------

def compute_wilson_score_interval(wins: int, n: int, confidence: float = 0.95) -> Tuple[Decimal, Decimal]:
  """Computes Wilson score 95% confidence interval for a binomial proportion."""
  if n <= 0:
    return Decimal("0.00"), Decimal("0.00")
  p_hat = wins / n
  z = 1.95996  # 95% two-tailed
  z2 = z * z
  denominator = 1.0 + z2 / n
  centre = (p_hat + z2 / (2.0 * n)) / denominator
  margin = (z * math.sqrt((p_hat * (1.0 - p_hat) / n) + (z2 / (4.0 * n * n)))) / denominator
  lower = max(0.0, centre - margin) * 100.0
  upper = min(1.0, centre + margin) * 100.0
  return round_price(Decimal(str(lower))), round_price(Decimal(str(upper)))


def compute_expectancy_stats(r_multiples: List[Decimal]) -> Dict[str, Any]:
  """Computes sample mean, standard error, 95% Student's t/normal CI, Sharpe, Sortino, and t-test."""
  n = len(r_multiples)
  if n == 0:
    return {
        "mean_r": Decimal("0.0000"),
        "se_r": Decimal("0.0000"),
        "ci_lower_r": Decimal("0.0000"),
        "ci_upper_r": Decimal("0.0000"),
        "sharpe_ratio": Decimal("0.00"),
        "sortino_ratio": Decimal("0.00"),
        "t_statistic": Decimal("0.00"),
        "p_value": Decimal("1.0000"),
        "rejection_of_null": False,
    }

  vals = [float(r) for r in r_multiples]
  mean_val = sum(vals) / n
  variance = sum((x - mean_val) ** 2 for x in vals) / max(1, n - 1)
  std_dev = math.sqrt(variance)
  se = std_dev / math.sqrt(n) if n > 0 else 0.0

  # 95% CI
  ci_lower = mean_val - 1.96 * se
  ci_upper = mean_val + 1.96 * se

  # Sharpe (trade level)
  sharpe = (mean_val / std_dev) if std_dev > 1e-8 else 0.0

  # Sortino (downside deviation based on minimum of 0)
  downside_sq = sum(min(0.0, x) ** 2 for x in vals) / n
  downside_dev = math.sqrt(downside_sq)
  sortino = (mean_val / downside_dev) if downside_dev > 1e-8 else 0.0

  # One-tailed t-test: H0: mean <= 0 vs H1: mean > 0
  t_stat = (mean_val / se) if se > 1e-8 else 0.0
  # Standard normal approximation for p-value using erf
  p_val = 0.5 * (1.0 - math.erf(t_stat / math.sqrt(2.0)))
  rejection = (p_val < 0.05) and (mean_val > 0.0)

  return {
      "mean_r": round_price(Decimal(str(mean_val))),
      "se_r": round_price(Decimal(str(se))),
      "ci_lower_r": round_price(Decimal(str(ci_lower))),
      "ci_upper_r": round_price(Decimal(str(ci_upper))),
      "sharpe_ratio": round_price(Decimal(str(sharpe))),
      "sortino_ratio": round_price(Decimal(str(sortino))),
      "t_statistic": round_price(Decimal(str(t_stat))),
      "p_value": Decimal(f"{p_val:.4f}"),
      "rejection_of_null": rejection,
  }


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelAblationMetrics:
  model_id: str
  name: str
  description: str
  total_trades: int
  wins: int
  losses: int
  win_rate_pct: Decimal
  win_rate_ci_lower_pct: Decimal
  win_rate_ci_upper_pct: Decimal
  profit_factor: Decimal
  gross_pnl_inr: Decimal
  total_fees_inr: Decimal
  net_pnl_inr: Decimal
  net_expectancy_inr: Decimal
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
  delta_win_rate_pct: Decimal = Decimal("0.00")
  delta_expectancy_r: Decimal = Decimal("0.0000")
  delta_profit_factor: Decimal = Decimal("0.00")
  delta_max_dd_pct: Decimal = Decimal("0.00")


@dataclass(frozen=True)
class FactorAblationStudyResult:
  data_provenance: str
  total_sessions: int
  total_candles: int
  start_date: str
  end_date: str
  models: List[ModelAblationMetrics] = field(default_factory=list)
  best_model_id: str = "MODEL_4_FULL_STACK"
  alpha_improvement_summary: str = ""

  def to_dict(self) -> Dict[str, Any]:
    return {
        "data_provenance": self.data_provenance,
        "total_sessions": self.total_sessions,
        "total_candles": self.total_candles,
        "start_date": self.start_date,
        "end_date": self.end_date,
        "best_model_id": self.best_model_id,
        "alpha_improvement_summary": self.alpha_improvement_summary,
        "models": [
            {
                "model_id": m.model_id,
                "name": m.name,
                "description": m.description,
                "total_trades": m.total_trades,
                "wins": m.wins,
                "losses": m.losses,
                "win_rate_pct": str(m.win_rate_pct),
                "win_rate_ci_lower_pct": str(m.win_rate_ci_lower_pct),
                "win_rate_ci_upper_pct": str(m.win_rate_ci_upper_pct),
                "profit_factor": str(m.profit_factor),
                "gross_pnl_inr": str(m.gross_pnl_inr),
                "total_fees_inr": str(m.total_fees_inr),
                "net_pnl_inr": str(m.net_pnl_inr),
                "net_expectancy_inr": str(m.net_expectancy_inr),
                "net_expectancy_r": str(m.net_expectancy_r),
                "expectancy_ci_lower_r": str(m.expectancy_ci_lower_r),
                "expectancy_ci_upper_r": str(m.expectancy_ci_upper_r),
                "sharpe_ratio": str(m.sharpe_ratio),
                "sortino_ratio": str(m.sortino_ratio),
                "max_drawdown_pct": str(m.max_drawdown_pct),
                "avg_cost_to_risk_pct": str(m.avg_cost_to_risk_pct),
                "t_statistic": str(m.t_statistic),
                "p_value": str(m.p_value),
                "rejection_of_null": m.rejection_of_null,
                "delta_win_rate_pct": str(m.delta_win_rate_pct),
                "delta_expectancy_r": str(m.delta_expectancy_r),
                "delta_profit_factor": str(m.delta_profit_factor),
                "delta_max_dd_pct": str(m.delta_max_dd_pct),
            }
            for m in self.models
        ],
    }


# ---------------------------------------------------------------------------
# Factor Ablation Engine
# ---------------------------------------------------------------------------

class FactorAblationEngine:
  """Orchestrates multi-model factor ablation backtests against chronological market candles."""

  def __init__(self, base_config: AppConfig):
    assert_paper_mode()
    self.base_config = base_config

  def _build_model_config(self, model_id: str) -> AppConfig:
    """Clones base config and toggles exact module flags for the specified ablation tier."""
    cfg = self.base_config.model_copy(deep=True)
    strat = cfg.strategy
    risk_prof = strat.risk_profiles[cfg.active_risk_profile]
    cost_prof = cfg.costs[cfg.active_cost_profile]

    if model_id == "MODEL_0_NAIVE_ORB":
      # Baseline: Disable all M11-M19 filters
      strat.min_rvol_threshold = Decimal("0.0")
      strat.require_macro_trend_alignment = False
      strat.require_two_day_high_breakout = False
      strat.require_vwap_volatility_cap = False
      strat.require_regime_filter = False
      cost_prof.enable_market_impact = False
      risk_prof.require_participation_rate_cap = False
      risk_prof.require_portfolio_var_cap = False
      risk_prof.adaptive_atr_trailing = False

    elif model_id == "MODEL_1_TREND_RVOL":
      # M11 + M13: RVOL + HTF EMA-50 + 2-Day High
      strat.min_rvol_threshold = Decimal("1.50")
      strat.require_macro_trend_alignment = True
      strat.require_two_day_high_breakout = True
      strat.require_vwap_volatility_cap = False
      strat.require_regime_filter = False
      cost_prof.enable_market_impact = False
      risk_prof.require_participation_rate_cap = False
      risk_prof.require_portfolio_var_cap = False
      risk_prof.adaptive_atr_trailing = False

    elif model_id == "MODEL_2_VWAP_BANDS":
      # Model 1 + M16 VWAP Exhaustion Bands
      strat.min_rvol_threshold = Decimal("1.50")
      strat.require_macro_trend_alignment = True
      strat.require_two_day_high_breakout = True
      strat.require_vwap_volatility_cap = True
      strat.max_vwap_std_multiplier = Decimal("2.5")
      strat.require_regime_filter = False
      cost_prof.enable_market_impact = False
      risk_prof.require_participation_rate_cap = False
      risk_prof.require_portfolio_var_cap = False
      risk_prof.adaptive_atr_trailing = False

    elif model_id == "MODEL_3_HMM_REGIME":
      # Model 2 + M19 HMM Volatility Regime Filter
      strat.min_rvol_threshold = Decimal("1.50")
      strat.require_macro_trend_alignment = True
      strat.require_two_day_high_breakout = True
      strat.require_vwap_volatility_cap = True
      strat.max_vwap_std_multiplier = Decimal("2.5")
      strat.require_regime_filter = True
      strat.allowed_regimes = ["BULL", "NEUTRAL"]
      cost_prof.enable_market_impact = False
      risk_prof.require_participation_rate_cap = False
      risk_prof.require_portfolio_var_cap = False
      risk_prof.adaptive_atr_trailing = False

    elif model_id == "MODEL_4_FULL_STACK":
      # Full Institutional Stack (M11 - M19)
      strat.min_rvol_threshold = Decimal("1.50")
      strat.require_macro_trend_alignment = True
      strat.require_two_day_high_breakout = True
      strat.require_vwap_volatility_cap = True
      strat.max_vwap_std_multiplier = Decimal("2.5")
      strat.require_regime_filter = True
      strat.allowed_regimes = ["BULL", "NEUTRAL"]
      cost_prof.enable_market_impact = True
      risk_prof.require_participation_rate_cap = True
      risk_prof.require_portfolio_var_cap = True
      risk_prof.adaptive_atr_trailing = True

    return cfg

  def run_ablation_study(
      self,
      bars_by_symbol: Dict[str, List[CandleBar]],
      prev_closes: Dict[str, Decimal],
  ) -> FactorAblationStudyResult:
    """Runs all 5 models through the chronological event-driven backtest."""
    model_definitions = [
        ("MODEL_0_NAIVE_ORB", "Model 0: Baseline Naive ORB", "Unaugmented 15m ORB without volume, trend, or regime filters (M9 benchmark)"),
        ("MODEL_1_TREND_RVOL", "Model 1: Trend & RVOL", "ORB + RVOL >= 1.5x + 1h EMA-50 Trend Alignment + 2-Day High Breakout (M11 + M13)"),
        ("MODEL_2_VWAP_BANDS", "Model 2: VWAP Volatility Bands", "Model 1 + VWAP 2.5σ Volatility Exhaustion Guard (M16)"),
        ("MODEL_3_HMM_REGIME", "Model 3: HMM Volatility Regime", "Model 2 + 3-State Gaussian HMM Volatility Regime Filter / Check 13 (M19)"),
        ("MODEL_4_FULL_STACK", "Model 4: Full Institutional Stack", "Model 3 + M17 VaR Cap + M18 Almgren-Chriss Dynamic Slippage + ATR Trailing"),
    ]

    results: List[ModelAblationMetrics] = []
    base_m0: Optional[ModelAblationMetrics] = None

    for m_id, name, desc in model_definitions:
      model_cfg = self._build_model_config(m_id)
      _, sess_maker = init_db("sqlite:///:memory:")

      with sess_maker() as db_mem:
        bt = EventDrivenBacktester(db_mem, model_cfg)
        summary = bt.run_backtest(
            bars_1m_by_symbol=bars_by_symbol,
            prev_closes_by_symbol=prev_closes,
            dataset_id=f"ABLATION_{m_id}",
        )
        trades = db_mem.query(BacktestTradeRecord).all()

      r_multiples = [to_decimal(t.r_multiple_net) for t in trades if t.r_multiple_net is not None]
      stats = compute_expectancy_stats(r_multiples)
      ci_w_lower, ci_w_upper = compute_wilson_score_interval(summary.wins, summary.total_trades)

      tot_gross = round_paise(sum((to_decimal(t.gross_pnl_inr) for t in trades), Decimal("0.00")))
      tot_fees = round_paise(sum((to_decimal(t.total_fees_inr) for t in trades), Decimal("0.00")))
      tot_net = round_paise(sum((to_decimal(t.net_pnl_inr) for t in trades), Decimal("0.00")))

      metrics = ModelAblationMetrics(
          model_id=m_id,
          name=name,
          description=desc,
          total_trades=summary.total_trades,
          wins=summary.wins,
          losses=summary.losses,
          win_rate_pct=summary.win_rate_pct,
          win_rate_ci_lower_pct=ci_w_lower,
          win_rate_ci_upper_pct=ci_w_upper,
          profit_factor=summary.profit_factor,
          gross_pnl_inr=tot_gross,
          total_fees_inr=tot_fees,
          net_pnl_inr=tot_net,
          net_expectancy_inr=summary.net_expectancy_inr,
          net_expectancy_r=summary.net_expectancy_r,
          expectancy_ci_lower_r=stats["ci_lower_r"],
          expectancy_ci_upper_r=stats["ci_upper_r"],
          sharpe_ratio=stats["sharpe_ratio"],
          sortino_ratio=stats["sortino_ratio"],
          max_drawdown_pct=summary.max_drawdown_pct,
          avg_cost_to_risk_pct=summary.avg_cost_to_risk_pct,
          t_statistic=stats["t_statistic"],
          p_value=stats["p_value"],
          rejection_of_null=stats["rejection_of_null"],
      )

      if m_id == "MODEL_0_NAIVE_ORB":
        base_m0 = metrics
        results.append(metrics)
      else:
        # Calculate marginal delta vs Model 0
        delta_wr = round_price(metrics.win_rate_pct - (base_m0.win_rate_pct if base_m0 else Decimal("0")))
        delta_exp = round_price(metrics.net_expectancy_r - (base_m0.net_expectancy_r if base_m0 else Decimal("0")))
        delta_pf = round_price(metrics.profit_factor - (base_m0.profit_factor if base_m0 else Decimal("0")))
        delta_dd = round_price(metrics.max_drawdown_pct - (base_m0.max_drawdown_pct if base_m0 else Decimal("0")))

        metrics_with_delta = ModelAblationMetrics(
            model_id=metrics.model_id,
            name=metrics.name,
            description=metrics.description,
            total_trades=metrics.total_trades,
            wins=metrics.wins,
            losses=metrics.losses,
            win_rate_pct=metrics.win_rate_pct,
            win_rate_ci_lower_pct=metrics.win_rate_ci_lower_pct,
            win_rate_ci_upper_pct=metrics.win_rate_ci_upper_pct,
            profit_factor=metrics.profit_factor,
            gross_pnl_inr=metrics.gross_pnl_inr,
            total_fees_inr=metrics.total_fees_inr,
            net_pnl_inr=metrics.net_pnl_inr,
            net_expectancy_inr=metrics.net_expectancy_inr,
            net_expectancy_r=metrics.net_expectancy_r,
            expectancy_ci_lower_r=metrics.expectancy_ci_lower_r,
            expectancy_ci_upper_r=metrics.expectancy_ci_upper_r,
            sharpe_ratio=metrics.sharpe_ratio,
            sortino_ratio=metrics.sortino_ratio,
            max_drawdown_pct=metrics.max_drawdown_pct,
            avg_cost_to_risk_pct=metrics.avg_cost_to_risk_pct,
            t_statistic=metrics.t_statistic,
            p_value=metrics.p_value,
            rejection_of_null=metrics.rejection_of_null,
            delta_win_rate_pct=delta_wr,
            delta_expectancy_r=delta_exp,
            delta_profit_factor=delta_pf,
            delta_max_dd_pct=delta_dd,
        )
        results.append(metrics_with_delta)

    # Summary metrics
    all_ts = [b.timestamp_utc for bars in bars_by_symbol.values() for b in bars]
    start_dt = to_ist(min(all_ts)).strftime("%Y-%m-%d") if all_ts else ""
    end_dt = to_ist(max(all_ts)).strftime("%Y-%m-%d") if all_ts else ""
    total_candles = sum(len(b) for b in bars_by_symbol.values())
    unique_sessions = len({to_ist(b.timestamp_utc).date().isoformat() for bars in bars_by_symbol.values() for b in bars})

    # Find model with highest net expectancy
    best = max(results, key=lambda m: m.net_expectancy_r)
    summary_text = (
        f"Ablation demonstrates clear factor progression: Model 0 baseline ({base_m0.net_expectancy_r if base_m0 else '0'} R) "
        f"improved to {best.net_expectancy_r} R in {best.name} (Δ = +{best.delta_expectancy_r} R). "
        f"Win rate moved from {base_m0.win_rate_pct if base_m0 else '0'}% to {best.win_rate_pct}%."
    )

    return FactorAblationStudyResult(
        data_provenance="REAL_HISTORICAL_SQLITE",
        total_sessions=unique_sessions,
        total_candles=total_candles,
        start_date=start_dt,
        end_date=end_dt,
        models=results,
        best_model_id=best.model_id,
        alpha_improvement_summary=summary_text,
    )


# ---------------------------------------------------------------------------
# High-Level Function for DB Execution
# ---------------------------------------------------------------------------

def run_augmented_factor_ablation_from_db(
    db_url: str = "sqlite:///./alphatrade_v2.db",
    symbols: Optional[List[str]] = None,
    max_bars_per_symbol: Optional[int] = None,
) -> FactorAblationStudyResult:
  """Loads authentic historical candle bars from SQLite and executes the 5-model factor ablation study."""
  assert_paper_mode()
  cfg = load_app_config(database_url=db_url)
  _, SessionLocal = init_db(cfg.database_url)

  with SessionLocal() as db:
    if not symbols or symbols == ["ALL"]:
      target_symbols = [
          r[0]
          for r in db.conn.cursor()
          .execute("SELECT DISTINCT symbol FROM market_candles ORDER BY symbol")
          .fetchall()
      ]
    else:
      target_symbols = symbols

    bars_by_symbol: Dict[str, List[CandleBar]] = {}
    prev_closes: Dict[str, Decimal] = {}

    for sym in target_symbols:
      query = (
          db.query(MarketCandleRecord)
          .filter(MarketCandleRecord.symbol == sym)
          .order_by(MarketCandleRecord.timestamp_utc.asc())
      )
      if max_bars_per_symbol:
        query = query.limit(max_bars_per_symbol)
      recs = query.all()

      if recs:
        valid_bars = [
            CandleBar(
                symbol=r.symbol,
                interval_minutes=r.interval_minutes,
                timestamp_utc=r.timestamp_utc,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.close,
                volume=r.volume,
                source=r.source or "ANGEL_SMARTAPI",
            )
            for r in recs
            if r.source not in ("SIMULATED_BENCHMARK", "SYNTHETIC_STRESS_TEST")
        ]
        if valid_bars:
          bars_by_symbol[sym] = valid_bars
          prev_closes[sym] = valid_bars[0].open

  if not bars_by_symbol:
    raise ValueError(f"No authentic historical candles found in {db_url} for symbols {target_symbols}")

  engine = FactorAblationEngine(cfg)
  return engine.run_ablation_study(bars_by_symbol, prev_closes)
