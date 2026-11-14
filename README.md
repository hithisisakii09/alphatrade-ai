# AlphaTrade AI v2.0

**NSE India • Intraday Cash Equities • Long-Only 15m Opening Range Breakout (ORB) • Deterministic Paper-Trading & Research Engine**

---

## Quick Start (Local)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the unified zero-trust test suite (121/121 tests passing)
python run_tests.py
# or: pytest -v

# 3. Verify Angel One live market data feeder & daily JWT caching
python scripts/verify_angel_data.py

# 4. Ingest multi-month authentic historical candles (Angel One SmartAPI)
python scripts/download_historical_candles.py --start-date 2026-09-01 --end-date 2026-11-11

# 5. Run the empirical walk-forward quantitative study (70% IS / 30% OOS)
python scripts/run_walk_forward_study.py --source HISTORICAL

# 6. Run the Monte Carlo Bootstrap & Risk of Ruin simulator
python scripts/run_monte_carlo.py --iterations 5000

# 7. Run the Portfolio Value-at-Risk (VaR) & Expected Shortfall (CVaR) analyzer
python scripts/run_var_analysis.py --symbols RELIANCE,TCS,INFY,HDFCBANK

# 8. Run the Market Impact & Implementation Shortfall TCA analyzer
python scripts/run_execution_impact.py --symbol RELIANCE --quantity 100

# 9. Run the HMM Volatility Regime Analyzer
python scripts/run_regime_analysis.py --symbol RELIANCE --bars 120

# 10. Run the M20 Augmented Multi-Month Walk-Forward & Factor Ablation Study
python scripts/run_augmented_walk_forward.py --symbols RELIANCE,SBIN,TCS,INFY,HDFCBANK

# 11. Run the M21 Adaptive Signal Calibration & Bayesian Hyperparameter Optimizer
python scripts/run_hyperparameter_optimizer.py --symbols RELIANCE,SBIN,TCS,INFY,HDFCBANK

# 12. Run the M22 Continuous Multi-Session Paper Soak & Drift Surveillance
python scripts/run_multi_session_soak.py --profile CALIBRATED_RESEARCH_M21 --max-sessions 15

# 13. Run the M23 Dynamic Portfolio Capital Allocation & Margin Stress Analysis
python scripts/run_capital_allocation.py --symbols RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK,SBIN --capital 100000.00

# 14. Run the M24 Multi-Broker Execution Gateway Fault Injection Soak
python scripts/run_gateway_reconciliation.py

# 15. Launch the live FastAPI server & dynamic 5-second polling dashboard
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/dashboard/` in your browser.

---

## Production Hardening & Disaster Recovery (`scripts/backup_manager.py`)

```bash
# Execute immediate SQLite WAL TRUNCATE checkpoint
python scripts/backup_manager.py --checkpoint

# Create point-in-time database snapshot with SHA-256 cryptographic manifest
python scripts/backup_manager.py --backup

# List and verify integrity of all backups
python scripts/backup_manager.py --list
python scripts/backup_manager.py --verify backups/<backup_file>.db

# Restore database safely with automated pre-restore backup
python scripts/backup_manager.py --restore backups/<backup_file>.db
```

---

## Docker Deployment

```bash
# Build and run rootless containerized daemon (appuser:1000) with persistent SQLite database
docker compose up -d

# Check health and container status
docker compose ps
curl http://localhost:8000/health
```

---

## Version 2.0 Architectural Highlights
- **Fail-Closed Safety Guard (`app/core/execution_guard.py`)**: Strictly enforces `PAPER` mode at startup and prior to every order placement. Any attempt to set `LIVE` raises `LiveTradingProhibitedError`.
- **Intraday Live Market Ingestion & Breakout Loop (`app/services/daily_runner.py`)**: Ingests real-time 1m candles via Angel One SmartAPI, computes 15m Opening Range (09:15–09:30 IST), evaluates breakouts on 5m candles, manages trailing stops (+2.50% activation, 0.75% trail, 1.25% stop-loss), and force-liquidates positions at 15:00 IST.
- **Feed Watchdog & Dead-Man's Switch (`app/services/watchdog.py`)**: Real-time surveillance of tick arrivals during active market hours (09:15–15:30 IST). Tripping the 180s staleness threshold triggers emergency webhooks and activates the risk engine kill switch fail-closed.
- **Regime & Volatility Adaptability (`app/strategy/orb/engine.py`, `app/risk/engine.py`)**: Enforces Relative Volume ($\text{RVOL} \ge 1.50\times$) breakout filter, Wilder's ATR(14) dynamic volatility sizing, and sector concentration limits ($\le 1$ position per sector).
- **Automated WAL Checkpointing & Backups**: Daily 15:40 IST SQLite WAL truncation (`PRAGMA wal_checkpoint(TRUNCATE)`) and point-in-time online backup snapshots with SHA-256 cryptographic integrity verification.
- **Non-Blocking Telemetry & Webhooks (`app/services/notifications.py`)**: High-resolution performance profiling ($< 5\text{ms}$ avg execution tick latency) with daemon background threads for webhook dispatch (Discord & Telegram), eliminating execution blocking.
- **Empirical Walk-Forward Validation (`app/research/walk_forward.py`)**: Evaluated on **348,453 validated historical candles across 48 continuous trading sessions**, producing 137 Out-of-Sample trades qualifying for `ELIGIBLE_FOR_STATISTICAL_EVALUATION` standard.
- **M20 Factor Ablation Study (`app/research/ablation.py`)**: 5-stage comparative factor ablation study across 86,685 authentic 1-minute bars evaluating Model 0 (Naive ORB) through Model 4 (Full Institutional Stack) with Wilson Score CIs, Student's t hypothesis testing, Sharpe/Sortino ratios, and marginal alpha attribution. Results exposed via `GET /research/augmented-study`. Report in `docs/M20_AUGMENTED_WALK_FORWARD_REPORT.md`.
- **M21 Bayesian Hyperparameter Optimizer (`app/research/optimizer.py`)**: 2-stage coarse grid and GP-UCB Bayesian optimization across 8-D strategy parameter space (opening range, RVOL, stops, trailing drops, VWAP band multiplier, HTF trend, HMM regime filter) optimizing composite risk-adjusted expectancy. Results exposed via `GET /research/hyperparameter-optimization`. Report in `docs/M21_HYPERPARAMETER_OPTIMIZATION_REPORT.md`.
- **M22 Statistical Drift Surveillance & Multi-Session Paper Soak (`app/surveillance/drift.py`, `scripts/run_multi_session_soak.py`)**: Real-time statistical drift surveillance engine implementing exact Two-Sample Kolmogorov-Smirnov ($D, p\text{-value}$) distribution comparison and sequential CUSUM change-point degradation detection ($R_t$ tracking) with automatic fail-safe risk kill switch shutdown on `CRITICAL_DRIFT`. Automated multi-session soak harness verifying zero memory leaks via `tracemalloc`, previous-close rollover, and 100% double-entry ledger reconciliation. Results exposed via `GET /surveillance/drift`. Report in `docs/M22_PAPER_SOAK_AND_DRIFT_REPORT.md`.
- **M23 Dynamic Portfolio Capital Allocation & Margin Stress Engine (`app/risk/allocation.py`, `app/risk/margin_stress.py`)**: Institutional Equal Risk Contribution (ERC) cyclical optimization in pure Decimal solving for equal risk contributions across multi-asset books; evaluates Diversification Ratio ($DR$); models SEBI cash intraday (MIS) peak margins with deterministic pre-trade stress testing under composite instantaneous shocks (-2.5% market gap, +50% volatility margin multiplier, and 20 bps liquidity haircut); rejects prospective signals exceeding 85% stressed margin utilization (`INTRADAY_MARGIN_STRESS_EXCEEDED`). Results exposed via `GET /risk/allocation` and `GET /risk/margin-stress`. Report in `docs/M23_CAPITAL_ALLOCATION_AND_MARGIN_STRESS_REPORT.md`.
- **M24 Multi-Broker Execution Gateway & Shadow Reconciler (`app/execution/gateway.py`, `app/execution/shadow_reconciler.py`)**: Protocol-agnostic `BrokerGateway` interface with `PaperBrokerGateway` (in-process, 0.25 ms latency) and `MockAngelSmartAPIGateway` (REST emulation, 12.5 ms) adapters; `SmartOrderRouter` (SOR) selecting venues by latency; `RealTimeShadowReconciler` detecting ORPHAN/PHANTOM/QUANTITY discrepancies in <0.05 ms with automatic fail-closed `RiskEngine.trigger_kill_switch()` on CRITICAL variance; 4-phase fault injection soak CLI with scorecard. Results exposed via `GET /execution/gateway/status` and `GET /execution/reconciliation/status`. Report in `docs/M24_EXECUTION_GATEWAY_AND_RECONCILER_REPORT.md`.
- **Verified Indian Statutory Cost Model (`app/execution/costs.py`)**: Models percentage-capped brokerage (`min(₹20, 0.03%)` per [Zerodha Charges](https://zerodha.com/charges/)), `0.025%` sell-side STT, `0.003%` buy-side stamp duty, NSE transaction charges, SEBI/IPFT turnover fees, and `18%` GST.
