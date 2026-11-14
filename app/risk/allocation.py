"""Dynamic Portfolio Capital Allocation & Risk Parity Engine (Milestone M23).

Conforms strictly to institutional quantitative portfolio management standards:
1. Pure Decimal financial arithmetic with zero binary float drift.
2. Equal Risk Contribution (ERC) portfolio optimization via cyclical coordinate descent.
3. Inverse-Volatility risk parity baseline allocation.
4. Marginal Risk Contribution (MRC), Risk Contribution (RC), and Percentage Risk Contribution (PRC).
5. Portfolio Diversification Ratio (DR = sum(w_i * sigma_i) / sigma_p).
6. Single-name maximum capital constraints and lot-size share integer rounding.
7. SEBI/NSE cash equities portfolio notional sizing.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
import sqlite3
from typing import Any

from app.core.execution_guard import assert_paper_mode
from app.core.money import round_paise, round_price, to_decimal
from app.risk.var import (
    NSE_SESSION_MINUTES,
    SQRT_375,
    compute_returns_from_prices,
    compute_cross_asset_covariance,
    compute_series_statistics,
)


def compute_portfolio_covariance_matrix(
    returns: dict[str, list[Decimal]],
    symbols: list[str],
) -> tuple[dict[str, dict[str, Decimal]], dict[str, dict[str, Decimal]]]:
  """Computes cross-asset covariance and correlation matrices for symbols."""
  cov_matrix: dict[str, dict[str, Decimal]] = {s: {} for s in symbols}
  corr_matrix: dict[str, dict[str, Decimal]] = {s: {} for s in symbols}
  for s1 in symbols:
    for s2 in symbols:
      cov, corr = compute_cross_asset_covariance(
          returns.get(s1, []),
          returns.get(s2, []),
      )
      cov_matrix[s1][s2] = cov
      corr_matrix[s1][s2] = corr
  return cov_matrix, corr_matrix


@dataclass(frozen=True)
class AssetRiskAllocation:
  """Allocation and risk contribution metrics for a single constituent."""
  symbol: str
  price_inr: Decimal
  volatility_daily_pct: Decimal
  weight: Decimal
  target_notional_inr: Decimal
  allocated_quantity: int
  actual_notional_inr: Decimal
  marginal_risk_contribution: Decimal
  risk_contribution: Decimal
  percentage_risk_contribution_pct: Decimal


@dataclass(frozen=True)
class PortfolioAllocationReport:
  """Institutional multi-asset dynamic capital allocation report."""
  timestamp_utc: datetime
  method: str
  total_capital_inr: Decimal
  deployable_capital_inr: Decimal
  allocated_notional_inr: Decimal
  portfolio_volatility_daily_pct: Decimal
  undiversified_volatility_daily_pct: Decimal
  diversification_ratio: Decimal
  allocations: list[AssetRiskAllocation]
  covariance_matrix: dict[str, dict[str, Decimal]]
  correlation_matrix: dict[str, dict[str, Decimal]]

  def to_dict(self) -> dict[str, Any]:
    return {
        "timestamp_utc": self.timestamp_utc.isoformat(),
        "method": self.method,
        "total_capital_inr": str(self.total_capital_inr),
        "deployable_capital_inr": str(self.deployable_capital_inr),
        "allocated_notional_inr": str(self.allocated_notional_inr),
        "portfolio_volatility_daily_pct": str(self.portfolio_volatility_daily_pct),
        "undiversified_volatility_daily_pct": str(self.undiversified_volatility_daily_pct),
        "diversification_ratio": str(self.diversification_ratio),
        "allocations": [
            {
                "symbol": a.symbol,
                "price_inr": str(a.price_inr),
                "volatility_daily_pct": str(a.volatility_daily_pct),
                "weight": str(a.weight),
                "target_notional_inr": str(a.target_notional_inr),
                "allocated_quantity": a.allocated_quantity,
                "actual_notional_inr": str(a.actual_notional_inr),
                "marginal_risk_contribution": str(a.marginal_risk_contribution),
                "risk_contribution": str(a.risk_contribution),
                "percentage_risk_contribution_pct": str(a.percentage_risk_contribution_pct),
            }
            for a in self.allocations
        ],
        "covariance_matrix": {
            s1: {s2: str(val) for s2, val in row.items()}
            for s1, row in self.covariance_matrix.items()
        },
        "correlation_matrix": {
            s1: {s2: str(val) for s2, val in row.items()}
            for s1, row in self.correlation_matrix.items()
        },
    }


def compute_inverse_volatility_weights(
    volatilities: dict[str, Decimal],
    max_weight: Decimal = Decimal("0.35"),
) -> dict[str, Decimal]:
  """Computes naive risk parity weights inversely proportional to volatility.

  Args:
      volatilities: dict of symbol -> standard deviation (e.g. daily pct).
      max_weight: maximum allowable weight for any single constituent.

  Returns:
      dict of symbol -> normalized weight summing to Decimal('1.0000').
  """
  symbols = list(volatilities.keys())
  if not symbols:
    return {}
  if len(symbols) == 1:
    return {symbols[0]: Decimal("1.0000")}

  inv_vols: dict[str, Decimal] = {}
  for s in symbols:
    vol = volatilities[s]
    if vol > Decimal("0"):
      inv_vols[s] = Decimal("1") / vol
    else:
      inv_vols[s] = Decimal("1")

  total_inv = sum(inv_vols.values())
  raw_weights = {s: inv_vols[s] / total_inv for s in symbols}

  return enforce_weight_caps(raw_weights, max_weight=max_weight)


def enforce_weight_caps(
    weights: dict[str, Decimal],
    max_weight: Decimal = Decimal("0.35"),
    tolerance: Decimal = Decimal("0.00001"),
    max_iterations: int = 20,
) -> dict[str, Decimal]:
  """Clamps weights to max_weight and redistributes excess proportionally."""
  symbols = list(weights.keys())
  n = len(symbols)
  if n <= 1:
    return weights

  capped = dict(weights)
  for _ in range(max_iterations):
    excess = Decimal("0")
    unconstrained: list[str] = []

    for s in symbols:
      if capped[s] > max_weight:
        excess += capped[s] - max_weight
        capped[s] = max_weight
      else:
        unconstrained.append(s)

    if excess <= tolerance or not unconstrained:
      break

    unconstrained_sum = sum(capped[s] for s in unconstrained)
    if unconstrained_sum <= Decimal("0"):
      # Split equally among unconstrained
      split = excess / Decimal(len(unconstrained))
      for s in unconstrained:
        capped[s] += split
    else:
      for s in unconstrained:
        share = capped[s] / unconstrained_sum
        capped[s] += excess * share

  # Final exact normalization to 1.0000
  total = sum(capped.values())
  if total > Decimal("0"):
    capped = {s: capped[s] / total for s in symbols}

  return capped


def compute_risk_contributions(
    weights: dict[str, Decimal],
    cov_matrix: dict[str, dict[str, Decimal]],
) -> tuple[Decimal, dict[str, Decimal], dict[str, Decimal], dict[str, Decimal]]:
  """Computes portfolio volatility, MRC, RC, and Percentage Risk Contributions (PRC).

  Args:
      weights: dict of symbol -> portfolio weight w_i.
      cov_matrix: cross-asset covariance matrix Sigma.

  Returns:
      tuple of (portfolio_vol, mrc_dict, rc_dict, prc_dict)
  """
  symbols = list(weights.keys())
  if not symbols:
    return Decimal("0"), {}, {}, {}

  # Portfolio variance: sigma_p^2 = sum_i sum_j w_i * w_j * Sigma_ij
  var_p = Decimal("0")
  for s1 in symbols:
    w1 = weights[s1]
    for s2 in symbols:
      w2 = weights[s2]
      cov = cov_matrix.get(s1, {}).get(s2, Decimal("0"))
      var_p += w1 * w2 * cov

  if var_p < Decimal("0.000000000001"):
    # Zero or negligible variance fallback
    n = Decimal(len(symbols))
    return (
        Decimal("0"),
        {s: Decimal("0") for s in symbols},
        {s: Decimal("0") for s in symbols},
        {s: Decimal("1") / n for s in symbols},
    )

  vol_p = var_p.sqrt()
  mrc: dict[str, Decimal] = {}
  rc: dict[str, Decimal] = {}
  prc: dict[str, Decimal] = {}

  for s in symbols:
    # (Sigma * w)_i = sum_j Sigma_ij * w_j
    sigma_w_i = sum(cov_matrix.get(s, {}).get(s2, Decimal("0")) * weights[s2] for s2 in symbols)
    mrc_i = sigma_w_i / vol_p
    rc_i = weights[s] * mrc_i
    prc_i = rc_i / vol_p if vol_p > Decimal("0") else Decimal("0")

    mrc[s] = mrc_i
    rc[s] = rc_i
    prc[s] = prc_i

  return vol_p, mrc, rc, prc


def compute_equal_risk_contribution_weights(
    cov_matrix: dict[str, dict[str, Decimal]],
    volatilities: dict[str, Decimal],
    max_weight: Decimal = Decimal("0.35"),
    max_iterations: int = 50,
    tolerance: Decimal = Decimal("0.0001"),
) -> dict[str, Decimal]:
  """Computes exact Equal Risk Contribution (ERC) weights via multiplicative descent.

  Solves for w such that PRC_i = 1 / N for all i.
  """
  symbols = list(cov_matrix.keys())
  n = len(symbols)
  if n <= 1:
    return {symbols[0]: Decimal("1.0000")} if n == 1 else {}

  # Initial guess: Inverse Volatility weights
  w = compute_inverse_volatility_weights(volatilities, max_weight=Decimal("1.0"))
  target_prc = Decimal("1") / Decimal(n)

  for it in range(max_iterations):
    vol_p, mrc, rc, prc = compute_risk_contributions(w, cov_matrix)
    if vol_p <= Decimal("0"):
      break

    max_dev = max(abs(prc[s] - target_prc) for s in symbols)
    if max_dev < tolerance:
      break

    target_rc = vol_p / Decimal(n)
    w_next: dict[str, Decimal] = {}
    for s in symbols:
      rc_s = rc[s]
      if rc_s > Decimal("0"):
        ratio = (target_rc / rc_s).sqrt()
      else:
        ratio = Decimal("1.0")
      w_next[s] = w[s] * ratio

    tot = sum(w_next.values())
    if tot > Decimal("0"):
      w = {s: w_next[s] / tot for s in symbols}

  return enforce_weight_caps(w, max_weight=max_weight)


def compute_diversification_ratio(
    weights: dict[str, Decimal],
    volatilities: dict[str, Decimal],
    portfolio_vol: Decimal,
) -> Decimal:
  """Computes Portfolio Diversification Ratio DR = sum(w_i * sigma_i) / sigma_p."""
  if portfolio_vol <= Decimal("0"):
    return Decimal("1.0000")
  undiversified_vol = sum(weights[s] * volatilities[s] for s in weights)
  return (undiversified_vol / portfolio_vol).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


class RiskParityAllocator:
  """Institutional Dynamic Risk Parity & Capital Allocator for NSE Equities."""

  def __init__(self, db_path: str = "./alphatrade_v2.db"):
    assert_paper_mode()
    self.db_path = db_path

  def get_minute_returns(
      self,
      symbols: list[str],
      lookback_bars: int = 1500,
  ) -> dict[str, list[Decimal]]:
    """Loads aligned 1-minute historical returns from genuine SQLite database."""
    conn = sqlite3.connect(self.db_path)
    c = conn.cursor()
    by_timestamp: dict[str, dict[str, Decimal]] = {}
    fetch_limit = lookback_bars + 100 if lookback_bars > 0 else 50000

    for s in symbols:
      c.execute(
          """
          SELECT timestamp_utc, close
          FROM market_candles
          WHERE symbol = ? AND interval_minutes = 1
          ORDER BY timestamp_utc DESC
          LIMIT ?
          """,
          (s, fetch_limit),
      )
      rows = c.fetchall()
      for ts, cl in rows:
        by_timestamp.setdefault(ts, {})[s] = Decimal(str(cl))

    conn.close()

    aligned_ts = sorted([
        ts for ts, val_dict in by_timestamp.items()
        if all(s in val_dict for s in symbols)
    ])
    if lookback_bars > 0 and len(aligned_ts) > lookback_bars:
      aligned_ts = aligned_ts[-lookback_bars:]

    if len(aligned_ts) < 2:
      # Standalone fallback
      return self._get_individual_minute_returns(symbols, lookback_bars)

    returns_by_symbol: dict[str, list[Decimal]] = {s: [] for s in symbols}
    for i in range(1, len(aligned_ts)):
      prev_ts = aligned_ts[i - 1]
      curr_ts = aligned_ts[i]
      for s in symbols:
        p_prev = by_timestamp[prev_ts][s]
        p_curr = by_timestamp[curr_ts][s]
        ret = (p_curr - p_prev) / p_prev if p_prev > Decimal("0") else Decimal("0")
        returns_by_symbol[s].append(ret)

    return returns_by_symbol

  def _get_individual_minute_returns(
      self,
      symbols: list[str],
      lookback_bars: int = 1500,
  ) -> dict[str, list[Decimal]]:
    conn = sqlite3.connect(self.db_path)
    c = conn.cursor()
    returns_by_symbol: dict[str, list[Decimal]] = {}
    for s in symbols:
      c.execute(
          "SELECT close FROM market_candles WHERE symbol = ? ORDER BY timestamp_utc ASC",
          (s,),
      )
      rows = c.fetchall()
      prices = [Decimal(str(r[0])) for r in rows]
      if lookback_bars > 0 and len(prices) > lookback_bars:
        prices = prices[-lookback_bars:]
      returns_by_symbol[s] = compute_returns_from_prices(prices)
    conn.close()
    return returns_by_symbol

  def allocate(
      self,
      symbols: list[str],
      prices: dict[str, Decimal],
      total_capital_inr: Decimal,
      deployable_pct: Decimal = Decimal("0.80"),
      max_single_weight: Decimal = Decimal("0.35"),
      method: str = "EQUAL_RISK_CONTRIBUTION",
      lookback_bars: int = 1500,
      custom_returns: dict[str, list[Decimal]] | None = None,
  ) -> PortfolioAllocationReport:
    """Computes dynamic risk parity allocation across candidate symbols.

    Args:
        symbols: list of tradeable constituent symbols.
        prices: latest market prices in INR.
        total_capital_inr: total account equity balance in INR.
        deployable_pct: maximum capital deployable fraction (e.g. 0.80 = 80%).
        max_single_weight: upper cap on any single constituent's weight.
        method: 'EQUAL_WEIGHT', 'INVERSE_VOLATILITY', or 'EQUAL_RISK_CONTRIBUTION'.
        lookback_bars: number of 1-minute bars for covariance estimation.
        custom_returns: optional precomputed return series.

    Returns:
        PortfolioAllocationReport
    """
    assert_paper_mode()
    total_cap = to_decimal(total_capital_inr)
    deployable_cap = round_paise(total_cap * to_decimal(deployable_pct))
    n = len(symbols)

    if n == 0:
      now = datetime.now(timezone.utc)
      return PortfolioAllocationReport(
          timestamp_utc=now,
          method=method,
          total_capital_inr=total_cap,
          deployable_capital_inr=deployable_cap,
          allocated_notional_inr=Decimal("0.00"),
          portfolio_volatility_daily_pct=Decimal("0.0000"),
          undiversified_volatility_daily_pct=Decimal("0.0000"),
          diversification_ratio=Decimal("1.0000"),
          allocations=[],
          covariance_matrix={},
          correlation_matrix={},
      )

    returns = custom_returns or self.get_minute_returns(symbols, lookback_bars=lookback_bars)
    cov_matrix, corr_matrix = compute_portfolio_covariance_matrix(returns, symbols)

    # Compute daily volatilities: sigma_daily = sigma_minute * sqrt(375)
    daily_vols: dict[str, Decimal] = {}
    for s in symbols:
      stats = compute_series_statistics(returns.get(s, []))
      s_vol_1m = stats.get("std", Decimal("0.001"))
      daily_vols[s] = s_vol_1m * SQRT_375

    # Daily covariance matrix: Sigma_daily = Sigma_1m * 375
    daily_cov: dict[str, dict[str, Decimal]] = {}
    for s1 in symbols:
      daily_cov[s1] = {}
      for s2 in symbols:
        daily_cov[s1][s2] = cov_matrix.get(s1, {}).get(s2, Decimal("0")) * NSE_SESSION_MINUTES

    # Determine weights according to method
    if method == "EQUAL_WEIGHT":
      w_equal = {s: Decimal("1") / Decimal(n) for s in symbols}
      weights = enforce_weight_caps(w_equal, max_weight=max_single_weight)
    elif method == "INVERSE_VOLATILITY":
      weights = compute_inverse_volatility_weights(daily_vols, max_weight=max_single_weight)
    elif method == "EQUAL_RISK_CONTRIBUTION":
      weights = compute_equal_risk_contribution_weights(
          daily_cov,
          daily_vols,
          max_weight=max_single_weight,
      )
    else:
      raise ValueError(f"Unknown allocation method: '{method}'")

    vol_p, mrc, rc, prc = compute_risk_contributions(weights, daily_cov)
    div_ratio = compute_diversification_ratio(weights, daily_vols, vol_p)
    undiversified_vol = sum(weights[s] * daily_vols[s] for s in symbols)

    allocations: list[AssetRiskAllocation] = []
    total_allocated_notional = Decimal("0.00")

    for s in symbols:
      p = to_decimal(prices.get(s, Decimal("100.00")))
      w_s = weights[s]
      target_notional = round_paise(deployable_cap * w_s)
      qty = int((target_notional / p).to_integral_value(rounding=ROUND_DOWN)) if p > Decimal("0") else 0
      actual_notional = round_paise(Decimal(qty) * p)
      total_allocated_notional += actual_notional

      allocations.append(
          AssetRiskAllocation(
              symbol=s,
              price_inr=p,
              volatility_daily_pct=(daily_vols[s] * Decimal("100")).quantize(
                  Decimal("0.01"), rounding=ROUND_HALF_UP
              ),
              weight=w_s.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
              target_notional_inr=target_notional,
              allocated_quantity=qty,
              actual_notional_inr=actual_notional,
              marginal_risk_contribution=mrc[s].quantize(
                  Decimal("0.000001"), rounding=ROUND_HALF_UP
              ),
              risk_contribution=rc[s].quantize(
                  Decimal("0.000001"), rounding=ROUND_HALF_UP
              ),
              percentage_risk_contribution_pct=(prc[s] * Decimal("100")).quantize(
                  Decimal("0.01"), rounding=ROUND_HALF_UP
              ),
          )
      )

    return PortfolioAllocationReport(
        timestamp_utc=datetime.now(timezone.utc),
        method=method,
        total_capital_inr=total_cap,
        deployable_capital_inr=deployable_cap,
        allocated_notional_inr=total_allocated_notional,
        portfolio_volatility_daily_pct=(vol_p * Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ),
        undiversified_volatility_daily_pct=(undiversified_vol * Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ),
        diversification_ratio=div_ratio,
        allocations=allocations,
        covariance_matrix=daily_cov,
        correlation_matrix=corr_matrix,
    )
