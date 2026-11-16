"""Unit tests for autonomous exit endpoints: POST /positions/{symbol}/exit and POST /positions/exit-all."""

import pytest
from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_exit_nonexistent_symbol_returns_not_found(client):
    resp = client.post("/positions/NONEXISTENT_XYZ/exit")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "NOT_FOUND"


def test_exit_all_when_empty_returns_success(client):
    resp = client.post("/positions/exit-all")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "SUCCESS"
    assert data["freed_for_next_trade"] is True
