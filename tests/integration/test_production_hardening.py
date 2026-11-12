"""Integration tests for Milestone M12: Production Hardening, SQLite WAL Checkpoint, Feed Watchdog & Dead-Man Switch."""

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
import json
import os
import sqlite3
import tempfile
import pytest

from app.core.clock import IST, NSECalendar
from app.services.daily_runner import ScheduledDailyRunner
from app.services.watchdog import FeedWatchdog
from scripts.backup_manager import (
    checkpoint_wal,
    compute_sha256,
    create_backup,
    restore_backup,
    verify_backup,
)


def test_wal_checkpoint_and_backup_creation_and_restore():
  """Verifies WAL checkpoint TRUNCATE, online backup creation with SHA-256, and safe restoration."""
  with tempfile.TemporaryDirectory() as tmp_dir:
    db_path = os.path.join(tmp_dir, "test_trading.db")
    backup_dir = os.path.join(tmp_dir, "backups")

    # 1. Initialize SQLite database with WAL journal mode
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(
        "CREATE TABLE orders (order_id TEXT PRIMARY KEY, symbol TEXT, quantity INTEGER, price TEXT);"
    )
    for i in range(10):
      conn.execute(
          "INSERT INTO orders VALUES (?, ?, ?, ?)",
          (f"ORD_{i}", "RELIANCE", 10 + i, f"{2500 + i}.00"),
      )
    conn.commit()
    conn.close()

    # Verify WAL file exists
    wal_path = db_path + "-wal"
    assert os.path.exists(db_path)

    # 2. Execute WAL Checkpoint TRUNCATE
    busy, log_f, chk_f = checkpoint_wal(db_path, mode="TRUNCATE")
    assert busy == 0

    # 3. Create point-in-time backup with SHA-256 manifest
    manifest = create_backup(db_path, backup_dir=backup_dir)
    backup_path = manifest["backup_path"]
    assert os.path.exists(backup_path)
    assert os.path.exists(backup_path + ".meta.json")
    assert manifest["sha256_checksum"] == compute_sha256(backup_path)
    assert manifest["stats"]["total_orders"] == 10
    assert manifest["stats"]["integrity_check"] == "ok"

    # 4. Verify backup passes cryptographic and SQLite integrity check
    valid, msg = verify_backup(backup_path)
    assert valid is True
    assert "PASSED" in msg

    # 5. Tamper test: Corrupt backup file and verify detection
    tampered_backup = os.path.join(backup_dir, "tampered.db")
    with open(backup_path, "rb") as f:
      content = bytearray(f.read())
    content[100] = (content[100] + 1) % 256  # Alter byte
    with open(tampered_backup, "wb") as f:
      f.write(content)
    with open(tampered_backup + ".meta.json", "w") as f:
      json.dump(manifest, f)

    t_valid, t_msg = verify_backup(tampered_backup)
    assert t_valid is False
    assert "mismatch" in t_msg.lower() or "failed" in t_msg.lower()

    # 6. Test Safe Restore to new target
    restored_db = os.path.join(tmp_dir, "restored.db")
    restored_ok = restore_backup(backup_path, restored_db)
    assert restored_ok is True
    assert os.path.exists(restored_db)

    # Verify contents of restored database
    res_conn = sqlite3.connect(restored_db)
    cur = res_conn.cursor()
    cnt = cur.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    res_conn.close()
    assert cnt == 10


def test_watchdog_health_and_dead_man_switch():
  """Verifies feed watchdog monitoring, dead-man switch activation on stall, and callback execution."""
  cal = NSECalendar()
  tripped_reasons = []
  recovered = []

  def mock_on_stall(reason: str):
    tripped_reasons.append(reason)

  def mock_on_recovery():
    recovered.append(True)

  watchdog = FeedWatchdog(
      staleness_threshold_seconds=180,
      notification_service=None,
      on_stall_callback=mock_on_stall,
      on_recovery_callback=mock_on_recovery,
      calendar=cal,
  )

  # Scenario 1: Weekend or Off-Hours (e.g. Sunday 10:00 AM UTC = 15:30 IST)
  sunday_utc = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)
  res_off = watchdog.check_health(sunday_utc)
  assert res_off["status"] == "IDLE_OFF_HOURS"
  assert res_off["is_healthy"] is True
  assert res_off["active_window"] is False

  # Scenario 2: Active trading session (Tuesday 2026-11-10 04:30 UTC = 10:00 IST)
  # But 2026-11-10 is a holiday, let's use Wednesday 2026-11-11 04:30 UTC = 10:00 IST
  wed_utc = datetime(2026, 11, 11, 4, 30, tzinfo=timezone.utc)
  assert cal.is_trading_day(wed_utc) is True

  # Record a tick at 10:00:00 IST
  watchdog.record_tick(wed_utc)
  assert watchdog.last_tick_time_utc == wed_utc

  # 30 seconds later (10:00:30 IST): Healthy
  res_healthy = watchdog.check_health(wed_utc + timedelta(seconds=30))
  assert res_healthy["status"] == "HEALTHY"
  assert res_healthy["is_healthy"] is True
  assert res_healthy["seconds_since_last_tick"] == 30.0

  # 200 seconds later (10:03:20 IST): Stall > 180s threshold!
  res_stalled = watchdog.check_health(wed_utc + timedelta(seconds=200))
  assert res_stalled["status"] == "FEED_STALLED"
  assert res_stalled["is_healthy"] is False
  assert watchdog.is_stalled is True
  assert len(tripped_reasons) == 1
  assert "DEAD_MAN_SWITCH" in tripped_reasons[0]

  # Recovery: New tick arrives at 10:04:00 IST
  watchdog.record_tick(wed_utc + timedelta(seconds=240))
  assert watchdog.is_stalled is False
  assert len(recovered) == 1

  res_recovered = watchdog.check_health(wed_utc + timedelta(seconds=245))
  assert res_recovered["status"] == "HEALTHY"
  assert res_recovered["is_healthy"] is True


def test_daily_runner_production_hardening_jobs():
  """Verifies that ScheduledDailyRunner registers the watchdog and checkpoint jobs."""
  runner = ScheduledDailyRunner(auto_start=False)
  jobs = {j.id for j in runner.scheduler.get_jobs()}

  assert "feed_watchdog_30s" in jobs
  assert "nse_wal_checkpoint_1540" in jobs

  # Directly execute watchdog job
  wd_res = runner.job_watchdog_check()
  assert "status" in wd_res
  assert "is_healthy" in wd_res

  # Directly execute WAL checkpoint job
  chk_res = runner.job_wal_checkpoint()
  assert chk_res["status"] == "CHECKPOINT_SUCCESS"
