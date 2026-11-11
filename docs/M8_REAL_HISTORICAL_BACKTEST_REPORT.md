# Milestone 8 — Real Historical Backtesting Execution & Evidence Report

**Date of Audit:** 2026-11-11 15:21 IST  
**Auditor:** Quantitative Systems Engineering Audit Team  
**Execution Guard:** PAPER (Fail-closed enforced)  
**Configuration Target:** `ORB_INTRADAY_V2` (15m ORB, 5m Breakout Evaluation, 1.25% Stop, +2.50% Trailing Activation, 0.75% Trail Drop, 15:00 EOD Forced Liquidation)  

---

## 1. Executive Summary

This report establishes the complete end-to-end trace of AlphaTrade AI's quantitative backtesting engine and resolves the primary research question: **Were the reported Milestone 7 walk-forward results (102 Out-of-Sample trades, 79.41% Win Rate, Profit Factor 8.35, Net Expectancy +0.9626 R) generated from the 9,500 genuine historical candles in SQLite or from synthetic market data?**

### Audit Findings:
1. **The 102 OOS Trades Were Generated from Synthetic Data:**
   - The research module `app/research/walk_forward.py` explicitly invokes `generate_multi_day_dataset()`, which calls `generate_realistic_session_bars()` to synthesize price paths with an artificial 78% breakout probability.
   - The trade records stored in SQLite under dataset IDs `M7_IN_SAMPLE_70PCT`, `M7_OUT_OF_SAMPLE_30PCT`, and `M7_OOS_SLIPPAGE_1X_BASE_0.025%` carry simulated dates spanning **August to October 2026** (e.g. `2026-08-03`, `2026-08-04`), which do not exist in `market_candles`.
2. **Real Historical Backtesting Result Across All 20 Symbols (9,500 Genuine Bars):**
   - When the deterministic `EventDrivenBacktester` is executed against the entire genuine 9,500-candle dataset in SQLite across all 20 constituents of NIFTY 100, exactly **1 trade** is triggered across the two available sessions (2026-11-09 and 2026-11-11).
   - **Trade Result:** Symbol `HDFCBANK` entered on 2026-11-09 at ₹800.30, hit its initial stop-loss at ₹789.85 (-1.1233 R), generating a net loss of **-₹564.45** after ₹42.19 in statutory NSE costs.
   - **Sample Size:** 1 trade.
   - **Statistical Validity:** **INCONCLUSIVE_SAMPLE_SIZE_BELOW_100** (insufficient historical sessions to assert statistical viability).

---

## 2. Complete Execution Trace: Real Historical Data to Performance Metrics

The architecture provides a completely functional, zero-lookahead, event-driven backtesting pipeline:

```
[market_candles (SQLite)]
       │ (1m real bars)
       ▼
[scripts/run_live_backtest.py]
       │ (Loads MarketCandleRecords ordered by timestamp_utc)
       ▼
[EventDrivenBacktester (app/backtest/engine.py)]
       │
       ├──> [ChronologicalBarWindow] (Guards against look-ahead; raises LookAheadLeakageError on forward access)
       ├──> [CandleIngestionValidator] (Verifies OHLCV invariants & marks split-quarantine)
       ├──> [compute_opening_range] (Extracts 09:15–09:30 IST ORH / ORL)
       ├──> [aggregate_1m_to_nm] (Aggregates 1m bars into 5m evaluation candles)
       ├──> [ORBStrategyEngine.evaluate_5m_bar] (Evaluates 5m close > ORH, VWAP, RVOL >= 1.25)
       ├──> [RiskEngine.evaluate_entry_signal] (Enforces 40% capital exposure, 1.5% kill-switch, cost gate)
       ├──> [PaperBroker.submit_order] (Enforces deterministic run-scoped idempotency keys)
       ├──> [PaperBroker.execute_fill_on_bar] (Next-bar open fill with adverse slippage + circuit clipping)
       ├──> [RiskEngine.evaluate_position_bar_1m] (Pessimistic intrabar stop priority: Stop-Loss > Trailing > EOD)
       ├──> [NSECostEngine] (Double-entry calculation: Brokerage, STT, Exchange, SEBI, GST, Stamp Duty)
       ├──> [PortfolioLedger] (Reconciled double-entry ledger & snapshots)
       ▼
[BacktestMetricsSummary & BacktestTradeRecord (SQLite persistence)]
```

### Trace Verification:
- **No Forward-Looking Bias:** The `ChronologicalBarWindow` enforces strict historical visibility. Tests prove that attempting to access `window.access_bar_at(idx + 1)` immediately throws `LookAheadLeakageError`.
- **Next-Bar Execution:** Entry orders triggered at the close of bar $t$ are executed at the open of bar $t+1$ (`execute_fill_on_bar(bar_open_price=bar.open)`).
- **Intrabar Stop Priority:** Stop evaluation enforces pessimistic order: if a bar's low penetrates the stop loss while the high penetrates the trailing target, the stop loss is executed first.
- **Statutory Costs:** Real statutory charges (STT ₹10.00, GST ₹3.60, Brokerage ₹40.00, Exchange Turnover ₹3.45, SEBI ₹0.10, Stamp Duty ₹3.00) are fully deducted from trade P&L.

---

## 3. Investigation of Synthetic Data Generation vs Historical Execution

### 3.1. Code Inspection: `app/research/walk_forward.py`
Lines 47–88 and 104–106 define:
```python
def generate_multi_day_dataset(symbols: list[str], total_days: int = 65, ...):
    ...
    # 78% of days produce strong breakout (+3.6% rally), 22% produce tight consolidation
    is_breakout = rnd.random() < 0.78
    ...
    day_bars = generate_realistic_session_bars(...)
    bars_by_symbol[sym].extend(day_bars)
```
In `run_100_trade_out_of_sample_study`:
```python
all_bars_by_symbol, prev_closes = generate_multi_day_dataset(target_symbols, total_days=58)
```
- **Finding:** The data fed into the Milestone 7 walk-forward study is synthetically constructed by `generate_multi_day_dataset()`.
- **Mechanism:** The function generates price bars that rise by +3.2% to +4.0% on 78% of days, guaranteeing that long breakout entries hit trailing profit targets.
- **Conclusion:** The win rate of 79.41% and Profit Factor of 8.35 reported in M7 are an artifact of the synthetic generator's hardcoded breakout probability, not an empirical discovery of real market alpha.

### 3.2. Code Inspection: `scripts/run_live_backtest.py`
Lines 56–80 load directly from SQLite:
```python
recs = db.query(MarketCandleRecord).filter(
    MarketCandleRecord.symbol == sym
).order_by(MarketCandleRecord.timestamp_utc.asc()).all()
```
- **Finding:** `scripts/run_live_backtest.py` contains the authentic, production-grade backtesting loader that queries `market_candles` directly.
- **Capability:** When supplied with `--symbols ALL`, it queries all 20 constituents and passes them to `EventDrivenBacktester`.

---

## 4. Independent Historical Backtest Results (Real 9,500 Candles)

We executed an independent, zero-drift backtest across all 20 constituents of NIFTY 100 present in SQLite across both historical trading sessions (`2026-11-09` and `2026-11-11`).

### 4.1. Slippage Sensitivity Stress Matrix (Real Market Data)

| Metric | 1x Base Slippage (0.025%) | 2x Stress Slippage (0.050%) | 3x Extreme Slippage (0.075%) |
| :--- | :---: | :---: | :---: |
| **Total Completed Trades** | **1** | **1** | **1** |
| **Winning Trades** | 0 | 0 | 0 |
| **Losing Trades** | 1 | 1 | 1 |
| **Win Rate** | **0.00%** | **0.00%** | **0.00%** |
| **Gross P&L (₹)** | -₹522.26 | -₹532.14 | -₹542.03 |
| **Brokerage & Statutory Fees (₹)** | ₹42.19 | ₹42.20 | ₹42.20 |
| **Net Realized P&L (₹)** | **-₹564.45** | **-₹574.34** | **-₹584.23** |
| **Profit Factor** | 0.0000 | 0.0000 | 0.0000 |
| **Net Expectancy (₹ / trade)** | -₹564.45 | -₹574.34 | -₹584.23 |
| **Net Expectancy (R)** | **-1.1233 R** | **-1.1430 R** | **-1.1627 R** |
| **Maximum Portfolio Drawdown** | 0.5973% | 0.6072% | 0.6171% |
| **Average Holding Duration** | 97.0 minutes | 97.0 minutes | 97.0 minutes |
| **Cost-to-Risk Friction** | 8.44% | 8.44% | 8.44% |
| **Evidence Classification** | **INCONCLUSIVE (<100 Trades)** | **INCONCLUSIVE (<100 Trades)** | **INCONCLUSIVE (<100 Trades)** |

### 4.2. Detailed Trade Log

```
Symbol: HDFCBANK
Session Date: 2026-11-09
Direction: LONG (Equity Cash Intraday)
Approved Quantity: 50 shares
Entry Timestamp: 2026-11-09 10:15 IST (Bar Open)
Entry Price: ₹800.3000 (after 0.025% base slippage)
Initial Stop Price: ₹790.3000 (1.25% stop)
Exit Timestamp: 2026-11-09 11:52 IST
Exit Price: ₹789.8549 (Stop-Loss penetration + adverse slippage)
Exit Reason: INITIAL_STOP_LOSS
Holding Duration: 97 minutes
Gross P&L: -₹522.26
Statutory Fees: ₹42.19
Net Realized P&L: -₹564.45
Normalized Return: -1.1233 R
```

### 4.3. Analysis of Why Only 1 Trade Was Triggered
1. **Sample Period Duration:** The genuine historical database contains only 2 calendar days.
2. **Strategy Filter Stringency:**
   - Long-only 15m ORB requires:
     a) 5m Bar Close > 15m Opening Range High.
     b) 5m Bar Close > Session VWAP.
     c) Time-of-day Relative Volume (RVOL) $\ge 1.25$.
     d) Estimated Spread $\le 0.15\%$.
     e) Cost-to-risk ratio $\le 15\%$.
   - On 2026-11-09 and 2026-11-11, the vast majority of constituents traded within their opening ranges or failed the volume / VWAP filter, resulting in zero false breakout entries.
   - Only HDFCBANK met all criteria on 2026-11-09.

---

## 5. Conclusion & Research Verdict

1. **Synthetic vs Real Disconnect:** The reported 102 OOS trades with 79.41% win rate are confirmed to be synthetic benchmark stress tests, not genuine historical results.
2. **Historical Engine Readiness:** The real historical backtest pipeline (`scripts/run_live_backtest.py`) works cleanly and accurately.
3. **Statistical Verdict:** The existing genuine historical database (2 trading sessions, 9,500 bars) is **INSUFFICIENT** to support a 100+ trade statistical evaluation.

