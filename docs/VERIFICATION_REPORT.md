# AlphaTrade AI v2.0 — Verified Engineering State & Continuation Report

**Inspection Date:** 10 November 2026  
**Auditor / Engineering Role:** Principal Quantitative Systems Auditor & Continuation Lead  
**Repository:** `alphatrade-ai/` (NSE India Intraday Cash Equities • Long-Only 15m ORB • Automated Paper Trading)  
**Execution Mode:** Strictly `PAPER` (Live Trading Prohibited)  

---

## 1. Phase 1 — Repository & Architecture Audit

### 1.1 Git History & Repository Integrity
- **Repository Version Control Status:** The workspace directory (`c:\Users\akhil\Desktop\Workspace\alphatrade-ai`) does **not** have an initialized `.git` repository (`git status` returned `fatal: not a git repository`). No local commit log exists.
- **File System Integrity:** All core modules under `app/`, configuration files under `config/`, documentation under `docs/`, and test suites under `tests/` are intact and verified.

### 1.2 Architecture & Exit Profile Conflict Resolution
- **Historical Quantitative Defect (Legacy 8/20/10):**
  - Legacy `8/20/10` settings (`8.0%` initial stop, `+20.0%` trailing activation, `10.0%` trailing drop) are structurally mismatched with NSE India intraday cash equities, where individual equities operate under strict daily price bands of `2%`, `5%`, `10%`, or `20%` (and dynamic 10% operating bands for F&O stocks).
  - Under `8/20/10`, intraday positions rarely if ever reach `+20%` in a single session without hitting the daily circuit limit, causing trailing activation to remain dead code while risking catastrophic 8% single-trade drawdowns.
- **Version 2.0 Realignment (`INTRADAY_V2`):**
  - Confirmed active profile: `STANDARD_RESEARCH_100K` in `config/strategy/orb_v2.json`.
  - Calibrated intraday exit rules: `1.25%` initial stop (or Opening Range Low clamped between `0.50%` and `1.50%` below fill), `+2.50%` (`+2R`) trailing activation, and `0.75%` trailing drop.
  - Sizing: Whole-share floor sizing on a `₹1,00,000` virtual research ledger with a strict `<= 15%` round-trip cost-to-risk gate and `1.5%` daily loss kill switch.
  - `LEGACY_SWING_8_20_10` is preserved strictly as an isolated configuration profile for backwards-compatibility unit tests.

### 1.3 Documentation vs. Actual Code Discrepancies
- `PROGRESS.md` and `README.md` previously claimed all milestones (M0–M7) were `PASSED` and complete.
- **Zero-Trust Code Evidence Discrepancies:**
  1. **M0 (Broker Adapters):** Only response JSON parsers exist in `app/data/providers.py`. No live HTTP/WebSocket network calls, `.env` token fetching, or retry mechanisms are implemented.
  2. **M1 / M5 (Persistence Layer):** `app/core/database.py` defines all 17 tables via SQLite DDL, but stores records in an in-memory dictionary (`shared_store`). No SQL `INSERT` or `UPDATE` commands are executed against `alphatrade_v2.db`. On process exit, all records disappear.
  3. **M6 (Dashboard):** `dashboard/index.html` displays static HTML snapshots; asynchronous JavaScript `fetch()` calls to the 11 FastAPI endpoints are not wired up.
  4. **M7 (Walk-Forward Splits):** Backtesting engine metrics logic is implemented, but automated date-based 60% In-Sample / 40% Out-of-Sample splitting runners are absent.

---

## 2. Phase 2 — Runtime Verification & Test Results

### 2.1 Python Runtime Environment Setup
- **Blocker Resolved:** Host system WindowsApps execution alias was disabled, and `.local\bin\python3.15.exe` was blocked by Windows Application Control Policy.
- **Resolution:**
  - Installed official **Python 3.12.10** via `winget` in user scope (`winget install --id Python.Python.3.12 --scope user`).
  - Created isolated virtual environment at `alphatrade-ai\.venv`.
  - Installed verified, zero-cost, open-source dependencies:
    - `fastapi==0.162.2`
    - `pydantic==2.14.3`
    - `sqlalchemy==2.1.8`
    - `pytest==9.2.0`
    - `httpx==0.28.1`
    - `tzdata==2026.6` (required for IANA `Asia/Kolkata` time zone resolution on Windows).

### 2.2 Verbatim Runtime Test Suite Execution
- **Command 1:** `.\.venv\Scripts\python.exe run_tests.py`
  - **Exit Code:** `0`
  - **Summary:** `13 passed, 0 failed`
  - **Output Log:**
    ```text
    [PASS] unit/test_core_and_indicators.py::test_decimal_rejects_binary_float
    [PASS] unit/test_core_and_indicators.py::test_holiday_and_special_session_calendar
    [PASS] unit/test_core_and_indicators.py::test_validator_catches_duplicates_stale_and_unadjusted_splits
    [PASS] unit/test_core_and_indicators.py::test_indicators_vwap_ema_atr_orb_rvol
    [PASS] unit/test_risk_and_costs.py::test_exact_legacy_8_20_10_worked_example
    [PASS] unit/test_risk_and_costs.py::test_v2_intraday_exit_levels_and_pessimistic_intrabar_priority
    [PASS] unit/test_risk_and_costs.py::test_gap_below_stop_uses_conservative_bar_open
    [PASS] integration/test_broker_and_recovery.py::test_idempotency_partial_fills_and_restart_recovery
    [PASS] backtest/test_backtester_and_lookahead.py::test_deliberate_lookahead_bug_caught
    [PASS] backtest/test_backtester_and_lookahead.py::<lambda>
    [PASS] safety/test_live_guard_and_edge_cases.py::<lambda>
    [PASS] safety/test_live_guard_and_edge_cases.py::test_rejected_orders_do_not_count_as_completed_entries_and_kill_switch_edges
    [PASS] safety/test_live_guard_and_edge_cases.py::test_all_11_readonly_fastapi_endpoints

    === TEST SUMMARY: 13 passed, 0 failed ===
    ```

- **Command 2:** `.\.venv\Scripts\python.exe -m pytest -v`
  - **Exit Code:** `0`
  - **Results:** `13 passed, 4 warnings in 1.06s`
  - **Execution Details:**
    - `tests/backtest/test_backtester_and_lookahead.py::test_deliberate_lookahead_bug_caught` -> **PASSED**
    - `tests/backtest/test_backtester_and_lookahead.py::test_reproducible_end_to_end_benchmark_and_api_routes` -> **PASSED**
    - `tests/integration/test_broker_and_recovery.py::test_idempotency_partial_fills_and_restart_recovery` -> **PASSED**
    - `tests/safety/test_live_guard_and_edge_cases.py::test_live_mode_rejected_at_startup_and_by_guard` -> **PASSED**
    - `tests/safety/test_live_guard_and_edge_cases.py::test_rejected_orders_do_not_count_as_completed_entries_and_kill_switch_edges` -> **PASSED**
    - `tests/safety/test_live_guard_and_edge_cases.py::test_all_11_readonly_fastapi_endpoints` -> **PASSED**
    - `tests/unit/test_core_and_indicators.py::test_decimal_rejects_binary_float` -> **PASSED**
    - `tests/unit/test_core_and_indicators.py::test_holiday_and_special_session_calendar` -> **PASSED**
    - `tests/unit/test_core_and_indicators.py::test_validator_catches_duplicates_stale_and_unadjusted_splits` -> **PASSED**
    - `tests/unit/test_core_and_indicators.py::test_indicators_vwap_ema_atr_orb_rvol` -> **PASSED**
    - `tests/unit/test_risk_and_costs.py::test_exact_legacy_8_20_10_worked_example` -> **PASSED**
    - `tests/unit/test_risk_and_costs.py::test_v2_intraday_exit_levels_and_pessimistic_intrabar_priority` -> **PASSED**
    - `tests/unit/test_risk_and_costs.py::test_gap_below_stop_uses_conservative_bar_open` -> **PASSED**

- **Command 3:** `.\.venv\Scripts\python.exe -m app.services.daily_runner`
  - **Exit Code:** `0`
  - **Benchmark Output:**
    ```text
    Benchmark completed: BacktestMetricsSummary(
        run_id='BTR_144edd35e9',
        dataset_id='NSE_BENCHMARK_2026_11_10',
        risk_profile='STANDARD_RESEARCH_100K',
        total_trades=2,
        wins=2,
        losses=0,
        win_rate_pct=Decimal('100.0000'),
        profit_factor=Decimal('9.99'),
        net_expectancy_inr=Decimal('685.68'),
        net_expectancy_r=Decimal('1.4498'),
        max_drawdown_pct=Decimal('0.0000'),
        avg_holding_minutes=Decimal('15.0000'),
        max_consecutive_losses=0,
        rejection_rate_pct=Decimal('83.3333'),
        avg_cost_to_risk_pct=Decimal('8.9200'),
        evidence_classification='INCONCLUSIVE_SAMPLE_SIZE_BELOW_100'
    )
    ```

---

## 3. Phase 3 — Critical Persistence Audit & Remediation Plan

### 3.1 Persistence Mechanism Inspection
In `app/core/database.py`:
- `init_db()` calls `conn.executescript(SCHEMA_DDL)` which creates all 17 tables on the SQLite database file (`alphatrade_v2.db`).
- However, lines 425–431 create a shared Python dictionary `shared_store = {}`.
- The `Session` class:
  ```python
  def add(self, obj: BaseRecord) -> None:
      tbl = obj.__tablename__
      bucket = self._store.setdefault(tbl, [])
      if obj not in bucket:
          bucket.append(obj)
  ```
- `commit()` calls `self.conn.commit()`, but no `INSERT`, `UPDATE`, or `DELETE` SQL statements were ever sent to `conn`.
- `QueryProxy.all()` queries the in-memory Python list `self.session._store.get(...)`.

### 3.2 Runtime Verification of Volatility Across Process Restarts
- **Test:** We executed `app.services.daily_runner` to completion, creating 2 positions, orders, fills, and ledger records.
- **Verification Query:** Executed independent SQLite inspection:
  ```powershell
  python -c "import sqlite3; conn = sqlite3.connect('alphatrade_v2.db'); cur = conn.cursor(); cur.execute('SELECT COUNT(*) FROM positions'); print('Positions in DB file:', cur.fetchone()[0])"
  ```
- **Result:** `Positions in DB file: 0`. Orders and Ledger entries also returned `0`.
- **Finding:** While restart recovery passes inside a single Python process session, it fails completely across actual OS process restarts. **Milestone M5 cannot be considered production-complete until disk persistence is implemented.**

### 3.3 Minimal Implementation Plan for Genuine SQLite Persistence
1. **Model Persistence via Parameterized SQL:**
   - Enhance `BaseRecord` with `.to_dict()` and `.from_row(cursor_description, row)`.
   - Update `Session.add()`: Buffer pending objects.
   - Update `Session.commit()`: Execute parameterized `INSERT INTO {tbl} (...) VALUES (...)` and `UPDATE {tbl} SET ... WHERE id = ?`.
2. **True Disk Queries in `QueryProxy`:**
   - Convert `QueryProxy.filter()` conditions directly into SQL `SELECT * FROM {tbl} WHERE ... ORDER BY ... LIMIT ...`.
3. **Double-Entry Balance Verification:**
   - Ensure `ledger_entries` and `positions` reload accurately upon process boot.

---

## 4. Phase 4 — M0 Data Feasibility & Go/No-Go Report

### 4.1 Provider Comparison Matrix (Verified 2026 Terms)

| Evaluation Criteria | **FYERS API v3** (History API) | **Upstox Developer API v3** | **Angel One SmartAPI** (`getCandleData`) |
| :--- | :--- | :--- | :--- |
| **Pricing / Subscription** | **₹0 base fee** for registered clients | **₹0 base fee** for developer API | **₹0 base fee** for client accounts |
| **Historical 1m Depth** | Back to July 2017 (5+ years depth) | Available from January 2022 | Available across NSE equities |
| **Rate Limits (Standard)** | • 5 requests / second<br>• 50 requests / minute<br>• 5,000 requests / day | • 50 requests / second<br>• 500 requests / minute<br>• 2,000 requests / 30 mins | • 3 requests / second<br>• 180 requests / minute<br>• 5,000 requests / hour |
| **Max Retrieval / Request** | Up to 100 days per request | Max 1 month per request (1m–15m) | Varies by date range |
| **Authentication Flow** | App ID + Access Token (OAuth2) | API Key + Access Token (OAuth2) | API Key + Client Code + TOTP |
| **Automated Terms Permitted** | Personal research & paper trading permitted; redistribution prohibited | Personal algorithmic development & paper trading permitted | Retail client backtesting & algorithmic trading permitted |
| **Audit Recommendation** | **GO (Primary Recommended)** | **GO (Secondary Fallback)** | **CONDITIONAL GO (Strict 3 req/s throttle)** |

### 4.2 Data Feasibility Conclusion
- **Genuine Read-Only Feasibility:** Yes. Both FYERS and Upstox provide zero-cost, legal, documented API access to historical 1m NSE cash equity candles without web scraping or paid data subscriptions.
- **Provider Recommendation:** **FYERS API v3** is recommended as primary due to its 2017 historical depth (satisfying the 100+ trade OOS sample requirement). **Upstox v3** is an ideal high-throughput secondary fallback (50 req/sec limit).
- **Gate:** In accordance with audit rules, **no network data fetching or live broker calls will be initiated until the user provides written approval**.

---

## 5. Phase 5 — Milestone Status Table & Continuation Roadmap

### 5.1 Exact Milestone Status

| Milestone | Target Capability | Documented Claim | Verified Status | Key Blocker / Remaining Action |
| :---: | :--- | :---: | :---: | :--- |
| **M0** | Data Feasibility & Adapters | `PASSED` | **PARTIAL (Parsers Only)** | Waiting for user written approval on provider selection before implementing live HTTP fetching. |
| **M1** | Foundation & Execution Guard | `PASSED` | **COMPLETE (Logic)**<br>**PARTIAL (Storage)** | Core math, calendar, and PAPER guard fully verified. SQLite disk row persistence pending. |
| **M2** | Ingestion & Quality Validation | `PASSED` | **COMPLETE** | 1m OHLC invariants, staleness, and >15% overnight corporate action quarantine fully pass tests. |
| **M3** | Intraday Risk Engine | `PASSED` | **COMPLETE** | `INTRADAY_V2` (1.25% stop / +2.50% activation / 0.75% drop), cost-to-risk gate, and kill switch verified. |
| **M4** | Backtester & Look-Ahead Guard | `PASSED` | **COMPLETE** | Next-bar open execution, intrabar stop priority, and `LookAheadLeakageError` verified in tests. |
| **M5** | Paper Broker & Costs & Recovery | `PASSED` | **PARTIAL (Simulated Only)** | Statutory costs and order lifecycle verified. True crash recovery blocked by in-memory `shared_store`. |
| **M6** | Daily Runner, API & Dashboard | `PASSED` | **PARTIAL** | All 11 FastAPI endpoints pass tests. Dashboard is static HTML; live market daemon not implemented. |
| **M7** | Research Validation (100+ OOS) | `PASSED` | **PARTIAL** | Metrics classification logic verified. Requires historical dataset loader to achieve 100+ OOS trades. |

### 5.2 Next Action
1. **Awaiting User Review & Written Approval:** Please review the provider comparison and approve selecting **FYERS API v3** (or Upstox v3) as the primary data provider.
2. **Immediate Implementation Sequence Upon Approval:**
   - Step 1: Implement genuine SQLite disk persistence in `app/core/database.py` (resolving the M1/M5 persistence gap).
   - Step 2: Implement the authenticated read-only historical candle fetcher in `app/data/providers.py` for the approved provider.
   - Step 3: Run multi-symbol 100+ trade Out-of-Sample historical backtest and verify disk persistence across process termination.

