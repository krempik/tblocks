import importlib
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture(scope="session")
def client():
    """Isolated app against a throwaway temp database, lifespan included."""
    tmpdir = tempfile.mkdtemp(prefix="tblocks_test_")
    os.environ["TBLOCKS_DB"] = os.path.join(tmpdir, "test.db")
    os.environ["TBLOCKS_MAX_ROWS"] = "50"
    os.environ["TBLOCKS_RATE_LIMIT"] = "100000"
    importlib.reload(server)
    with TestClient(server.app) as c:
        yield c


def test_version_consistency(client):
    r = client.get("/api/version")
    assert r.status_code == 200
    version = r.json()["version"]
    assert version == open(server.VERSION_FILE, encoding="utf-8").read().strip()

    index_html = open("index.html", encoding="utf-8").read()
    assert f"v{version}" in index_html
    assert f"{version}" in open("manifest.json", encoding="utf-8").read()
    assert version in open("sw.js", encoding="utf-8").read()
    assert version in open("README.md", encoding="utf-8").read()


def test_stats(client):
    r = client.get("/api/stats")
    assert r.status_code == 200
    data = r.json()
    assert "total_games" in data
    assert "total_players" in data
    assert "best_scores" in data


def test_leaderboard_default(client):
    r = client.get("/api/leaderboard")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_leaderboard_modes(client):
    for mode in ("classic", "marathon", "boss", "ultra"):
        r = client.get(f"/api/leaderboard?mode={mode}")
        assert r.status_code == 200


def test_submit_score(client):
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


def test_submit_score_invalid_mode(client):
    r = client.post("/api/score", json={
        "name": "Test",
        "mode": "invalid_mode",
        "score": 100
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_submit_score_validation(client):
    r = client.post("/api/score", json={
        "name": "",
        "mode": "classic",
        "score": -100,
        "lines": -5,
        "level": -1
    })
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["id"] > 0


def test_score_dedup_keeps_best(client):
    name = "DedupBoy"
    low = client.post("/api/score", json={"name": name, "mode": "classic", "score": 100})
    high = client.post("/api/score", json={"name": name, "mode": "classic", "score": 5000})
    assert low.status_code == 200 and high.status_code == 200
    eager = client.post("/api/score", json={"name": name, "mode": "classic", "score": 10})
    assert eager.status_code == 200
    assert eager.json()["id"] == low.json()["id"]
    assert eager.json()["best"] is True

    rows = client.get("/api/leaderboard?mode=classic&limit=100").json()
    mine = [x for x in rows if x["name"] == name]
    assert len(mine) == 1
    assert mine[0]["score"] == 5000


def test_leaderboard_trim_keeps_top(client):
    for i in range(60):
        r = client.post("/api/score", json={"name": f"Trimmer{i}", "mode": "ultra", "score": i})
        assert r.status_code == 200
    rows = client.get("/api/leaderboard?mode=ultra&limit=100").json()
    assert len(rows) <= 50
    assert rows[0]["score"] == 59


def test_leaderboard_has_score(client):
    client.post("/api/score", json={
        "name": "LeaderTest",
        "mode": "classic",
        "score": 999999
    })
    r = client.get("/api/leaderboard?mode=classic")
    scores = r.json()
    assert any(s["name"] == "LeaderTest" for s in scores)


def test_position(client):
    r = client.get("/api/leaderboard/position?mode=classic&score=50000")
    assert r.status_code == 200
    data = r.json()
    assert "position" in data
    assert "total" in data


def test_visitors(client):
    r = client.post("/api/visit", params={"page": "/test"})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r = client.get("/api/visitors")
    assert r.status_code == 200
    data = r.json()
    assert "total" in data
    assert "today" in data


def test_visitors_today_from_db(client):
    before = client.get("/api/visitors").json()["today"]
    client.post("/api/visit", params={"page": "/more"})
    after = client.get("/api/visitors").json()["today"]
    assert after == before + 1


def test_online(client):
    r = client.post("/api/online", params={"session_id": "test123"})
    assert r.status_code == 200
    assert "online" in r.json()

    r = client.get("/api/online")
    assert r.status_code == 200