"""Integration tests for ScheduledDailyRunner APScheduler jobs and runner API endpoint."""

from fastapi.testclient import TestClient

from app.main import app
from app.services.daily_runner import ScheduledDailyRunner


def test_scheduled_daily_runner_jobs_and_lifecycle():
  runner = ScheduledDailyRunner()
  jobs = runner.get_jobs_summary()
  assert len(jobs) == 6
  job_ids = {j["id"] for j in jobs}
  assert "nse_pre_market_0900" in job_ids
  assert "nse_orb_formed_0930" in job_ids
  assert "nse_intraday_tick" in job_ids
  assert "nse_entry_cutoff_1430" in job_ids
  assert "nse_eod_liquidation_1500" in job_ids
  assert "nse_session_close_1530" in job_ids

  # Test pre-market check execution
  res = runner.job_pre_market_check()
  assert "is_trading_day" in res
  assert "session_state" in res

  # Test lifecycle start/stop
  runner.start()
  assert runner.is_running is True
  runner.stop()
  assert runner.is_running is False


def test_runner_status_fastapi_endpoint():
  client = TestClient(app)
  resp = client.get("/runner/status")
  assert resp.status_code == 200
  data = resp.json()
  assert "is_running" in data
  assert data["jobs_count"] == 6
  assert len(data["jobs"]) == 6

