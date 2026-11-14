"""Real-Time Shadow Reconciler & State Parity Engine (Milestone M24).

Conforms strictly to institutional execution integrity standards:
1. Pure Decimal financial arithmetic with zero binary float drift.
2. Sub-second discrepancy detection between local PortfolioLedger and external BrokerGateway.
3. Detects Orphan positions, Phantom positions, Quantity mismatches, Price drifts, and Margin discrepancies.
4. Automated fail-safe risk reduction: Trips RiskEngine kill-switch fail-closed upon critical variance.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from typing import Any
import uuid

from app.core.execution_guard import assert_paper_mode
from app.core.money import round_paise, round_price, to_decimal
from app.execution.gateway import BrokerGateway, BrokerPosition, BrokerMarginInfo


@dataclass(frozen=True)
class ReconciliationDiscrepancy:
  """Detailed discrepancy record between internal ledger and external broker."""
  discrepancy_type: str  # "ORPHAN_BROKER_POSITION", "PHANTOM_LEDGER_POSITION", "QUANTITY_MISMATCH", "PRICE_MISMATCH", "MARGIN_DISCREPANCY"
  symbol: str
  internal_value: str
  broker_value: str
  variance: str
  severity: str  # "CRITICAL", "WARNING"
  timestamp_utc: datetime


@dataclass(frozen=True)
class ShadowReconciliationReport:
  """Comprehensive institutional audit report from shadow reconciliation."""
  audit_id: str
  timestamp_utc: datetime
  status: str  # "HEALTHY", "DISCREPANCY_DETECTED"
  internal_positions_count: int
  broker_positions_count: int
  discrepancies_count: int
  discrepancies: list[ReconciliationDiscrepancy]
  margin_discrepancy_inr: Decimal
  kill_switch_triggered: bool
  reconciliation_run_count: int

  def to_dict(self) -> dict[str, Any]:
    return {
        "audit_id": self.audit_id,
        "timestamp_utc": self.timestamp_utc.isoformat(),
        "status": self.status,
        "internal_positions_count": self.internal_positions_count,
        "broker_positions_count": self.broker_positions_count,
        "discrepancies_count": self.discrepancies_count,
        "discrepancies": [
            {
                "discrepancy_type": d.discrepancy_type,
                "symbol": d.symbol,
                "internal_value": d.internal_value,
                "broker_value": d.broker_value,
                "variance": d.variance,
                "severity": d.severity,
                "timestamp_utc": d.timestamp_utc.isoformat(),
            }
            for d in self.discrepancies
        ],
        "margin_discrepancy_inr": str(self.margin_discrepancy_inr),
        "kill_switch_triggered": self.kill_switch_triggered,
        "reconciliation_run_count": self.reconciliation_run_count,
    }


class RealTimeShadowReconciler:
  """Institutional Real-Time State Reconciler comparing local ledger against broker books."""

  def __init__(
      self,
      gateway: BrokerGateway,
      ledger: Any = None,
      risk_engine: Any = None,
      price_tolerance_pct: Decimal = Decimal("0.0005"),  # 0.05%
      margin_tolerance_pct: Decimal = Decimal("0.10"),   # 10.0%
  ):
    assert_paper_mode()
    self.gateway = gateway
    self.ledger = ledger
    self.risk_engine = risk_engine
    self.price_tolerance_pct = to_decimal(price_tolerance_pct)
    self.margin_tolerance_pct = to_decimal(margin_tolerance_pct)
    self.run_count: int = 0
    self.last_report: ShadowReconciliationReport | None = None

  def reconcile(
      self,
      explicit_internal_positions: dict[str, tuple[int, Decimal]] | None = None,
      explicit_internal_margin: Decimal | None = None,
  ) -> ShadowReconciliationReport:
    """Executes state reconciliation between local ledger and external broker gateway."""
    assert_paper_mode()
    self.run_count += 1
    now = datetime.now(timezone.utc)
    audit_id = f"AUD_{uuid.uuid4().hex[:10]}"

    # 1. Fetch internal positions map (symbol -> (quantity, avg_price))
    internal_pos: dict[str, tuple[int, Decimal]] = {}
    if explicit_internal_positions is not None:
      internal_pos = explicit_internal_positions
    elif self.ledger is not None and hasattr(self.ledger, "get_open_positions"):
      raw_positions = self.ledger.get_open_positions()
      for sym, p in raw_positions.items():
        qty = getattr(p, "quantity", 0)
        avg_p = getattr(p, "entry_price", Decimal("0.00"))
        if qty > 0:
          internal_pos[sym] = (qty, to_decimal(avg_p))

    # 2. Fetch external broker positions
    broker_positions = self.gateway.get_positions()
    broker_pos_map: dict[str, BrokerPosition] = {
        p.symbol: p for p in broker_positions if p.quantity > 0
    }

    discrepancies: list[ReconciliationDiscrepancy] = []

    # Check for Orphan positions (broker has position, ledger does not)
    for sym, b_pos in broker_pos_map.items():
      if sym not in internal_pos:
        discrepancies.append(
            ReconciliationDiscrepancy(
                discrepancy_type="ORPHAN_BROKER_POSITION",
                symbol=sym,
                internal_value="NONE",
                broker_value=f"{b_pos.quantity} shares @ ₹{b_pos.average_price}",
                variance=f"+{b_pos.quantity} shares",
                severity="CRITICAL",
                timestamp_utc=now,
            )
        )

    # Check for Phantom positions & Quantity / Price mismatches
    for sym, (i_qty, i_price) in internal_pos.items():
      if sym not in broker_pos_map:
        discrepancies.append(
            ReconciliationDiscrepancy(
                discrepancy_type="PHANTOM_LEDGER_POSITION",
                symbol=sym,
                internal_value=f"{i_qty} shares @ ₹{i_price}",
                broker_value="NONE",
                variance=f"-{i_qty} shares",
                severity="CRITICAL",
                timestamp_utc=now,
            )
        )
      else:
        b_pos = broker_pos_map[sym]
        # Quantity check
        if i_qty != b_pos.quantity:
          diff = b_pos.quantity - i_qty
          discrepancies.append(
              ReconciliationDiscrepancy(
                  discrepancy_type="QUANTITY_MISMATCH",
                  symbol=sym,
                  internal_value=f"{i_qty} shares",
                  broker_value=f"{b_pos.quantity} shares",
                  variance=f"{diff:+d} shares",
                  severity="CRITICAL",
                  timestamp_utc=now,
              )
          )

        # Price check
        if b_pos.average_price > Decimal("0"):
          price_drift = abs(i_price - b_pos.average_price) / b_pos.average_price
          if price_drift > self.price_tolerance_pct:
            discrepancies.append(
                ReconciliationDiscrepancy(
                    discrepancy_type="PRICE_MISMATCH",
                    symbol=sym,
                    internal_value=f"₹{i_price}",
                    broker_value=f"₹{b_pos.average_price}",
                    variance=f"{price_drift * 100:.3f}%",
                    severity="WARNING",
                    timestamp_utc=now,
                )
            )

    # 3. Margin check
    broker_margins = self.gateway.get_margins()
    internal_margin = Decimal("0.00")
    if explicit_internal_margin is not None:
      internal_margin = explicit_internal_margin
    elif self.ledger is not None and hasattr(self.ledger, "get_deployed_exposure"):
      internal_margin = round_paise(self.ledger.get_deployed_exposure() * Decimal("0.20"))

    margin_diff = abs(internal_margin - broker_margins.used_margin)
    if broker_margins.used_margin > Decimal("0"):
      margin_pct_drift = margin_diff / broker_margins.used_margin
      if margin_pct_drift > self.margin_tolerance_pct:
        discrepancies.append(
            ReconciliationDiscrepancy(
                discrepancy_type="MARGIN_DISCREPANCY",
                symbol="PORTFOLIO",
                internal_value=f"₹{internal_margin}",
                broker_value=f"₹{broker_margins.used_margin}",
                variance=f"₹{margin_diff} ({margin_pct_drift * 100:.2f}%)",
                severity="WARNING",
                timestamp_utc=now,
            )
        )

    # 4. Automated fail-safe risk reduction
    kill_switch_triggered = False
    critical_discrepancies = [d for d in discrepancies if d.severity == "CRITICAL"]
    if critical_discrepancies and self.risk_engine is not None:
      c_disc = critical_discrepancies[0]
      reason = f"SHADOW_RECON_FAILURE:{c_disc.discrepancy_type}:{c_disc.symbol}"
      if hasattr(self.risk_engine, "trigger_kill_switch"):
        self.risk_engine.trigger_kill_switch(reason)
      elif hasattr(self.risk_engine, "trip_kill_switch"):
        self.risk_engine.trip_kill_switch(reason)
      kill_switch_triggered = True

    status = "DISCREPANCY_DETECTED" if discrepancies else "HEALTHY"

    report = ShadowReconciliationReport(
        audit_id=audit_id,
        timestamp_utc=now,
        status=status,
        internal_positions_count=len(internal_pos),
        broker_positions_count=len(broker_pos_map),
        discrepancies_count=len(discrepancies),
        discrepancies=discrepancies,
        margin_discrepancy_inr=margin_diff,
        kill_switch_triggered=kill_switch_triggered,
        reconciliation_run_count=self.run_count,
    )
    self.last_report = report
    return report
