# Milestone M12: Production Hardening, SQLite WAL Checkpointing, Health Watchdog & Docker Orchestration

**AlphaTrade AI v2.0** — NSE India Intraday Cash Equities • Long-Only 15m ORB • Strictly PAPER Mode  
*Execution Date: 2026-11-11*  
*Baseline Commit: `64ac76f`*  
*Target Milestone: M12 (Production Hardening & Operational Resilience)*

---

## 1. Executive Summary

Milestone **M12** completes the operational resilience and production hardening tier of AlphaTrade AI v2.0. The architecture now guarantees point-in-time state recoverability, automated database log truncation, real-time feed staleness surveillance with an automated dead-man's switch, and rootless container orchestration.

All components adhere strictly to the project's zero-drift, zero-trust constraints:
- **Zero Drift Financial Accounting**: Double-entry ledger states are snapshotted and reconciled before checkpoints.
- **Fail-Closed Market Surveillance**: The feed watchdog trips a dead-man's switch if real-time ticks stall during active NSE trading hours (09:15–15:30 IST), activating the risk engine kill switch.
- **Cryptographic Provenance**: Backups are verified against SHA-256 digests and SQLite `PRAGMA integrity_check`.
- **Rootless Container Security**: Multi-stage Docker container runs under dedicated unprivileged `appuser` (UID 1000) with persistent volume isolation.

---

## 2. Automated SQLite WAL Checkpointing & Backup Manager

### 2.1 WAL Accumulation & Truncation Mechanism
Under high-frequency market ingestion (348,000+ 1m candles), SQLite Write-Ahead Logging (`WAL`) accumulates frames in `alphatrade_v2.db-wal`. To prevent unbounded file growth and memory pressure:
1. `scripts/backup_manager.py` implements deterministic WAL truncation:
   $$\text{PRAGMA wal\_checkpoint(TRUNCATE)};$$
   flushing all committed transactions back into the main `.db` file and resetting the WAL file to 0 bytes.
2. `ScheduledDailyRunner` automatically schedules `nse_wal_checkpoint_1540` at **15:40 IST** (post-market) and immediately following the 15:35 IST daily reconciliation job.

### 2.2 Point-in-Time Online Backup with SHA-256 Checksums
Backups are performed using SQLite's native `sqlite3.Connection.backup()` API in 1,000-page increments, enabling consistent, non-blocking snapshots without locking concurrent read transactions:
- **Cryptographic Hashing**: Backups compute full SHA-256 manifests (`<backup_name>.meta.json`).
- **Integrity Verification**: `verify_backup()` checks both SHA-256 bit-level integrity and SQLite internal structural health (`PRAGMA integrity_check`).
- **Safe Atomic Restoration**: `restore_backup()` verifies integrity prior to restoration and takes an automatic safety copy of any existing active database before overwriting.

---

## 3. Real-Time Feed Watchdog & Dead-Man's Switch

### 3.1 Market Session Awareness
Data feed disruptions during live trading must not fail open. `FeedWatchdog` (`app/services/watchdog.py`) actively monitors tick arrival timestamps:
- **Trading Window Filter**: Active only between 09:15 and 15:30 IST on verified NSE trading days (accounting for public holidays and special muhurat sessions). Outside these hours, the watchdog remains in `IDLE_OFF_HOURS` without raising false alarms.
- **Staleness Threshold**: Default $\Delta t_{\max} = 180\text{ seconds}$.

### 3.2 Dead-Man's Switch Circuit
If $\Delta t_{\text{tick}} > 180\text{s}$ during market hours:
1. The state transitions immediately to `FEED_STALLED`.
2. An urgent alert is queued and dispatched via `WebhookNotificationService`.
3. The watchdog invokes `on_stall_callback`, which trips the RiskEngine kill switch:
   $$\text{RiskEngine.trigger\_kill\_switch}("STALE\_OR\_INVALID\_MARKET\_DATA")$$
   halting all order generation fail-closed.
4. When fresh market ticks resume, the watchdog detects feed recovery, resets `is_stalled = False`, and logs recovery telemetry.

---

## 4. Container Hardening & Docker Orchestration

The container infrastructure was audited and hardened for cloud/on-prem production deployments:
1. **Rootless Security**:
   - Added unprivileged system user `appuser` (UID 1000).
   - Container processes execute strictly as `USER appuser`.
2. **Persistent Storage Isolation**:
   - Application code and dependencies reside in read-only container layers.
   - Mutable state is isolated into persistent host bind-mounts:
     - `/app/data`: SQLite database persistence (`alphatrade_v2.db`).
     - `/app/.cache`: Data provider session tokens and candle cache.
     - `/app/backups`: Cryptographically signed point-in-time database backups.
3. **Fail-Closed Container Healthcheck**:
   - Periodic 30-second health probe queries `http://localhost:8000/health`.
   - Healthcheck reports `DEGRADED` / fails if the watchdog detects stalled market data during active market hours.
4. **Compose Syntax**:
   - Validated cleanly via `docker compose config`.

---

## 5. Verification & Test Suite Results

The comprehensive test suite in `run_tests.py` was expanded to incorporate the new production hardening and watchdog test specifications (`tests/integration/test_production_hardening.py`):

| Test Suite | Tests | Result | Execution Time |
| :--- | :---: | :---: | :---: |
| `unit/test_core_and_indicators.py` | 4 | **PASS** | < 0.2s |
| `unit/test_candle_cache.py` | 1 | **PASS** | < 0.1s |
| `unit/test_risk_and_costs.py` | 3 | **PASS** | < 0.1s |
| `unit/test_angel_provider.py` | 6 | **PASS** | < 0.4s |
| `unit/test_notifications.py` | 4 | **PASS** | < 0.3s |
| `unit/test_volatility_and_regime.py` | 3 | **PASS** | < 0.2s |
| `integration/test_broker_and_recovery.py` | 2 | **PASS** | < 0.5s |
| `integration/test_daily_runner.py` | 4 | **PASS** | < 1.8s |
| `integration/test_production_hardening.py` | 3 | **PASS** | < 0.5s |
| `backtest/test_backtester_and_lookahead.py` | 2 | **PASS** | < 0.4s |
| `backtest/test_walk_forward_oos.py` | 2 | **PASS** | ~22.0s |
| `backtest/test_research_provenance.py` | 6 | **PASS** | < 0.4s |
| `backtest/test_multi_session_backtest.py` | 6 | **PASS** | < 0.3s |
| `safety/test_live_guard_and_edge_cases.py` | 3 | **PASS** | < 0.4s |
| **Total Test Suite** | **49** | **49 PASSED, 0 FAILED** | **27.6s** |

All M12 engineering requirements are fully satisfied, deterministic, and committed to source control.
