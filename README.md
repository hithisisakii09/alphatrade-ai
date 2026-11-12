# AlphaTrade AI v2.0

**NSE India • Intraday Cash Equities • Long-Only 15m Opening Range Breakout (ORB) • Deterministic Paper-Trading & Research Engine**

---

## Quick Start (Local)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the unified zero-trust test suite (54/54 tests passing)
python run_tests.py
# or: pytest -v

# 3. Verify Angel One live market data feeder & daily JWT caching
python scripts/verify_angel_data.py

# 4. Ingest multi-month authentic historical candles (Angel One SmartAPI)
python scripts/download_historical_candles.py --start-date 2026-09-01 --end-date 2026-11-11

# 5. Run the empirical walk-forward quantitative study (70% IS / 30% OOS)
python scripts/run_walk_forward_study.py --source HISTORICAL

# 6. Launch the live FastAPI server & dynamic 5-second polling dashboard
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
- **Verified Indian Statutory Cost Model (`app/execution/costs.py`)**: Models percentage-capped brokerage (`min(₹20, 0.03%)` per [Zerodha Charges](https://zerodha.com/charges/)), `0.025%` sell-side STT, `0.003%` buy-side stamp duty, NSE transaction charges, SEBI/IPFT turnover fees, and `18%` GST.
