# Milestone M18: Dynamic Execution Cost, Square-Root Market Impact & Slippage Analytics

**Session Date:** 2026-11-12  
**System Version:** AlphaTrade AI v2.0  
**Author:** DeepMind Agentic AI & Quantitative Execution Desk  
**Status:** COMPLETE & VERIFIED (`77/77` tests passing)

---

## 1. Executive Summary & Objective

In quantitative algorithmic trading and institutional execution, fixed linear transaction cost models (e.g., constant 2.5 bps slippage) severely underestimate execution drag for larger orders or during low-liquidity market regimes. Conversely, fixed models fail to credit small retail or institutional orders executing in highly liquid large-cap stocks.

Milestone M18 implements a pure `Decimal` **Square-Root Market Impact Model** (Almgren-Chriss framework / Barone-Adesi et al. 2008), an **Implementation Shortfall TCA Engine** (Perold 1988), and a deterministic **Participation Rate Liquidity Guard** (`app/execution/impact.py`). It dynamically sizes execution friction based on real-time and historical bar volume, exposes Transaction Cost Analysis (TCA) via REST API (`GET /execution/impact`), and integrates live execution quality analytics into Tab 3 of the web dashboard.

---

## 2. Mathematical Formulation & Architecture

### 2.1 The Square-Root Law of Market Impact
Empirical microstructure research across global equity exchanges demonstrates that price impact does not scale linearly with order size, but follows a universal sublinear square-root relationship with participation rate:
$$\text{Participation Rate: } \rho = \frac{Q}{V_{\text{bar}}}$$
where:
- $Q$ is order quantity in shares.
- $V_{\text{bar}}$ is the reference volume of the execution bar.
- $\sigma_{\text{daily}}$ is the instrument's daily price volatility.

### 2.2 Temporary vs. Permanent Price Impact Decomposition
Following the Almgren-Chriss framework, market impact is decomposed into two distinct economic components:
$$\text{Temporary Impact: } I_{\text{temp}} = \eta \cdot \sigma_{\text{daily}} \cdot \sqrt{\frac{Q}{V_{\text{bar}}}}$$
$$\text{Permanent Impact: } I_{\text{perm}} = \gamma \cdot \sigma_{\text{daily}} \cdot \sqrt{\frac{Q}{V_{\text{bar}}}}$$
$$\text{Total Price Impact: } I_{\text{total}} = (\eta + \gamma) \cdot \sigma_{\text{daily}} \cdot \sqrt{\frac{Q}{V_{\text{bar}}}}$$
where:
- $\eta = 0.50$ (temporary impact coefficient representing immediate order book resilience depletion).
- $\gamma = 0.25$ (permanent impact coefficient representing information leakage into the limit order book).

### 2.3 Effective Fill Price & Implementation Shortfall
The effective simulated fill price incorporates the half-spread and dynamic square-root impact:
$$\text{Buy Order: } P_{\text{fill}} = P_{\text{arrival}} \cdot \left(1 + \text{HalfSpread} + I_{\text{total}}\right)$$
$$\text{Sell Order: } P_{\text{fill}} = P_{\text{arrival}} \cdot \left(1 - \text{HalfSpread} - I_{\text{total}}\right)$$

Implementation Shortfall (IS) benchmarked against the Arrival / Reference Price:
$$\text{IS}_{\text{buy}} = \left(P_{\text{fill}} - P_{\text{arrival}}\right) \cdot Q$$
$$\text{IS}_{\text{sell}} = \left(P_{\text{arrival}} - P_{\text{fill}}\right) \cdot Q$$
$$\text{Total Execution Friction} = \text{IS} + \text{Statutory Fees} + \text{Brokerage}$$

### 2.4 Participation Rate Liquidity Guard (Check 6c)
To prevent market impact from overwhelming the strategy's statistical edge, `RiskEngine` enforces a maximum participation ceiling:
$$\rho = \frac{Q}{V_{\text{bar}}} \le \rho_{\max} \quad (\text{default: } 5.0\%)$$
Orders whose sized quantity exceeds $\lfloor V_{\text{bar}} \cdot \rho_{\max} \rfloor$ are rejected with `PARTICIPATION_RATE_LIMIT_EXCEEDED`.

---

## 3. Empirical Results across Historical Database Executions

Evaluating all 16 historical order fills recorded in SQLite (`alphatrade_v2.db`):

| Metric | Empirical Value | Institutional Benchmark |
| :--- | :--- | :--- |
| **Total Filled Orders Analyzed** | **16 orders** | — |
| **Average Fill Slippage** | **5.14 bps** | $\le 10.0$ bps |
| **Total Implementation Shortfall** | **₹320.24** | $\le 0.50\%$ of turnover |
| **Average Bar Participation Rate** | **0.0698%** | $\le 1.0\%$ (ultra-low impact) |
| **Total Execution Friction (Fees + Shortfall)** | **₹642.36** | — |

### Symbol-Level TCA Breakdown:

| Symbol | Orders | Total Shares | Turnover (INR) | Fees (INR) | Shortfall (INR) | Avg Slippage |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **`RELIANCE`** | 9 | 267 | ₹3,51,871.89 | ₹182.68 | ₹184.93 | **5.25 bps** |
| **`TCS`** | 4 | 50 | ₹1,53,559.15 | ₹81.65 | ₹76.78 | **5.00 bps** |
| **`AXISBANK`** | 2 | 58 | ₹77,235.93 | ₹40.99 | ₹38.62 | **5.00 bps** |
| **`SBIN`** | 1 | 37 | ₹39,854.12 | ₹16.80 | ₹19.92 | **5.00 bps** |

---

## 4. Architecture & Implementation Summary

1. **`app/execution/impact.py`:** Core analytical engine implementing `compute_square_root_market_impact` and `ExecutionAnalyticsEngine`.
2. **`app/execution/costs.py`:** Upgraded `NSECostEngine.compute_leg_costs` and `estimate_roundtrip_cost` to support volume-sensitive dynamic market impact slippage.
3. **`app/risk/engine.py`:** Added Check 6c evaluating candidate quantity against bar volume participation limits (`PARTICIPATION_RATE_LIMIT_EXCEEDED`).
4. **`app/core/config.py` & Config Files:**
   - `CostProfileConfig`: Added `enable_market_impact`, `impact_coefficient_eta`, `impact_coefficient_gamma`.
   - `RiskProfileConfig`: Added `require_participation_rate_cap`, `max_participation_rate_pct`.
5. **`app/api/routes.py`:** Added `GET /execution/impact` returning historical TCA metrics and real-time order impact simulations.
6. **`scripts/run_execution_impact.py`:** Standalone CLI tool for terminal TCA reporting and market impact simulation.
7. **`dashboard/index.html`:** Integrated Execution Quality & Market Impact TCA card on Tab 3 with live symbol breakdown.
8. **`tests/unit/test_market_impact_m18.py`:** 6 unit tests covering mathematical accuracy, boundary compliance, dynamic cost integration, and API routes.

---

## 5. Verification Matrix

| Component | Test File | Status | Notes |
| :--- | :--- | :--- | :--- |
| **Square-Root Impact Math** | `tests/unit/test_market_impact_m18.py` | **PASS** | Verified against analytical vectors |
| **Participation Boundaries** | `tests/unit/test_market_impact_m18.py` | **PASS** | Compliant vs exceeded states verified |
| **Dynamic Cost Engine** | `tests/unit/test_market_impact_m18.py` | **PASS** | Static vs dynamic volume-sensitive slippage |
| **Risk Participation Gate** | `tests/unit/test_market_impact_m18.py` | **PASS** | `PARTICIPATION_RATE_LIMIT_EXCEEDED` rejection |
| **Historical Aggregation** | `tests/unit/test_market_impact_m18.py` | **PASS** | 16 database orders aggregated |
| **FastAPI REST Endpoint** | `tests/unit/test_market_impact_m18.py` | **PASS** | `GET /execution/impact` 200 OK |
| **Unified Test Suite** | `run_tests.py` | **PASS** | `77 passed, 0 failed` across entire repo |

