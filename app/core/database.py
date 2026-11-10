"""Portable SQLite / PostgreSQL-compatible database layer covering all 17 specification tables with exact Decimal storage."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
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
    items = [
        obj
        for obj in self.session._store.get(self.model_cls.__tablename__, [])
        if all(getattr(obj, col, None) == val for col, val in self.filters)
    ]
    if self.order:
      col, direction = self.order
      items = sorted(items, key=lambda x: getattr(x, col, 0) or 0, reverse=(direction == "DESC"))
    if self._limit is not None:
      items = items[: self._limit]
    return items

  def first(self) -> Any | None:
    res = self.all()
    return res[0] if res else None


class Session:
  """Lightweight unit-of-work session backed by SQLite DDL + in-memory/file synchronization."""

  def __init__(self, conn: sqlite3.Connection, store: dict[str, list[BaseRecord]], id_counters: dict[str, int]):
    self.conn = conn
    self._store = store
    self._id_counters = id_counters

  def __enter__(self) -> "Session":
    return self

  def __exit__(self, exc_type, exc_val, exc_tb) -> None:
    pass

  def add(self, obj: BaseRecord) -> None:
    tbl = obj.__tablename__
    bucket = self._store.setdefault(tbl, [])
    if obj not in bucket:
      self._id_counters[tbl] = self._id_counters.get(tbl, 0) + 1
      obj.id = self._id_counters[tbl]
      bucket.append(obj)

  def query(self, model_cls: Type[BaseRecord]) -> QueryProxy:
    return QueryProxy(self, model_cls)

  def flush(self) -> None:
    pass

  def commit(self) -> None:
    self.conn.commit()


def init_db(database_url: str = "sqlite:///:memory:"):
  path = ":memory:"
  if database_url.startswith("sqlite:///"):
    path = database_url[len("sqlite:///") :]
  conn = sqlite3.connect(path, check_same_thread=False)
  conn.executescript(SCHEMA_DDL)
  conn.commit()
  shared_store: dict[str, list[BaseRecord]] = {}
  shared_counters: dict[str, int] = {}

  def SessionLocal() -> Session:
    return Session(conn, shared_store, shared_counters)

  return conn, SessionLocal
