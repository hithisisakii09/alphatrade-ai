"""Verification script for live market paper-trading runner and dashboard."""

import json
import httpx

client = httpx.Client(base_url="http://127.0.0.1:8000", timeout=5.0)

# 1. Health check
h = client.get("/health")
print("=== 1. HEALTH CHECK ===")
print(f"HTTP Status: {h.status_code}")
print(json.dumps(h.json(), indent=2))
assert h.status_code == 200
assert h.json()["execution_mode"] == "PAPER"

# 2. Check runner status and jobs
r = client.get("/runner/status")
print("\n=== 2. RUNNER STATUS & 6 INTRADAY CRON JOBS ===")
print(f"HTTP Status: {r.status_code}")
r_data = r.json()
print(f"Scheduler is_running: {r_data.get('is_running')}")
print(f"Active Jobs Count: {r_data.get('jobs_count')}")
for j in r_data.get("jobs", []):
  print(f"  • [{j['id']}] {j['name']} (Trigger: {j['trigger']})")

assert r.status_code == 200
assert r_data["is_running"] is True
assert r_data["jobs_count"] == 6

expected_jobs = {
    "nse_pre_market_0900",
    "nse_market_open_0915",
    "nse_orb_formed_0930",
    "nse_entry_cutoff_1430",
    "nse_eod_liquidation_1500",
    "nse_session_close_1535",
}
actual_jobs = {j["id"] for j in r_data["jobs"]}
assert expected_jobs == actual_jobs, f"Job mismatch: {expected_jobs ^ actual_jobs}"

# 3. Check dashboard HTML endpoints
d1 = client.get("/")
d2 = client.get("/dashboard")
d3 = client.get("/dashboard/")
print("\n=== 3. DASHBOARD HTML ENDPOINTS ===")
print(f"GET /           HTTP Status: {d1.status_code} ({len(d1.text)} bytes)")
print(f"GET /dashboard  HTTP Status: {d2.status_code} ({len(d2.text)} bytes)")
print(f"GET /dashboard/ HTTP Status: {d3.status_code} ({len(d3.text)} bytes)")
assert d1.status_code == 200
assert d2.status_code == 200
assert d3.status_code == 200
assert "AlphaTrade AI v2.0" in d3.text
assert "setInterval" in d3.text

print("\n>>> ALL VERIFICATION CHECKS PASSED: RUNNER IS LIVE AND ARMED! <<<")

