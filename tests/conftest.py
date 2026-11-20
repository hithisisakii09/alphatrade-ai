"""Pytest global configuration and fixtures.
Ensures every test runs in clean PAPER mode by default so safety guards and regression tests pass cleanly.
"""
import os
import pytest

@pytest.fixture(autouse=True, scope="function")
def default_paper_test_environment(monkeypatch):
    monkeypatch.setenv("ALPHATRADE_EXECUTION_MODE", "PAPER")
    monkeypatch.setenv("ALPHATRADE_LIVE_CONFIRMATION", "")
    monkeypatch.setenv("ALPHATRADE_CAPITAL_PROFILE", "STANDARD_RESEARCH_100K")
    monkeypatch.setenv("ALPHATRADE_ENABLE_INTRADAY_POLLER", "false")
