"""Unit tests for Milestone M14: Trade Analytics, MAE/MFE computation, Sector Attribution, and Session Journal."""

from datetime import datetime, timezone
from decimal import Decimal
import io
import csv
import pytest
from fastapi.testclient import TestClient

from app.api.routes import router
from app.core.clock import ist_datetime
from app.core.database import PositionRecord, MarketCandleRecord, PortfolioSnapshotRecord, init_db
from app.reporting.analytics import (
    compute_position_mae_mfe,
    compute_trade_analytics,
    generate_trade_journal_csv,
)


def _setup_test_db():
  _, SessionLocal = init_db("sqlite:///:memory:")
  db = SessionLocal()

  # Insert candles for MAE/MFE testing
  for i in range(10):
    p = Decimal("1000.00") + Decimal(i * 5)
    db.add(
        MarketCandleRecord(
            symbol="RELIANCE",
            interval_minutes=1,
            timestamp_utc=ist_datetime(2026, 11, 12, 10, i),
            open=p,
            high=p + Decimal("10.00"),
            low=p - Decimal("5.00"),
            close=p + Decimal("2.00"),
            volume=10000,
            source="TEST",
        )
    )

  # Insert mock positions: 1 Win, 1 Loss, 1 Open
  # Win (RELIANCE - ENERGY)
  p1 = PositionRecord(
      position_id="POS_TEST_WIN",
      symbol="RELIANCE",
      session_date="2026-11-12",
      status="CLOSED",
      quantity=10,
      entry_price=Decimal("1000.00"),
      exit_price=Decimal("1025.00"),
      initial_stop_price=Decimal("987.50"),  # Risk = 12.50/share
      current_stop_price=Decimal("1020.00"),
      trailing_activation_price=Decimal("1025.00"),
      trailing_activated=True,
      highest_price_since_entry=Decimal("1030.00"),
      trailing_drop_pct=Decimal("0.0075"),
      entry_fees_inr=Decimal("15.00"),
      exit_fees_inr=Decimal("15.00"),
      realized_pnl_inr=Decimal("220.00"),
      unrealized_pnl_inr=Decimal("0.00"),
      exit_reason="TRAILING_STOP_EXIT",
      opened_at_utc=ist_datetime(2026, 11, 12, 10, 0),
      closed_at_utc=ist_datetime(2026, 11, 12, 10, 30),
  )

  # Loss (TCS - IT)
  p2 = PositionRecord(
      position_id="POS_TEST_LOSS",
      symbol="TCS",
      session_date="2026-11-12",
      status="CLOSED",
      quantity=10,
      entry_price=Decimal("2500.00"),
      exit_price=Decimal("2470.00"),
      initial_stop_price=Decimal("2468.75"),  # Risk = 31.25/share
      current_stop_price=Decimal("2468.75"),
      trailing_activation_price=Decimal("2562.50"),
      trailing_activated=False,
      highest_price_since_entry=Decimal("2510.00"),
      trailing_drop_pct=Decimal("0.0075"),
      entry_fees_inr=Decimal("20.00"),
      exit_fees_inr=Decimal("20.00"),
      realized_pnl_inr=Decimal("-340.00"),
      unrealized_pnl_inr=Decimal("0.00"),
      exit_reason="INITIAL_STOP_LOSS",
      opened_at_utc=ist_datetime(2026, 11, 12, 10, 5),
      closed_at_utc=ist_datetime(2026, 11, 12, 10, 20),
  )

  # Open (HDFCBANK - BANKING)
  p3 = PositionRecord(
      position_id="POS_TEST_OPEN",
      symbol="HDFCBANK",
      session_date="2026-11-12",
      status="OPEN",
      quantity=20,
      entry_price=Decimal("800.00"),
      exit_price=None,
      initial_stop_price=Decimal("790.00"),  # Risk = 10.00/share
      current_stop_price=Decimal("790.00"),
      trailing_activation_price=Decimal("820.00"),
      trailing_activated=False,
      highest_price_since_entry=Decimal("812.00"),
      trailing_drop_pct=Decimal("0.0075"),
      entry_fees_inr=Decimal("12.00"),
      exit_fees_inr=Decimal("0.00"),
      realized_pnl_inr=Decimal("0.00"),
      unrealized_pnl_inr=Decimal("150.00"),
      exit_reason=None,
      opened_at_utc=ist_datetime(2026, 11, 12, 10, 10),
      closed_at_utc=None,
  )

  db.add(p1)
  db.add(p2)
  db.add(p3)

  # Insert a snapshot
  snap = PortfolioSnapshotRecord(
      timestamp_utc=ist_datetime(2026, 11, 12, 10, 15),
      starting_capital_inr=Decimal("100000.00"),
      cash_balance_inr=Decimal("40000.00"),
      deployed_exposure_inr=Decimal("60000.00"),
      realized_pnl_inr=Decimal("-120.00"),
      unrealized_pnl_inr=Decimal("150.00"),
      total_fees_inr=Decimal("67.00"),
      net_equity_inr=Decimal("100030.00"),
  )
  db.add(snap)
  db.commit()
  return db


def test_compute_position_mae_mfe():
  db = _setup_test_db()
  p1 = db.query(PositionRecord).filter(PositionRecord.position_id == "POS_TEST_WIN").first()
  analytics = compute_position_mae_mfe(p1, db=db)

  # Risk = 1000 - 987.50 = 12.50
  assert Decimal(analytics["risk_per_share"]) == Decimal("12.50")
  # MFE = 1030 - 1000 = 30.00 -> MFE R = 30 / 12.5 = 2.40 R
  assert Decimal(analytics["mfe_points"]) == Decimal("30.00")
  assert Decimal(analytics["mfe_r"]) == Decimal("2.4000")
  # Duration: 10:00 to 10:30 = 30 mins
  assert analytics["duration_minutes"] == 30


def test_compute_trade_analytics_aggregation():
  db = _setup_test_db()
  res = compute_trade_analytics(db)

  assert res["total_trades"] == 3
  assert res["closed_trades"] == 2
  assert res["open_trades"] == 1
  assert res["winning_trades"] == 1
  assert res["losing_trades"] == 1
  # Win rate = 1 / 2 = 50.0%
  assert Decimal(res["win_rate_pct"]) == Decimal("50.0000")
  # Profit factor = 220 / 340 = 0.6471
  assert Decimal(res["profit_factor"]) == Decimal("0.6471")

  # Sector breakdown: ENERGY (RELIANCE), IT (TCS), BANKING (HDFCBANK)
  sectors = {s["sector"]: s for s in res["sector_breakdown"]}
  assert "ENERGY" in sectors
  assert "IT" in sectors
  assert "BANKING" in sectors
  assert sectors["ENERGY"]["net_pnl_inr"] == "220.00"
  assert sectors["IT"]["net_pnl_inr"] == "-340.00"


def test_generate_trade_journal_csv():
  db = _setup_test_db()
  csv_content = generate_trade_journal_csv(db)

  assert "position_id,session_date,symbol,sector" in csv_content
  assert "POS_TEST_WIN" in csv_content
  assert "POS_TEST_LOSS" in csv_content
  assert "POS_TEST_OPEN" in csv_content

  # Parse CSV back
  reader = csv.DictReader(io.StringIO(csv_content))
  rows = list(reader)
  assert len(rows) == 3
  win_row = next(r for r in rows if r["position_id"] == "POS_TEST_WIN")
  assert win_row["symbol"] == "RELIANCE"
  assert win_row["sector"] == "ENERGY"
  assert win_row["mfe_r"] == "2.4000"


def test_m14_api_routes_integration():
  from fastapi import FastAPI
  app = FastAPI()
  app.include_router(router)
  client = TestClient(app)

  # 1. Snapshots endpoint
  resp_snap = client.get("/portfolio/snapshots?limit=5")
  assert resp_snap.status_code == 200
  data_snap = resp_snap.json()
  assert "snapshots" in data_snap
  assert isinstance(data_snap["snapshots"], list)

  # 2. Analytics endpoint
  resp_ana = client.get("/reports/analytics")
  assert resp_ana.status_code == 200
  data_ana = resp_ana.json()
  assert "total_trades" in data_ana
  assert "win_rate_pct" in data_ana
  assert "sector_breakdown" in data_ana

  # 3. Journal JSON endpoint
  resp_jnl = client.get("/reports/journal")
  assert resp_jnl.status_code == 200
  data_jnl = resp_jnl.json()
  assert "journal" in data_jnl
  assert "summary" in data_jnl

  # 4. Journal CSV endpoint
  resp_csv = client.get("/reports/journal.csv")
  assert resp_csv.status_code == 200
  assert resp_csv.headers["content-type"].startswith("text/csv")
  assert "position_id" in resp_csv.text
