"""Clean out all residual static test trades, orders, positions, signals, and ledger entries from alphatrade_v2.db, preserving authentic market_candles."""

import shutil
import sqlite3
import os
from datetime import datetime, timezone

db_path = "alphatrade_v2.db"
backup_path = f"alphatrade_v2_backup_before_reset_{int(datetime.now(timezone.utc).timestamp())}.db"

print(f"[*] Creating pre-reset backup: {backup_path}")
shutil.copy2(db_path, backup_path)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

tables_to_clear = [
    "trade_signals",
    "risk_decisions",
    "orders",
    "fills",
    "positions",
    "portfolio_snapshots",
    "ledger_entries",
    "daily_metrics",
    "backtest_runs",
    "backtest_trades",
    "audit_events",
    "data_quality_events",
    "indicator_snapshots",
]

print("\n[*] Clearing operational tables:")
before_counts = {}
after_counts = {}

for tbl in tables_to_clear:
    try:
        cnt_before = cursor.execute(f"SELECT COUNT(*) FROM [{tbl}]").fetchone()[0]
        before_counts[tbl] = cnt_before
        cursor.execute(f"DELETE FROM [{tbl}]")
        cnt_after = cursor.execute(f"SELECT COUNT(*) FROM [{tbl}]").fetchone()[0]
        after_counts[tbl] = cnt_after
        print(f"  - {tbl:20s}: {cnt_before:5d} -> {cnt_after:5d} (cleaned {cnt_before} rows)")
    except Exception as e:
        print(f"  - {tbl:20s}: error {e}")

# Check market_candles preserved
candles_count = cursor.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0]
print(f"\n[OK] Preserved authentic market_candles: {candles_count:,} rows")

conn.commit()
cursor.execute("VACUUM")
conn.close()

print("\n[SUCCESS] All static operational data removed. Database reset to clean initial state.")
