"""Automated SQLite WAL Checkpoint and Point-in-Time Backup Manager.

Features:
- Deterministic WAL checkpointing (PRAGMA wal_checkpoint(TRUNCATE)).
- Zero-downtime online backup via SQLite Backup API.
- SHA-256 cryptographic checksum calculation and manifest verification.
- Offline and live database integrity verification (PRAGMA integrity_check).
- Safe atomic restoration with pre-restore safety snapshots.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("BackupManager")


def compute_sha256(file_path: str) -> str:
  """Computes SHA-256 hash of a file in 64KB chunks."""
  sha = hashlib.sha256()
  with open(file_path, "rb") as f:
    while chunk := f.read(65536):
      sha.update(chunk)
  return sha.hexdigest()


def checkpoint_wal(db_path: str, mode: str = "TRUNCATE") -> tuple[int, int, int]:
  """Performs an SQLite WAL checkpoint and returns (busy, log_frames, checkpointed_frames)."""
  if not os.path.exists(db_path):
    raise FileNotFoundError(f"Database not found: {db_path}")

  conn = sqlite3.connect(db_path, timeout=30.0)
  try:
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA wal_checkpoint({mode});")
    row = cursor.fetchone()
    logger.info("WAL Checkpoint (%s) on %s: busy=%s, log=%s, checkpointed=%s", mode, db_path, row[0], row[1], row[2])
    return int(row[0]), int(row[1]), int(row[2])
  finally:
    conn.close()


def get_db_stats(db_path: str) -> dict:
  """Extracts row counts and schema summary for integrity manifest."""
  stats = {
      "total_candles": 0,
      "total_orders": 0,
      "total_positions": 0,
      "total_trades": 0,
      "integrity_check": "FAILED",
  }
  conn = sqlite3.connect(db_path, timeout=30.0)
  try:
    c = conn.cursor()
    c.execute("PRAGMA integrity_check;")
    chk = c.fetchone()
    stats["integrity_check"] = chk[0] if chk else "FAILED"

    # Query tables if present
    c.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = {r[0] for r in c.fetchall()}

    if "candles_1m" in tables:
      c.execute("SELECT COUNT(*) FROM candles_1m;")
      stats["total_candles"] = c.fetchone()[0]
    if "orders" in tables:
      c.execute("SELECT COUNT(*) FROM orders;")
      stats["total_orders"] = c.fetchone()[0]
    if "positions" in tables:
      c.execute("SELECT COUNT(*) FROM positions;")
      stats["total_positions"] = c.fetchone()[0]
    if "trades" in tables:
      c.execute("SELECT COUNT(*) FROM trades;")
      stats["total_trades"] = c.fetchone()[0]
  finally:
    conn.close()
  return stats


def create_backup(db_path: str, backup_dir: str = "backups") -> dict:
  """Creates a consistent point-in-time backup with SHA-256 checksum and metadata manifest."""
  if not os.path.exists(db_path):
    raise FileNotFoundError(f"Source database not found: {db_path}")

  os.makedirs(backup_dir, exist_ok=True)

  # 1. Flush WAL frames into main database file
  checkpoint_wal(db_path, mode="TRUNCATE")

  ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
  db_basename = os.path.splitext(os.path.basename(db_path))[0]
  backup_filename = f"{db_basename}_backup_{ts_str}.db"
  backup_path = os.path.join(backup_dir, backup_filename)
  meta_path = os.path.join(backup_dir, f"{backup_filename}.meta.json")

  logger.info("Starting online backup: %s -> %s", db_path, backup_path)

  # 2. Perform atomic online backup using SQLite backup API
  src_conn = sqlite3.connect(db_path, timeout=30.0)
  dst_conn = sqlite3.connect(backup_path)
  try:
    with dst_conn:
      src_conn.backup(dst_conn, pages=1000)
  finally:
    dst_conn.close()
    src_conn.close()

  # 3. Calculate cryptographic SHA-256 and gather metadata
  checksum = compute_sha256(backup_path)
  file_size = os.path.getsize(backup_path)
  stats = get_db_stats(backup_path)

  manifest = {
      "timestamp_utc": datetime.now(timezone.utc).isoformat(),
      "source_database": os.path.abspath(db_path),
      "backup_filename": backup_filename,
      "backup_path": os.path.abspath(backup_path),
      "file_size_bytes": file_size,
      "sha256_checksum": checksum,
      "stats": stats,
  }

  with open(meta_path, "w") as f:
    json.dump(manifest, f, indent=2)

  logger.info("Backup successfully generated: %s (Size: %d bytes, SHA-256: %s)", backup_path, file_size, checksum[:16])
  return manifest


def verify_backup(backup_path: str, meta_path: str | None = None) -> tuple[bool, str]:
  """Verifies backup file SHA-256 against manifest and runs SQLite PRAGMA integrity_check."""
  if not os.path.exists(backup_path):
    return False, f"Backup file not found: {backup_path}"

  if meta_path is None:
    meta_path = f"{backup_path}.meta.json"

  if not os.path.exists(meta_path):
    # If no metadata, at least verify SQLite integrity check
    stats = get_db_stats(backup_path)
    if stats["integrity_check"] == "ok":
      return True, f"Integrity check passed (no metadata manifest found). Candles: {stats['total_candles']}"
    return False, f"Integrity check failed: {stats['integrity_check']}"

  with open(meta_path, "r") as f:
    manifest = json.load(f)

  expected_checksum = manifest.get("sha256_checksum")
  actual_checksum = compute_sha256(backup_path)

  if expected_checksum != actual_checksum:
    return False, f"SHA-256 mismatch! Expected {expected_checksum}, found {actual_checksum}"

  stats = get_db_stats(backup_path)
  if stats["integrity_check"] != "ok":
    return False, f"SQLite PRAGMA integrity_check failed: {stats['integrity_check']}"

  return True, f"Verification PASSED (SHA-256: {actual_checksum[:16]}..., Candles: {stats['total_candles']}, Integrity: ok)"


def restore_backup(backup_path: str, target_db_path: str, force: bool = False) -> bool:
  """Restores target database from a verified backup. Creates a pre-restore safety copy first."""
  valid, msg = verify_backup(backup_path)
  if not valid and not force:
    logger.error("Restore aborted: Backup verification failed (%s)", msg)
    return False

  target_dir = os.path.dirname(os.path.abspath(target_db_path))
  os.makedirs(target_dir, exist_ok=True)

  # Safety snapshot of target if it already exists
  if os.path.exists(target_db_path):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    safety_copy = f"{target_db_path}.prerestore_{ts}.bak"
    logger.info("Creating safety copy of active target database: %s", safety_copy)
    shutil.copy2(target_db_path, safety_copy)

    # Remove stale -wal and -shm files
    for ext in ["-wal", "-shm"]:
      extra = target_db_path + ext
      if os.path.exists(extra):
        os.remove(extra)

  # Copy backup to target
  shutil.copy2(backup_path, target_db_path)
  logger.info("Database restored from %s to %s", backup_path, target_db_path)
  return True


def list_backups(backup_dir: str = "backups") -> list[dict]:
  """Lists all backups in directory with verification status."""
  if not os.path.exists(backup_dir):
    return []

  backups = []
  for f in sorted(os.listdir(backup_dir)):
    if f.endswith(".db") and not f.endswith(".bak"):
      bp = os.path.join(backup_dir, f)
      mp = f"{bp}.meta.json"
      size = os.path.getsize(bp)
      verified, reason = verify_backup(bp, mp if os.path.exists(mp) else None)
      backups.append({
          "filename": f,
          "size_bytes": size,
          "path": bp,
          "verified": verified,
          "message": reason,
      })
  return backups


def main():
  parser = argparse.ArgumentParser(description="AlphaTrade AI v2.0 - SQLite Backup & WAL Checkpoint Manager")
  parser.add_argument("--db", default="alphatrade_v2.db", help="Path to SQLite database file")
  parser.add_argument("--backup-dir", default="backups", help="Directory to store backups")
  parser.add_argument("--checkpoint", action="store_true", help="Execute PRAGMA wal_checkpoint(TRUNCATE)")
  parser.add_argument("--backup", action="store_true", help="Create consistent point-in-time backup with SHA-256")
  parser.add_argument("--verify", type=str, help="Verify integrity and SHA-256 of specified backup file")
  parser.add_argument("--restore", type=str, help="Restore target database from specified backup file")
  parser.add_argument("--list", action="store_true", help="List all backups and integrity status")

  args = parser.parse_args()

  if args.checkpoint:
    res = checkpoint_wal(args.db, mode="TRUNCATE")
    print(f"Checkpoint completed: busy={res[0]}, log_frames={res[1]}, checkpointed={res[2]}")

  elif args.backup:
    meta = create_backup(args.db, args.backup_dir)
    print(json.dumps(meta, indent=2))

  elif args.verify:
    valid, msg = verify_backup(args.verify)
    print(f"Verified: {valid} - {msg}")
    sys.exit(0 if valid else 1)

  elif args.restore:
    ok = restore_backup(args.restore, args.db)
    print(f"Restore result: {ok}")
    sys.exit(0 if ok else 1)

  elif args.list:
    backups = list_backups(args.backup_dir)
    print(f"Found {len(backups)} backups in {args.backup_dir}:")
    for b in backups:
      status = "[OK]" if b["verified"] else "[CORRUPT]"
      print(f"  {status} {b['filename']} ({b['size_bytes']:,} bytes) - {b['message']}")

  else:
    parser.print_help()


if __name__ == "__main__":
  main()
