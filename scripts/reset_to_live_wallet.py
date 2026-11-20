"""Reset DB to match real Angel One live wallet (Rs. 1000)."""
import sqlite3
from datetime import datetime, timezone
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["ALPHATRADE_EXECUTION_MODE"] = "LIVE"
os.environ["ALPHATRADE_LIVE_CONFIRMATION"] = "I_ACCEPT_FINANCIAL_RISK"

from app.core.config import _load_env_file
_load_env_file()
from app.execution.gateway import LiveAngelSmartAPIGateway

# Step 1: Fetch real live cash from Angel One
print("[1] Querying Angel One RMS for real wallet balance...")
gw = LiveAngelSmartAPIGateway()
margins = gw.get_margins()
live_cash = float(margins.available_cash)
print(f"    Angel One Available Cash: Rs. {live_cash:,.2f}")

# Step 2: Wipe stale ledger / orders / positions from paper sessions
print("[2] Resetting DB to live wallet state...")
conn = sqlite3.connect("alphatrade_v2.db")
conn.execute("DELETE FROM ledger_entries")
conn.execute("DELETE FROM fills")
conn.execute("DELETE FROM orders")
conn.execute("DELETE FROM positions")
conn.execute("DELETE FROM portfolio_snapshots")
conn.commit()

# Step 3: Insert single authoritative LIVE_WALLET_SYNC entry
now = datetime.now(timezone.utc).isoformat()
sql = (
    "INSERT INTO ledger_entries "
    "(entry_id, timestamp_utc, entry_type, symbol, reference_id, amount_inr, balance_after_inr, description) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
conn.execute(sql, (
    "LED_LIVE_ANGEL_ONE_RMS",
    now,
    "LIVE_WALLET_SYNC",
    None,
    "RMS_LIVE",
    live_cash,
    live_cash,
    f"Angel One Live Wallet RMS sync - Rs. {live_cash:.2f} available cash (Client: S54949978)",
))
conn.commit()

result = conn.execute("SELECT * FROM ledger_entries ORDER BY id DESC LIMIT 1").fetchone()
print(f"[3] Ledger reset complete:")
print(f"    Entry: {result}")
conn.close()
print(f"\n[DONE] DB now reflects real Angel One wallet: Rs. {live_cash:,.2f}")
print("       Restart server to apply changes.")
