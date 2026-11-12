# Milestone M17: Institutional Intraday Portfolio Value-at-Risk (VaR) & Expected Shortfall (CVaR) Engine

**Session Date:** 2026-11-12  
**System Version:** AlphaTrade AI v2.0  
**Author:** DeepMind Agentic AI & Quantitative Research Desk  
**Status:** COMPLETE & VERIFIED (`71/71` tests passing)

---

## 1. Executive Summary & Objective

In Milestones M0 through M16, AlphaTrade AI established single-instrument risk management, trade journaling, Monte Carlo sequence-of-returns ruin modeling, and session VWAP volatility breakout filters. However, institutional asset managers and quantitative hedge funds managing concurrent multi-asset intraday portfolios require aggregate risk quantification adhering to Basel III and Fundamental Review of the Trading Book (FRTB) standards.

Milestone M17 implements a pure `Decimal` multi-asset **Intraday Portfolio Value-at-Risk (VaR)**, **Expected Shortfall (CVaR)**, and **Cross-Asset Correlation Engine** (`app/risk/var.py`). It dynamically monitors portfolio tail risk, quantifies cross-sector diversification benefit, exposes real-time risk analytics via REST API (`GET /risk/var`), and provides a deterministic risk gate in `RiskEngine` (`PORTFOLIO_VAR_LIMIT_EXCEEDED`) preventing portfolio risk concentration.

---

## 2. Mathematical Formulation & Architecture

### 2.1 Pure Decimal Arithmetic & Return Series
All calculations operate using Python's standard `Decimal` module with native `Decimal.sqrt()`, eliminating binary floating-point representation drift:
$$\text{Return: } r_{i, t} = \frac{P_{i, t} - P_{i, t-1}}{P_{i, t-1}}$$

For high-frequency intraday 1-minute bars over an active NSE India trading session (09:15 to 15:30 IST = 375 trading minutes), volatility scales according to the square-root-of-time rule:
$$\sigma_{\text{daily}, i} = \sigma_{1\text{m}, i} \cdot \sqrt{375}$$

### 2.2 Cross-Asset Covariance & Pearson Correlation Matrix
For any two instruments $A$ and $B$ with aligned historical return series of length $N$:
$$\text{Cov}(A, B) = \frac{1}{N - 1} \sum_{t=1}^N \left(r_{A, t} - \mu_A\right)\left(r_{B, t} - \mu_B\right)$$
$$\rho(A, B) = \frac{\text{Cov}(A, B)}{\sigma_A \cdot \sigma_B}$$
$$\mathbf{\Sigma}_{\text{daily}} = \mathbf{\Sigma}_{1\text{m}} \times 375$$

### 2.3 Portfolio Volatility & Variance
Given portfolio position notional weights $w_i = \frac{\text{Exposure}_i}{\text{Equity}}$:
$$\sigma_p = \sqrt{\mathbf{w}^T \mathbf{\Sigma}_{\text{daily}} \mathbf{w}} = \sqrt{\sum_i \sum_j w_i w_j \text{Cov}_{ij}}$$

### 2.4 Parametric Value-at-Risk (VaR)
$$\text{VaR}_{95\%} = Z_{0.95} \cdot \sigma_p \cdot \text{Total Equity} \quad (Z_{0.95} \approx 1.64485)$$
$$\text{VaR}_{99\%} = Z_{0.99} \cdot \sigma_p \cdot \text{Total Equity} \quad (Z_{0.99} \approx 2.32635)$$

### 2.5 Expected Shortfall (CVaR / Conditional VaR)
Parametric Expected Shortfall quantifies the expected loss conditional on the loss exceeding the VaR threshold (mandated by Basel III / FRTB):
$$\text{CVaR}_\alpha = \frac{\phi(Z_\alpha)}{1 - \alpha} \cdot \sigma_p \cdot \text{Total Equity}$$
- For $\alpha = 0.95$: Multiplier $= \frac{\phi(1.64485)}{0.05} \approx 2.06271$
- For $\alpha = 0.99$: Multiplier $= \frac{\phi(2.32635)}{0.01} \approx 2.66521$

### 2.6 Cornish-Fisher Expansion for Non-Normal Empirical Tails
To account for empirical return skewness ($S$) and excess kurtosis ($K$), the Cornish-Fisher expansion adjusts the standard normal quantile for positive loss estimation ($S_{\text{loss}} = -S_{\text{returns}}$):
$$Z_{\text{cf}} = Z + \frac{1}{6}\left(Z^2 - 1\right)(-S) + \frac{1}{24}\left(Z^3 - 3Z\right)K - \frac{1}{36}\left(2Z^3 - 5Z\right)S^2$$
$$\text{Modified VaR} = Z_{\text{cf}} \cdot \sigma_p \cdot \text{Total Equity}$$

### 2.7 Diversification Benefit Ratio
Quantifies the exact percentage reduction in tail loss risk resulting from cross-asset non-unity correlation:
$$\text{Undiversified VaR} = \sum_{i} w_i \cdot \sigma_i \cdot Z_{0.95} \cdot \text{Total Equity}$$
$$\text{Diversification Benefit (\%)} = \left(\frac{\text{Undiversified VaR} - \text{VaR}_p}{\text{Undiversified VaR}}\right) \times 100\%$$

---

## 3. Empirical Results across NIFTY Liquid Constituents

Using 17,337 authentic aligned 1-minute historical bars from the AlphaTrade SQLite database (`alphatrade_v2.db`), the engine computed empirical pairwise correlations:

| Pair | Sector Relationship | Pearson Correlation ($\rho$) | Risk Interpretation |
| :--- | :--- | :--- | :--- |
| **TCS vs INFY** | Intra-Sector (IT Services) | **+0.8691** | High sector co-movement; limited diversification |
| **RELIANCE vs HDFCBANK** | Cross-Sector (Conglomerate vs Banking) | **+0.5635** | Moderate market-beta correlation |
| **RELIANCE vs TCS** | Cross-Sector (Conglomerate vs IT) | **+0.3958** | Strong diversification benefit |
| **INFY vs RELIANCE** | Cross-Sector (IT vs Conglomerate) | **+0.3735** | Strong diversification benefit |

### Portfolio Risk Attribution:

#### 1. Live Paper Portfolio (RELIANCE + TCS):
- **Holdings:** 29 shares RELIANCE (₹38,905.50) + 16 shares TCS (₹39,469.30)
- **Total Exposure:** ₹78,374.80 (79.35% of capital)
- **Individual Daily Volatilities:** RELIANCE: 1.68%, TCS: 2.71%
- **Portfolio Daily Volatility:** **1.48%**
- **95% 1-Day VaR:** **₹2,399.22 (2.43%)**
- **95% Expected Shortfall (CVaR):** **₹3,008.72 (3.05%)**
- **Undiversified Sum-of-VaR:** ₹2,836.50
- **Diversification Benefit:** **15.42% risk reduction**

#### 2. Balanced 4-Asset Equal-Weighted Portfolio (RELIANCE, TCS, INFY, HDFCBANK):
- **Capital:** ₹1,00,000.00
- **Daily Portfolio Volatility:** **1.98%**
- **95% 1-Day VaR:** **₹3,255.88 (3.26%)**
- **95% Expected Shortfall (CVaR):** **₹4,083.00 (4.08%)**
- **Undiversified Sum-of-VaR:** ₹3,989.75
- **Diversification Benefit:** **18.39% risk reduction**

---

## 4. Architecture & Implementation Summary

1. **`app/risk/var.py`:** Core mathematical and statistical engine providing `compute_returns_from_prices`, `compute_series_statistics`, `compute_cross_asset_covariance`, `cornish_fisher_quantile`, and `PortfolioVaREngine`.
2. **`app/core/config.py` & `config/strategy/orb_v2.json`:** Added `require_portfolio_var_cap: bool = False` and `max_portfolio_var_95_pct: Decimal = Decimal("0.025")` across all risk profiles.
3. **`app/risk/engine.py`:** Implemented Check 6b evaluating candidate trade impact on aggregate portfolio VaR, rejecting trades that would breach the risk ceiling with `PORTFOLIO_VAR_LIMIT_EXCEEDED`.
4. **`app/api/routes.py`:** Added `GET /risk/var` supporting live portfolio queries and what-if symbol analysis (`?symbols=RELIANCE,TCS,INFY`).
5. **`scripts/run_var_analysis.py`:** Standalone CLI tool with ASCII correlation matrix formatting and `--json` export.
6. **`dashboard/index.html`:** Integrated interactive VaR, CVaR, and correlation matrix card on Tab 4 with `⚡ Re-Calculate VaR` trigger.
7. **`tests/unit/test_var_and_cvar_m17.py`:** 6 unit tests covering statistical accuracy, quantile expansion, portfolio variance, and gate rejection.

---

## 5. Verification Matrix

| Component | Test File | Status | Notes |
| :--- | :--- | :--- | :--- |
| **Statistical Foundations** | `tests/unit/test_var_and_cvar_m17.py` | **PASS** | Returns, variance, skewness, kurtosis verified |
| **Covariance & Correlation** | `tests/unit/test_var_and_cvar_m17.py` | **PASS** | Identity and inverse symmetry verified |
| **Cornish-Fisher Quantile** | `tests/unit/test_var_and_cvar_m17.py` | **PASS** | Fat-tail quantile expansion verified |
| **Portfolio VaR & CVaR** | `tests/unit/test_var_and_cvar_m17.py` | **PASS** | Multi-asset variance & diversification benefit verified |
| **Deterministic Risk Gate** | `tests/unit/test_var_and_cvar_m17.py` | **PASS** | `PORTFOLIO_VAR_LIMIT_EXCEEDED` rejection verified |
| **FastAPI REST Endpoint** | `tests/unit/test_var_and_cvar_m17.py` | **PASS** | `GET /risk/var` 200 OK verified |
| **Unified Test Suite** | `run_tests.py` | **PASS** | `71 passed, 0 failed` across entire repo |

