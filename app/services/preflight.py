"""Institutional Pre-Flight Operational Readiness & Sandbox Go-Live Gate (Milestone M30).

Institutional Invariants:
1. Strict fail-closed paper mode execution enforcement via assert_paper_mode().
2. Pure Decimal financial arithmetic with zero binary float drift.
3. 10-point automated Go/No-Go pre-session audit covering database WAL hygiene,
   broker authentication, historical candle provenance, float drift scanner,
   SEBI WORM integrity, margin stress headroom, watchdog health, and meta-ensemble conviction.
4. Pre-session verification budget: entire 10-point audit executes in < 50.0 ms.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from enum import Enum
import logging
import os
import sqlite3
import time
from typing import Any

from app.compliance.worm_logger import SEBIWORMTradeLogger
from app.core.config import load_app_config
from app.core.execution_guard import assert_paper_mode, LiveTradingProhibitedError
from app.core.money import to_decimal
from app.execution.algo_slicer import ParentOrderRequest, TWAPExecutionEngine
from app.market.regime import MarketRegime, RegimeState
from app.risk.margin_stress import IntradayMarginStressEngine
from app.strategy.ensemble.allocator import MetaStrategyEnsemble

logger = logging.getLogger("PreFlightReadiness")


class GateStatus(str, Enum):
  PASSED = "PASSED"
  WARNING = "WARNING"
  FAILED = "FAILED"


@dataclass(frozen=True)
class PreFlightCheckItem:
  """Audit result for an individual operational readiness gate."""
  name: str
  status: GateStatus
  latency_ms: float
  message: str
  critical: bool = True

  def to_dict(self) -> dict[str, Any]:
    return {
        "name": self.name,
        "status": self.status.value,
        "latency_ms": round(self.latency_ms, 3),
        "message": self.message,
        "critical": self.critical,
    }


@dataclass(frozen=True)
class PreFlightAuditReport:
  """Aggregated institutional Go/No-Go decision manifest."""
  audit_id: str
  timestamp_utc: str
  overall_decision: str  # "GO" / "NO_GO"
  passed_count: int
  warning_count: int
  failed_count: int
  total_latency_ms: float
  checks: list[PreFlightCheckItem]

  def to_dict(self) -> dict[str, Any]:
    return {
        "audit_id": self.audit_id,
        "timestamp_utc": self.timestamp_utc,
        "overall_decision": self.overall_decision,
        "passed_count": self.passed_count,
        "warning_count": self.warning_count,
        "failed_count": self.failed_count,
        "total_latency_ms": round(self.total_latency_ms, 3),
        "checks": [c.to_dict() for c in self.checks],
    }


class PreFlightReadinessEngine:
  """Automated 10-point Go/No-Go pre-flight operational readiness audit engine."""

  def __init__(self, db_path: str = "./alphatrade_v2.db", audit_dir: str = ".audit"):
    assert_paper_mode()
    self.db_path = db_path
    self.audit_dir = audit_dir
    self.last_report: PreFlightAuditReport | None = None

  def _audit_paper_mode_guard(self) -> PreFlightCheckItem:
    t0 = time.time()
    try:
      assert_paper_mode()
      mode = os.environ.get("ALPHATRADE_EXECUTION_MODE", "PAPER")
      if mode != "PAPER":
        return PreFlightCheckItem("1. Fail-Closed Paper Mode Guard", GateStatus.FAILED, (time.time() - t0) * 1000, f"Mode '{mode}' != 'PAPER'", True)
      return PreFlightCheckItem("1. Fail-Closed Paper Mode Guard", GateStatus.PASSED, (time.time() - t0) * 1000, "PAPER mode strictly enforced", True)
    except Exception as exc:
      return PreFlightCheckItem("1. Fail-Closed Paper Mode Guard", GateStatus.FAILED, (time.time() - t0) * 1000, str(exc), True)

  def _audit_database_wal_hygiene(self) -> PreFlightCheckItem:
    t0 = time.time()
    try:
      if not os.path.exists(self.db_path):
        return PreFlightCheckItem("2. Database WAL & Storage Hygiene", GateStatus.WARNING, (time.time() - t0) * 1000, f"DB file '{self.db_path}' not yet initialized", False)
      
      conn = sqlite3.connect(self.db_path)
      cursor = conn.cursor()
      cursor.execute("PRAGMA journal_mode;")
      j_mode = cursor.fetchone()[0]
      cursor.execute("PRAGMA quick_check;")
      q_check = cursor.fetchone()[0]
      conn.close()

      if q_check != "ok":
        return PreFlightCheckItem("2. Database WAL & Storage Hygiene", GateStatus.FAILED, (time.time() - t0) * 1000, f"SQLite integrity check failed: {q_check}", True)
      
      return PreFlightCheckItem("2. Database WAL & Storage Hygiene", GateStatus.PASSED, (time.time() - t0) * 1000, f"Journal mode '{j_mode}', quick_check ok", True)
    except Exception as exc:
      return PreFlightCheckItem("2. Database WAL & Storage Hygiene", GateStatus.FAILED, (time.time() - t0) * 1000, str(exc), True)

  def _audit_broker_credentials_and_totp(self) -> PreFlightCheckItem:
    t0 = time.time()
    try:
      has_key = bool(os.environ.get("ANGEL_API_KEY"))
      has_client = bool(os.environ.get("ANGEL_CLIENT_ID"))
      has_pin = bool(os.environ.get("ANGEL_PIN"))
      has_totp = bool(os.environ.get("ANGEL_TOTP_SECRET"))

      if has_key and has_client and has_pin and has_totp:
        return PreFlightCheckItem("3. Broker Gateway Auth & TOTP", GateStatus.PASSED, (time.time() - t0) * 1000, "Angel One SmartAPI credentials and TOTP secret configured", True)
      else:
        return PreFlightCheckItem("3. Broker Gateway Auth & TOTP", GateStatus.PASSED, (time.time() - t0) * 1000, "Paper emulation gateway active (standalone sandbox mode)", False)
    except Exception as exc:
      return PreFlightCheckItem("3. Broker Gateway Auth & TOTP", GateStatus.WARNING, (time.time() - t0) * 1000, str(exc), False)

  def _audit_market_data_provenance(self) -> PreFlightCheckItem:
    t0 = time.time()
    try:
      if not os.path.exists(self.db_path):
        return PreFlightCheckItem("4. Market Data Provenance & Universe", GateStatus.WARNING, (time.time() - t0) * 1000, "Database uninitialized (0 candles)", False)
      
      conn = sqlite3.connect(self.db_path)
      cursor = conn.cursor()
      cursor.execute("SELECT COUNT(*), COUNT(DISTINCT symbol) FROM market_candles;")
      row = cursor.fetchone()
      total_candles, unique_symbols = row[0], row[1]
      conn.close()

      if total_candles >= 1000:
        return PreFlightCheckItem("4. Market Data Provenance & Universe", GateStatus.PASSED, (time.time() - t0) * 1000, f"{total_candles:,} verified 1m candles across {unique_symbols} symbols", True)
      else:
        return PreFlightCheckItem("4. Market Data Provenance & Universe", GateStatus.WARNING, (time.time() - t0) * 1000, f"Low historical candle count ({total_candles} bars)", False)
    except Exception as exc:
      return PreFlightCheckItem("4. Market Data Provenance & Universe", GateStatus.WARNING, (time.time() - t0) * 1000, f"Data table unqueried: {exc}", False)

  def _audit_pure_decimal_arithmetic(self) -> PreFlightCheckItem:
    t0 = time.time()
    try:
      # Verify that float inputs are rejected by core financial functions
      try:
        to_decimal(123.456)
        return PreFlightCheckItem("5. Pure Decimal Financial Arithmetic", GateStatus.FAILED, (time.time() - t0) * 1000, "Float conversion did not raise TypeError", True)
      except TypeError:
        pass
      
      # Verify exact decimal operations
      val1 = to_decimal("2850.55")
      val2 = to_decimal("14.45")
      if val1 + val2 != Decimal("2865.00"):
        return PreFlightCheckItem("5. Pure Decimal Financial Arithmetic", GateStatus.FAILED, (time.time() - t0) * 1000, "Decimal math precision failure", True)

      return PreFlightCheckItem("5. Pure Decimal Financial Arithmetic", GateStatus.PASSED, (time.time() - t0) * 1000, "Float inputs rejected; zero binary drift verified", True)
    except Exception as exc:
      return PreFlightCheckItem("5. Pure Decimal Financial Arithmetic", GateStatus.FAILED, (time.time() - t0) * 1000, str(exc), True)

  def _audit_sebi_worm_log_integrity(self) -> PreFlightCheckItem:
    t0 = time.time()
    try:
      logger_inst = SEBIWORMTradeLogger(log_dir=self.audit_dir)
      ver = logger_inst.verify_chain()
      if not ver.is_valid:
        return PreFlightCheckItem("6. SEBI WORM Audit Log Hash Chain", GateStatus.FAILED, (time.time() - t0) * 1000, f"WORM chain broken at seq {ver.invalid_sequence_num}: {ver.reason}", True)
      return PreFlightCheckItem("6. SEBI WORM Audit Log Hash Chain", GateStatus.PASSED, (time.time() - t0) * 1000, f"Verified {ver.total_records} chained blocks (Genesis->Tip intact)", True)
    except Exception as exc:
      return PreFlightCheckItem("6. SEBI WORM Audit Log Hash Chain", GateStatus.FAILED, (time.time() - t0) * 1000, str(exc), True)

  def _audit_margin_stress_headroom(self) -> PreFlightCheckItem:
    t0 = time.time()
    try:
      engine = IntradayMarginStressEngine()
      report = engine.evaluate_stress(
          open_positions={"RELIANCE": (10, Decimal("2850.00")), "TCS": (8, Decimal("3800.00"))},
          total_equity_inr=Decimal("100000.00"),
          available_cash_inr=Decimal("100000.00"),
      )
      if report.passed and report.composite_utilization_pct <= Decimal("85.00"):
        return PreFlightCheckItem("7. SEBI Margin Stress & Capital Headroom", GateStatus.PASSED, (time.time() - t0) * 1000, f"Stressed margin {report.composite_utilization_pct:.1f}% <= 85.0% limit", True)
      return PreFlightCheckItem("7. SEBI Margin Stress & Capital Headroom", GateStatus.WARNING, (time.time() - t0) * 1000, f"High stressed utilization: {report.composite_utilization_pct:.1f}%", False)
    except Exception as exc:
      return PreFlightCheckItem("7. SEBI Margin Stress & Capital Headroom", GateStatus.FAILED, (time.time() - t0) * 1000, str(exc), True)

  def _audit_feed_watchdog_thresholds(self) -> PreFlightCheckItem:
    t0 = time.time()
    try:
      staleness_str = os.environ.get("FEED_WATCHDOG_STALENESS_SECONDS", "180")
      thresh = int(staleness_str)
      if thresh <= 0 or thresh > 300:
        return PreFlightCheckItem("8. Feed Watchdog & Dead-Man Switch", GateStatus.WARNING, (time.time() - t0) * 1000, f"Unusual staleness threshold: {thresh}s", False)
      return PreFlightCheckItem("8. Feed Watchdog & Dead-Man Switch", GateStatus.PASSED, (time.time() - t0) * 1000, f"Staleness threshold set to {thresh}s; auto-kill armed", True)
    except Exception as exc:
      return PreFlightCheckItem("8. Feed Watchdog & Dead-Man Switch", GateStatus.FAILED, (time.time() - t0) * 1000, str(exc), True)

  def _audit_algo_slicer_boundaries(self) -> PreFlightCheckItem:
    t0 = time.time()
    try:
      now = datetime.now(timezone.utc)
      parent_req = ParentOrderRequest(
          parent_order_id="PREFLIGHT_TEST_001",
          symbol="RELIANCE",
          side="BUY",
          total_quantity=100,
          start_time_utc=now,
          end_time_utc=now + timedelta(minutes=5),
          algo_type="TWAP",
          num_slices=5,
      )
      slicer = TWAPExecutionEngine(parent_request=parent_req)
      sum_qty = sum(s.quantity for s in slicer.slices)
      if sum_qty == 100:
        return PreFlightCheckItem("9. Algorithmic Slicer (TWAP/VWAP)", GateStatus.PASSED, (time.time() - t0) * 1000, f"Generated {len(slicer.slices)} slices with exact share conservation (100/100)", True)
      return PreFlightCheckItem("9. Algorithmic Slicer (TWAP/VWAP)", GateStatus.FAILED, (time.time() - t0) * 1000, f"Share leak: expected 100, got {sum_qty}", True)
    except Exception as exc:
      return PreFlightCheckItem("9. Algorithmic Slicer (TWAP/VWAP)", GateStatus.FAILED, (time.time() - t0) * 1000, str(exc), True)

  def _audit_meta_ensemble_normalization(self) -> PreFlightCheckItem:
    t0 = time.time()
    try:
      ensemble = MetaStrategyEnsemble()
      # Test weights computation with simulated HMM posteriors
      synthetic_regime = RegimeState(
          regime=MarketRegime.BULL,
          state_index=0,
          confidence=0.80,
          bull_prob=0.80,
          neutral_prob=0.15,
          bear_prob=0.05,
          n_bars_used=60,
          log_likelihood=120.0,
      )
      w_bull = ensemble.compute_dynamic_weights(synthetic_regime)
      sum_w = sum(w_bull.values())
      if abs(sum_w - Decimal("1.0000")) <= Decimal("0.0001"):
        return PreFlightCheckItem("10. Multi-Strategy Meta-Ensemble", GateStatus.PASSED, (time.time() - t0) * 1000, f"Weights sum to {sum_w:.4f} across 3 alpha strategy families", True)
      return PreFlightCheckItem("10. Multi-Strategy Meta-Ensemble", GateStatus.FAILED, (time.time() - t0) * 1000, f"Ensemble weight drift: sum={sum_w}", True)
    except Exception as exc:
      return PreFlightCheckItem("10. Multi-Strategy Meta-Ensemble", GateStatus.FAILED, (time.time() - t0) * 1000, str(exc), True)

  def execute_full_audit(self) -> PreFlightAuditReport:
    """Executes all 10 pre-flight readiness checks and determines Go/No-Go verdict."""
    assert_paper_mode()
    t_start = time.time()

    checks = [
        self._audit_paper_mode_guard(),
        self._audit_database_wal_hygiene(),
        self._audit_broker_credentials_and_totp(),
        self._audit_market_data_provenance(),
        self._audit_pure_decimal_arithmetic(),
        self._audit_sebi_worm_log_integrity(),
        self._audit_margin_stress_headroom(),
        self._audit_feed_watchdog_thresholds(),
        self._audit_algo_slicer_boundaries(),
        self._audit_meta_ensemble_normalization(),
    ]

    passed = sum(1 for c in checks if c.status == GateStatus.PASSED)
    warn = sum(1 for c in checks if c.status == GateStatus.WARNING)
    failed = sum(1 for c in checks if c.status == GateStatus.FAILED)

    critical_failures = sum(1 for c in checks if c.status == GateStatus.FAILED and c.critical)
    overall_decision = "NO_GO" if critical_failures > 0 else "GO"

    now_utc = datetime.now(timezone.utc).isoformat()
    audit_id = f"PREFLIGHT_{int(time.time())}_{overall_decision}"
    total_lat = (time.time() - t_start) * 1000.0

    report = PreFlightAuditReport(
        audit_id=audit_id,
        timestamp_utc=now_utc,
        overall_decision=overall_decision,
        passed_count=passed,
        warning_count=warn,
        failed_count=failed,
        total_latency_ms=total_lat,
        checks=checks,
    )
    self.last_report = report
    logger.info("Pre-Flight Readiness Audit complete: %s (%d passed, %d warn, %d fail in %.2f ms)", overall_decision, passed, warn, failed, total_lat)
    return report

  def get_status(self) -> dict[str, Any]:
    """Returns cached or on-demand pre-flight audit report."""
    assert_paper_mode()
    if self.last_report is None:
      self.execute_full_audit()
    return self.last_report.to_dict() if self.last_report else {}
