# Milestone 8 — Database Persistence, State Recovery & Ledger Reconciliation Report

**Date of Audit:** 2026-11-11 15:24 IST  
**Auditor:** Quantitative Systems Engineering Audit Team  
**Database File:** `alphatrade_v2.db` (SQLite 3.x, WAL mode enabled)  
**Execution Guard:** PAPER (Fail-closed enforced)  

---

## 1. Executive Summary

This report documents the empirical audit of AlphaTrade AI v2.0's disk persistence, restart recovery, double-entry ledger reconciliation, and cross-session isolation. The system was subjected to deliberate process kill events, multi-process concurrency checks, and corrupted-state recovery scenarios.

- **SQLite Database Integrity:** **100% OK** (`PRAGMA integrity_check` returned `[('ok',)]`).
- **Double-Entry Ledger Accounting:** **RECONCILED_OK** (Cash ₹99,355.14 + Deployed Margin ₹0.00 = Net Equity ₹99,355.14).
- **Restart Recovery:** Verified across process restarts. Closed positions, open orders, and cash balances reload deterministically without state corruption.
- **Intraday EOD Liquidation:** Verified. Zero open positions remain at end-of-day.
- **Database Pollution Guard:** Verified. Backtest simulations execute strictly in isolated in-memory databases (`sqlite:///:memory:`), preventing synthetic trade records from corrupting the production ledger.
- **Ingestion Deduplication:** Verified. Zero duplicate `(symbol, timestamp_utc)` pairs in 9,500 historical candle records.

---

## 2. Table-by-Table State Inventory (`alphatrade_v2.db`)

A query of `sqlite_master` and row counts reveals the complete persistent state:

| Table Name | Total Records | Status / Description | Integrity Verification |
| :--- | :---: | :--- | :---: |
| `market_candles` | 9,500 | Genuine 1m historical candles across 20 symbols | Zero duplicate timestamps, zero invalid OHLCV |
| `instruments` | 21 | Eligible universe master definitions | Schema compliant, valid tick sizes |
| `trading_sessions` | 2 | Calendar session records (2026-11-09, 2026-11-11) | Valid status flags |
| `strategy_configs` | 1 | Active strategy parameters snapshot (`ORB_INTRADAY_V2`) | Hash verified |
| `trade_signals` | 7 | Generated trading signals across sessions | State transition: PROPOSED -> EXECUTED |
| `risk_decisions` | 7 | Risk engine audit trail (approvals and rejections) | Approved quantities strictly $\le$ risk budget |
| `orders` | 13 | Paper orders placed across sessions | Unique idempotency keys, no orphan orders |
| `fills` | 12 | Executed order fills with statutory cost itemization | Fill price $\ge$ lower circuit $\land \le$ upper circuit |
| `positions` | 6 | Paper trading positions (all 6 closed) | 0 OPEN, 6 CLOSED, zero negative quantities |
| `ledger_entries` | 23 | Double-entry journal records | Invariant: balance_after = previous + amount |
| `portfolio_snapshots` | 18 | Mark-to-market snapshots across 1m ticks | Cash + Margin + Unrealized = Net Equity |
| `daily_metrics` | 1 | Official session summary snapshot | EOD metric record |
| `backtest_runs` | 12 | Registered backtest run summaries | Correctly scoped run IDs |
| `backtest_trades` | 405 | Historical & synthetic backtest trade logs | Persisted with holding times and R-multiples |
| `audit_events` | 31 | Immutable system audit log (kill switches, restarts) | Monotonically increasing timestamps |

---

## 3. Core Persistence & Recovery Invariants

### 3.1. Double-Entry Accounting Invariant
Every transaction affecting portfolio cash generates a corresponding `LedgerEntryRecord`. The running balance is audited by `PortfolioLedger.reconcile()`:
$$\text{Balance After}_k = \text{Starting Capital} + \sum_{i=1}^k \text{Amount}_i$$
- **Audit Query:**
  ```python
  reconciled, msg = ledger.reconcile()
  ```
- **Result:** `reconciled = True`, `msg = "RECONCILED_OK"`.
- **Cash Balance:** ₹99,355.14
- **Deployed Margin:** ₹0.00
- **Unrealized P&L:** ₹0.00
- **Net Equity:** ₹99,355.14

### 3.2. Intraday EOD Liquidation Invariant
An intraday equity trading system must never hold leveraged or unhedged cash equity positions overnight.
- **Verification:**
  ```sql
  SELECT COUNT(*) FROM positions WHERE status = 'OPEN';
  ```
- **Count:** `0`.
- All 6 paper positions ever opened during runtime have been cleanly closed. The most recent position (`AXISBANK`, `POS_e3028b22a27a`) was liquidated at 15:05 IST via `EOD_FORCED_LIQUIDATION`.

### 3.3. Multi-Process Restart Recovery (`PaperBroker.recover_state_after_restart`)
When the trading runner or API daemon restarts:
1. `recover_state_after_restart()` queries `orders` where `status = 'PENDING'`.
2. Inspects `fills` to see if a fill was already recorded before process death.
3. If filled, transitions order state to `FILLED`; if unacknowledged, cancels or executes depending on timestamp staleness.
4. Loads all `positions` where `status = 'OPEN'` into memory cache.
- **Test Verification:** Unit test `tests/integration/test_broker_and_recovery.py` passes 100% of assertions under simulated mid-transaction crashes.

### 3.4. Backtest Database Isolation Guard
In early development, running backtests against `alphatrade_v2.db` caused synthetic trade records to pollute the live ledger.
- **Current Architecture:** All backtesting routines (`EventDrivenBacktester`, `scripts/run_live_backtest.py`, `app/research/walk_forward.py`) execute in isolated in-memory SQLite instances:
  ```python
  _, BacktestSession = init_db("sqlite:///:memory:")
  ```
- **Result:** Only final summarized run records (`backtest_runs`) and trade logs (`backtest_trades`) are copied back to `alphatrade_v2.db`. The primary `ledger_entries`, `orders`, and `positions` tables remain 100% clean and unpolluted.

### 3.5. Ingestion Deduplication & Idempotency
- When historical or live candles are ingested, `download_historical_candles.py` and `LiveCandleCache` verify existing timestamps before insertion.
- SQLite enforces a unique composite constraint on `(symbol, interval_minutes, timestamp_utc)` via index `sqlite_autoindex_market_candles_1`.
- Attempting to insert a duplicate bar throws `sqlite3.IntegrityError`, causing the transaction to roll back safely.

---

## 4. Disaster Recovery & Rollback Testing

1. **Mid-Trade Power Failure / Process Kill:**
   - Evaluated by terminating uvicorn daemon while orders are pending.
   - Upon restart, `PaperBroker.recover_state_after_restart()` reloaded the ledger, identified the state, and reconciled without double-charging capital.
2. **Database Rollback Integrity:**
   - Any unhandled exception during `job_intraday_tick` invokes `db.rollback()`, ensuring uncommitted partial fills or corrupted balance updates are never persisted.

