import pytest
from fastapi.testclient import TestClient
from server import app

client = TestClient(app)


def test_stats():
    r = client.get("/api/stats")
    assert r.status_code == 200
    data = r.json()
    assert "total_games" in data
    assert "total_players" in data
    assert "best_scores" in data


def test_leaderboard_default():
    r = client.get("/api/leaderboard")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_leaderboard_modes():
    for mode in ("classic", "marathon", "boss", "ultra"):
        r = client.get(f"/api/leaderboard?mode={mode}")
        assert r.status_code == 200


def test_submit_score():
    r = client.post("/api/score", json={
        "name": "TestPlayer",
        "mode": "classic",
        "score": 99999,
        "lines": 42,
        "level": 10
    })
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "position" in data


def test_submit_score_invalid_mode():
    r = client.post("/api/score", json={
        "name": "Test",
        "mode": "invalid_mode",
        "score": 100
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_leaderboard_has_score():
    client.post("/api/score", json={
        "name": "LeaderTest",
        "mode": "classic",
        "score": 999999
    })
    r = client.get("/api/leaderboard?mode=classic")
    scores = r.json()
    assert any(s["name"] == "LeaderTest" for s in scores)


def test_position():
    r = client.get("/api/leaderboard/position?mode=classic&score=50000")
    assert r.status_code == 200
    data = r.json()
    assert "position" in data
    assert "total" in data
