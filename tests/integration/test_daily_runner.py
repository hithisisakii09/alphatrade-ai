"""Integration tests for ScheduledDailyRunner APScheduler jobs and runner API endpoint."""

from decimal import Decimal
from fastapi.testclient import TestClient

from app.core.clock import ist_datetime
from app.main import app
from app.services.daily_runner import ScheduledDailyRunner, generate_realistic_session_bars


def test_scheduled_daily_runner_jobs_and_lifecycle():
  runner = ScheduledDailyRunner()
  jobs = runner.get_jobs_summary()
  assert len(jobs) == 6
  job_ids = {j["id"] for j in jobs}
  assert "nse_pre_market_0900" in job_ids
  assert "nse_market_open_0915" in job_ids
  assert "nse_orb_formed_0930" in job_ids
  assert "nse_entry_cutoff_1430" in job_ids
  assert "nse_eod_liquidation_1500" in job_ids
  assert "nse_session_close_1535" in job_ids

  # Test pre-market check execution
  res = runner.job_pre_market_check()
  assert "is_trading_day" in res
  assert "session_state" in res

  # Test dynamic enabling/disabling of 1-minute intraday tick job
  runner.enable_intraday_tick_job()
  assert len(runner.get_jobs_summary()) == 7
  assert "nse_intraday_tick" in {j["id"] for j in runner.get_jobs_summary()}
  runner.disable_intraday_tick_job()
  assert len(runner.get_jobs_summary()) == 6

  # Test lifecycle start/stop
  runner.start()
  assert runner.is_running is True
  runner.stop()
  assert runner.is_running is False


def test_runner_status_fastapi_endpoint(monkeypatch=None):
  from unittest.mock import patch
  client = TestClient(app)
  resp = client.get("/runner/status")
  assert resp.status_code == 200
  data = resp.json()
  assert "is_running" in data
  assert data["jobs_count"] == 6
  assert len(data["jobs"]) == 6

  # Test trigger tick endpoint (mock tick to avoid live network delay during test run)
  with patch("app.api.routes._daily_runner.job_intraday_tick", return_value={"status": "SUCCESS", "mock": True}):
    tick_resp = client.post("/runner/tick")
    assert tick_resp.status_code == 200
    assert tick_resp.json()["status"] == "SUCCESS"


def test_intraday_tick_breakout_execution_and_eod_liquidation(tmp_path=None):
  import tempfile
  from pathlib import Path
  folder = tmp_path or Path(tempfile.mkdtemp())
  test_db = f"sqlite:///{folder}/test_live_runner.db"
  runner = ScheduledDailyRunner(database_url=test_db)

  # Generate 35 minutes of 1m bars for RELIANCE (09:15 to 09:49 IST)
  # Bars 0-14 form ORB; bars 15-19 breakout above ORH with volume
  bars = generate_realistic_session_bars("RELIANCE", 2026, 11, 10, Decimal("1250.00"))[:35]
  eval_time = ist_datetime(2026, 11, 10, 9, 49)

  summary = runner.job_intraday_tick(now_utc=eval_time, custom_bars_by_symbol={"RELIANCE": bars})
  assert summary["status"] == "SUCCESS"
  assert summary["symbols_evaluated"] == 1
  assert len(summary["signals_generated"]) == 1
  assert summary["signals_generated"][0]["symbol"] == "RELIANCE"
  assert len(summary["orders_placed"]) == 1
  assert len(summary["orders_filled"]) == 1
  assert summary["open_positions_count"] == 1

  # Test forced EOD liquidation window at 15:00 IST
  liq_time = ist_datetime(2026, 11, 10, 15, 0)
  liq_summary = runner.job_intraday_tick(now_utc=liq_time, custom_bars_by_symbol={"RELIANCE": bars})
  assert liq_summary["status"] == "EOD_LIQUIDATION_WINDOW"
  assert len(liq_summary["liquidation_result"]["positions_closed"]) == 1
  assert liq_summary["liquidation_result"]["positions_closed"][0]["symbol"] == "RELIANCE"


def test_tick_latency_profiling_and_telemetry_summary(tmp_path=None):
  import tempfile
  from pathlib import Path
  folder = tmp_path or Path(tempfile.mkdtemp())
  test_db = f"sqlite:///{folder}/test_telemetry.db"
  runner = ScheduledDailyRunner(database_url=test_db)

  bars = generate_realistic_session_bars("RELIANCE", 2026, 11, 10, Decimal("1250.00"))[:35]
  eval_time = ist_datetime(2026, 11, 10, 9, 49)

  # Run tick and verify latency profiling metrics
  summary = runner.job_intraday_tick(now_utc=eval_time, custom_bars_by_symbol={"RELIANCE": bars})
  assert summary["status"] == "SUCCESS"
  assert "latency_ms" in summary
  assert summary["latency_ms"] > 0.0
  assert summary["latency_budget_exceeded"] is False

  # Verify rolling telemetry summary
  telemetry = runner.get_telemetry_summary()
  assert telemetry["total_ticks_executed"] == 1
  assert telemetry["recent_ticks_tracked"] == 1
  assert telemetry["avg_latency_ms"] > 0.0
  assert telemetry["max_latency_ms"] > 0.0
  assert telemetry["latency_budget_ms"] == 500.0
  assert telemetry["budget_compliant"] is True
  assert len(telemetry["recent_latencies"]) == 1

