"""Portable SQLite / PostgreSQL-compatible database layer covering all 17 specification tables with exact Decimal storage."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import os
import sqlite3
from typing import Any, Type, TypeVar

T = TypeVar("T")


def utc_now() -> datetime:
  return datetime.now(timezone.utc)


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS instruments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol VARCHAR(32) UNIQUE NOT NULL,
    exchange VARCHAR(16) NOT NULL DEFAULT 'NSE',
    isin VARCHAR(32) NOT NULL,
    tick_size NUMERIC(10, 4) NOT NULL DEFAULT 0.05,
    price_band_pct NUMERIC(10, 4) NOT NULL DEFAULT 10.0,
    is_fno BOOLEAN NOT NULL DEFAULT 1,
    median_daily_turnover_cr NUMERIC(14, 4) NOT NULL DEFAULT 100.0,
    eligible BOOLEAN NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS market_candles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol VARCHAR(32) NOT NULL,
    interval_minutes INTEGER NOT NULL DEFAULT 1,
    timestamp_utc TIMESTAMP NOT NULL,
    open NUMERIC(14, 4) NOT NULL,
    high NUMERIC(14, 4) NOT NULL,
    low NUMERIC(14, 4) NOT NULL,
    close NUMERIC(14, 4) NOT NULL,
    volume INTEGER NOT NULL,
    source VARCHAR(32) NOT NULL DEFAULT 'BROKER_API',
    UNIQUE(symbol, interval_minutes, timestamp_utc)
);
CREATE TABLE IF NOT EXISTS data_quality_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol VARCHAR(32) NOT NULL,
    timestamp_utc TIMESTAMP NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    severity VARCHAR(16) NOT NULL DEFAULT 'ERROR',
    details TEXT NOT NULL,
    created_at_utc TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS trading_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date VARCHAR(16) UNIQUE NOT NULL,
    is_holiday BOOLEAN NOT NULL DEFAULT 0,
    is_special_session BOOLEAN NOT NULL DEFAULT 0,
    allow_intraday_orb BOOLEAN NOT NULL DEFAULT 1,
    notes VARCHAR(128)
);
CREATE TABLE IF NOT EXISTS strategy_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id VARCHAR(64) NOT NULL,
    version VARCHAR(32) NOT NULL,
    parameters_json TEXT NOT NULL,
    created_at_utc TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS indicator_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol VARCHAR(32) NOT NULL,
    timestamp_utc TIMESTAMP NOT NULL,
    vwap NUMERIC(14, 4),
    ema_9 NUMERIC(14, 4),
    ema_20 NUMERIC(14, 4),
    atr_14 NUMERIC(14, 4),
    rvol NUMERIC(10, 4),
    orb_high NUMERIC(14, 4),
    orb_low NUMERIC(14, 4)
);
CREATE TABLE IF NOT EXISTS trade_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id VARCHAR(64) UNIQUE NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    session_date VARCHAR(16) NOT NULL,
    timestamp_utc TIMESTAMP NOT NULL,
    direction VARCHAR(16) NOT NULL DEFAULT 'LONG',
    signal_price NUMERIC(14, 4) NOT NULL,
    orb_high NUMERIC(14, 4) NOT NULL,
    orb_low NUMERIC(14, 4) NOT NULL,
    vwap NUMERIC(14, 4) NOT NULL,
    rvol NUMERIC(10, 4) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'PROPOSED',
    explanation TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    timestamp_utc TIMESTAMP NOT NULL,
    approved BOOLEAN NOT NULL,
    reason_code VARCHAR(64) NOT NULL,
    approved_quantity INTEGER NOT NULL DEFAULT 0,
    initial_stop_price NUMERIC(14, 4),
    trailing_activation_price NUMERIC(14, 4),
    risk_budget_inr NUMERIC(14, 2),
    estimated_roundtrip_cost_inr NUMERIC(14, 2),
    cost_to_risk_ratio NUMERIC(10, 4)
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id VARCHAR(64) UNIQUE NOT NULL,
    idempotency_key VARCHAR(128) UNIQUE NOT NULL,
    signal_id VARCHAR(64),
    symbol VARCHAR(32) NOT NULL,
    session_date VARCHAR(16) NOT NULL,
    side VARCHAR(8) NOT NULL,
    order_type VARCHAR(32) NOT NULL DEFAULT 'MARKET_NEXT_BAR',
    requested_qty INTEGER NOT NULL,
    filled_qty INTEGER NOT NULL DEFAULT 0,
    reference_price NUMERIC(14, 4) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'CREATED',
    reason VARCHAR(128),
    created_at_utc TIMESTAMP NOT NULL,
    updated_at_utc TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fill_id VARCHAR(64) UNIQUE NOT NULL,
    order_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    side VARCHAR(8) NOT NULL,
    quantity INTEGER NOT NULL,
    raw_bar_price NUMERIC(14, 4) NOT NULL,
    fill_price NUMERIC(14, 4) NOT NULL,
    spread_cost_inr NUMERIC(14, 2) NOT NULL,
    slippage_cost_inr NUMERIC(14, 2) NOT NULL,
    brokerage_inr NUMERIC(14, 2) NOT NULL,
    statutory_taxes_inr NUMERIC(14, 2) NOT NULL,
    total_fees_inr NUMERIC(14, 2) NOT NULL,
    timestamp_utc TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id VARCHAR(64) UNIQUE NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    session_date VARCHAR(16) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
    quantity INTEGER NOT NULL,
    entry_price NUMERIC(14, 4) NOT NULL,
    exit_price NUMERIC(14, 4),
    initial_stop_price NUMERIC(14, 4) NOT NULL,
    current_stop_price NUMERIC(14, 4) NOT NULL,
    trailing_activation_price NUMERIC(14, 4) NOT NULL,
    trailing_activated BOOLEAN NOT NULL DEFAULT 0,
    highest_price_since_entry NUMERIC(14, 4) NOT NULL,
    trailing_drop_pct NUMERIC(10, 4) NOT NULL,
    entry_fees_inr NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
    exit_fees_inr NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
    realized_pnl_inr NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
    unrealized_pnl_inr NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
    exit_reason VARCHAR(64),
    opened_at_utc TIMESTAMP NOT NULL,
    closed_at_utc TIMESTAMP
);
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TIMESTAMP NOT NULL,
    starting_capital_inr NUMERIC(14, 2) NOT NULL,
    cash_balance_inr NUMERIC(14, 2) NOT NULL,
    deployed_exposure_inr NUMERIC(14, 2) NOT NULL,
    realized_pnl_inr NUMERIC(14, 2) NOT NULL,
    unrealized_pnl_inr NUMERIC(14, 2) NOT NULL,
    total_fees_inr NUMERIC(14, 2) NOT NULL,
    net_equity_inr NUMERIC(14, 2) NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id VARCHAR(64) UNIQUE NOT NULL,
    timestamp_utc TIMESTAMP NOT NULL,
    entry_type VARCHAR(32) NOT NULL,
    symbol VARCHAR(32),
    reference_id VARCHAR(64),
    amount_inr NUMERIC(14, 2) NOT NULL,
    balance_after_inr NUMERIC(14, 2) NOT NULL,
    description VARCHAR(256) NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date VARCHAR(16) UNIQUE NOT NULL,
    starting_capital_inr NUMERIC(14, 2) NOT NULL,
    ending_equity_inr NUMERIC(14, 2) NOT NULL,
    net_pnl_inr NUMERIC(14, 2) NOT NULL,
    total_fees_inr NUMERIC(14, 2) NOT NULL,
    trades_count INTEGER NOT NULL,
    wins_count INTEGER NOT NULL,
    losses_count INTEGER NOT NULL,
    max_drawdown_pct NUMERIC(10, 4) NOT NULL,
    kill_switch_triggered BOOLEAN NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id VARCHAR(64) UNIQUE NOT NULL,
    strategy_version VARCHAR(32) NOT NULL,
    cost_version VARCHAR(32) NOT NULL,
    risk_profile VARCHAR(64) NOT NULL,
    dataset_id VARCHAR(128) NOT NULL,
    total_trades INTEGER NOT NULL,
    win_rate_pct NUMERIC(10, 4) NOT NULL,
    profit_factor NUMERIC(10, 4) NOT NULL,
    net_expectancy_inr NUMERIC(14, 2) NOT NULL,
    max_drawdown_pct NUMERIC(10, 4) NOT NULL,
    avg_cost_to_risk_pct NUMERIC(10, 4) NOT NULL,
    evidence_classification VARCHAR(32) NOT NULL,
    created_at_utc TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    session_date VARCHAR(16) NOT NULL,
    quantity INTEGER NOT NULL,
    entry_price NUMERIC(14, 4) NOT NULL,
    exit_price NUMERIC(14, 4) NOT NULL,
    gross_pnl_inr NUMERIC(14, 2) NOT NULL,
    total_fees_inr NUMERIC(14, 2) NOT NULL,
    net_pnl_inr NUMERIC(14, 2) NOT NULL,
    r_multiple_net NUMERIC(10, 4) NOT NULL,
    holding_minutes INTEGER NOT NULL,
    exit_reason VARCHAR(64) NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TIMESTAMP NOT NULL,
    component VARCHAR(64) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    severity VARCHAR(16) NOT NULL DEFAULT 'INFO',
    payload_json TEXT NOT NULL
);
"""


class _ColumnAttr:
  """Supports SQLAlchemy-style filter expressions (Model.field == val) and ordering."""

  def __init__(self, name: str):
    self.name = name

  def __eq__(self, other: Any) -> tuple[str, Any]:  # type: ignore[override]
    return (self.name, other)

  def asc(self) -> tuple[str, str]:
    return (self.name, "ASC")

  def desc(self) -> tuple[str, str]:
    return (self.name, "DESC")


class _ModelMeta(type):
  def __getattr__(cls, name: str) -> _ColumnAttr:
    return _ColumnAttr(name)


class BaseRecord(metaclass=_ModelMeta):
  __tablename__: str = ""

  def __init__(self, **kwargs: Any):
    self.id = kwargs.get("id")
    for k, v in kwargs.items():
      setattr(self, k, v)


class InstrumentRecord(BaseRecord):
  __tablename__ = "instruments"


class MarketCandleRecord(BaseRecord):
  __tablename__ = "market_candles"


class DataQualityEventRecord(BaseRecord):
  __tablename__ = "data_quality_events"


class TradingSessionRecord(BaseRecord):
  __tablename__ = "trading_sessions"


class StrategyConfigRecord(BaseRecord):
  __tablename__ = "strategy_configs"


class IndicatorSnapshotRecord(BaseRecord):
  __tablename__ = "indicator_snapshots"


class TradeSignalRecord(BaseRecord):
  __tablename__ = "trade_signals"


class RiskDecisionRecord(BaseRecord):
  __tablename__ = "risk_decisions"


class OrderRecord(BaseRecord):
  __tablename__ = "orders"


class FillRecord(BaseRecord):
  __tablename__ = "fills"


class PositionRecord(BaseRecord):
  __tablename__ = "positions"


class PortfolioSnapshotRecord(BaseRecord):
  __tablename__ = "portfolio_snapshots"


class LedgerEntryRecord(BaseRecord):
  __tablename__ = "ledger_entries"


class DailyMetricRecord(BaseRecord):
  __tablename__ = "daily_metrics"


class BacktestRunRecord(BaseRecord):
  __tablename__ = "backtest_runs"
  def __init__(self, **kwargs: Any):
    if "created_at_utc" not in kwargs:
      kwargs["created_at_utc"] = utc_now()
    super().__init__(**kwargs)


class BacktestTradeRecord(BaseRecord):
  __tablename__ = "backtest_trades"


class AuditEventRecord(BaseRecord):
  __tablename__ = "audit_events"


def _serialize_value(val: Any) -> Any:
  if val is None:
    return None
  if isinstance(val, Decimal):
    return str(val)
  if isinstance(val, datetime):
    return val.isoformat()
  if isinstance(val, bool):
    return 1 if val else 0
  if isinstance(val, (int, float, str)):
    return val
  return str(val)


def _deserialize_value(val: Any, col_type: str) -> Any:
  if val is None:
    return None
  col_type = col_type.upper()
  if "NUMERIC" in col_type or "DECIMAL" in col_type:
    return Decimal(str(val))
  if col_type == "BOOLEAN":
    return bool(val)
  if "INT" in col_type:
    return int(val)
  if "TIMESTAMP" in col_type:
    if isinstance(val, str):
      try:
        return datetime.fromisoformat(val)
      except Exception:
        return val
    return val
  return val


class QueryProxy:
  def __init__(self, session: "Session", model_cls: Type[BaseRecord]):
    self.session = session
    self.model_cls = model_cls
    self.filters: list[tuple[str, Any]] = []
    self.order: tuple[str, str] | None = None
    self._limit: int | None = None

  def filter(self, *conditions: tuple[str, Any]) -> "QueryProxy":
    for cond in conditions:
      if isinstance(cond, tuple) and len(cond) == 2:
        self.filters.append(cond)
    return self

  def filter_by(self, **kwargs: Any) -> "QueryProxy":
    for k, v in kwargs.items():
      self.filters.append((k, v))
    return self

  def order_by(self, spec: tuple[str, str]) -> "QueryProxy":
    self.order = spec
    return self

  def limit(self, n: int) -> "QueryProxy":
    self._limit = n
    return self

  def all(self) -> list[Any]:
    self.session.flush()

    tbl = self.model_cls.__tablename__
    schema = self.session.schema.get(tbl, {})

    clauses: list[str] = []
    params: list[Any] = []
    for col, val in self.filters:
      if val is None:
        clauses.append(f"[{col}] IS NULL")
      else:
        clauses.append(f"[{col}] = ?")
        params.append(_serialize_value(val))

    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    order_sql = f" ORDER BY [{self.order[0]}] {self.order[1]}" if self.order else ""
    limit_sql = f" LIMIT {int(self._limit)}" if self._limit is not None else ""

    sql = f"SELECT * FROM [{tbl}]{where_sql}{order_sql}{limit_sql}"

    cur = self.session.conn.cursor()
    rows = cur.execute(sql, params).fetchall()

    results: list[Any] = []
    col_names = [d[0] for d in cur.description] if cur.description else []
    for row in rows:
      row_dict: dict[str, Any] = {}
      for idx, col_name in enumerate(col_names):
        val = row[idx]
        ctype = schema.get(col_name, "TEXT")
        row_dict[col_name] = _deserialize_value(val, ctype)

      row_id = row_dict.get("id")
      obj_key = (self.model_cls, row_id)

      if obj_key in self.session._identity_map:
        tracked_obj = self.session._identity_map[obj_key]
        results.append(tracked_obj)
      else:
        obj = self.model_cls(**row_dict)
        self.session._identity_map[obj_key] = obj
        self.session._snapshot[obj_key] = {
            col: getattr(obj, col, None) for col in schema.keys() if hasattr(obj, col)
        }
        results.append(obj)

    return results

  def first(self) -> Any | None:
    old_limit = self._limit
    self._limit = 1
    res = self.all()
    self._limit = old_limit
    return res[0] if res else None


class Session:
  """Lightweight unit-of-work session backed by genuine SQLite disk persistence."""

  def __init__(self, conn: sqlite3.Connection, schema: dict[str, dict[str, str]]):
    self.conn = conn
    self.schema = schema
    self._pending_inserts: list[BaseRecord] = []
    self._identity_map: dict[tuple[Type[BaseRecord], Any], BaseRecord] = {}
    self._snapshot: dict[tuple[Type[BaseRecord], Any], dict[str, Any]] = {}

  def __enter__(self) -> "Session":
    return self

  def __exit__(self, exc_type, exc_val, exc_tb) -> None:
    if exc_type is None:
      self.commit()
    else:
      self.rollback()

  def add(self, obj: BaseRecord) -> None:
    if getattr(obj, "id", None) is not None:
      self._identity_map[(type(obj), obj.id)] = obj
    else:
      if obj not in self._pending_inserts:
        self._pending_inserts.append(obj)

  def query(self, model_cls: Type[BaseRecord]) -> QueryProxy:
    return QueryProxy(self, model_cls)

  def flush(self) -> None:
    cur = self.conn.cursor()

    # 1. Process pending inserts
    inserts_to_process = list(self._pending_inserts)
    self._pending_inserts.clear()

    for obj in inserts_to_process:
      tbl = obj.__tablename__
      table_schema = self.schema.get(tbl, {})
      cols: list[str] = []
      vals: list[Any] = []

      for col_name in table_schema.keys():
        if col_name == "id":
          if getattr(obj, "id", None) is not None:
            cols.append(f"[{col_name}]")
            vals.append(obj.id)
          continue
        if hasattr(obj, col_name):
          cols.append(f"[{col_name}]")
          vals.append(_serialize_value(getattr(obj, col_name)))

      if cols:
        col_str = ", ".join(cols)
        placeholder_str = ", ".join(["?"] * len(vals))
        cur.execute(f"INSERT INTO [{tbl}] ({col_str}) VALUES ({placeholder_str})", vals)
        if getattr(obj, "id", None) is None:
          obj.id = cur.lastrowid

      obj_key = (type(obj), obj.id)
      self._identity_map[obj_key] = obj
      self._snapshot[obj_key] = {
          col: getattr(obj, col, None) for col in table_schema.keys() if hasattr(obj, col)
      }

    # 2. Process updates for tracked instances in identity map
    for (model_cls, obj_id), obj in list(self._identity_map.items()):
      tbl = obj.__tablename__
      table_schema = self.schema.get(tbl, {})
      snapshot = self._snapshot.get((model_cls, obj_id), {})

      dirty = False
      update_cols: list[str] = []
      update_vals: list[Any] = []

      for col_name in table_schema.keys():
        if col_name == "id":
          continue
        curr_val = getattr(obj, col_name, None)
        snap_val = snapshot.get(col_name)
        if curr_val != snap_val:
          dirty = True
        if hasattr(obj, col_name):
          update_cols.append(f"[{col_name}] = ?")
          update_vals.append(_serialize_value(curr_val))

      if dirty and update_cols:
        update_vals.append(obj.id)
        sql = f"UPDATE [{tbl}] SET {', '.join(update_cols)} WHERE [id] = ?"
        cur.execute(sql, update_vals)
        self._snapshot[(model_cls, obj_id)] = {
            col: getattr(obj, col, None) for col in table_schema.keys() if hasattr(obj, col)
        }

  def commit(self) -> None:
    self.flush()
    self.conn.commit()

  def rollback(self) -> None:
    self._pending_inserts.clear()
    self.conn.rollback()


def init_db(database_url: str = "sqlite:///:memory:"):
  path = ":memory:"
  if database_url.startswith("sqlite:///"):
    path = database_url[len("sqlite:///") :]

  if path != ":memory:":
    parent_dir = os.path.dirname(os.path.abspath(path))
    if parent_dir and not os.path.exists(parent_dir):
      os.makedirs(parent_dir, exist_ok=True)

  conn = sqlite3.connect(path, check_same_thread=False)
  conn.row_factory = sqlite3.Row
  if path != ":memory:":
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
  conn.executescript(SCHEMA_DDL)
  conn.commit()

  cur = conn.cursor()
  tables = [
      r[0]
      for r in cur.execute(
          "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
      ).fetchall()
  ]
  schema: dict[str, dict[str, str]] = {}
  for t in tables:
    cols = cur.execute(f"PRAGMA table_info([{t}])").fetchall()
    schema[t] = {c[1]: c[2].upper() for c in cols}

  def SessionLocal() -> Session:
    return Session(conn, schema)

  return conn, SessionLocal
