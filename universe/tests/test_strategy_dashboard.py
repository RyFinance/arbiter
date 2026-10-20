from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
import server  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "STRATEGY_SNAPSHOT_PATH", tmp_path / "strategy_snapshot.json")
    return TestClient(server.app)

def test_strategy_endpoint_reports_missing_snapshot(client):
    response = client.get("/api/strategy")
    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "mode": "unavailable",
        "open_sequences": [],
    }


def test_strategy_endpoint_returns_snapshot(client, tmp_path, monkeypatch):
    path = tmp_path / "strategy_snapshot.json"
    path.write_text(json.dumps({
        "mode": "sequential",
        "phase_counts": {"seeking_second": 1},
        "open_sequences": [{"market_id": "m1"}],
    }))
    monkeypatch.setattr(server, "STRATEGY_SNAPSHOT_PATH", path)

    body = client.get("/api/strategy").json()
    assert body["available"] is True
    assert body["mode"] == "sequential"
    assert body["open_sequences"][0]["market_id"] == "m1"
