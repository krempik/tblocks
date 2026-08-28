import os
import time
import json
import subprocess
import threading
import re
from typing import Optional
from functools import lru_cache
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from sqlalchemy import create_engine, Column, Integer, String, DateTime, func, Index
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, timezone
from contextlib import asynccontextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "leaderboard.db")
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False}, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

VERSION_FILE = Path(__file__).parent / "VERSION"
def get_version():
    try:
        return VERSION_FILE.read_text().strip()
    except Exception:
        return "4.0.0"

_rate_limit_lock = threading.Lock()
_visitor_lock = threading.Lock()
_online_lock = threading.Lock()


class Score(Base):
    __tablename__ = "scores"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(32), nullable=False)
    mode = Column(String(16), nullable=False, index=True)
    score = Column(Integer, nullable=False)
    lines = Column(Integer, default=0)
    level = Column(Integer, default=1)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_scores_mode_score", "mode", "score"),
    )


class VisitorLog(Base):
    __tablename__ = "visitors"
    id = Column(Integer, primary_key=True, index=True)
    ip = Column(String(64))
    page = Column(String(128))
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


Base.metadata.create_all(bind=engine)


TUNNEL_URL: Optional[str] = None
_tunnel_process: Optional[subprocess.Popen] = None
HOST_START_TIME = time.time()

_rate_limit_store: dict[str, list[float]] = {}
_RATE_LIMIT_WINDOW = 60
_RATE_LIMIT_MAX_REQUESTS = 60


def _check_rate_limit(client_ip: str) -> bool:
    now = time.time()
    window_start = now - _RATE_LIMIT_WINDOW
    with _rate_limit_lock:
        stale = [k for k, v in _rate_limit_store.items() if not v or (v and v[-1] < window_start)]
        for k in stale:
            del _rate_limit_store[k]
        requests = _rate_limit_store.get(client_ip, [])
        while requests and requests[0] < window_start:
            requests.pop(0)
        if len(requests) >= _RATE_LIMIT_MAX_REQUESTS:
            return False
        requests.append(now)
        _rate_limit_store[client_ip] = requests
    return True


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@lru_cache(maxsize=1)
def _find_cloudflared() -> Optional[str]:
    path = os.path.join(os.path.dirname(__file__), "cloudflared.exe")
    if os.path.isfile(path):
        return path
    for p in ["cloudflared.exe", "cloudflared"]:
        for d in os.environ.get("PATH", "").split(os.pathsep):
            full = os.path.join(d, p)
            if os.path.isfile(full):
                return full
    return None


def _start_tunnel():
    global _tunnel_process, TUNNEL_URL
    cf = _find_cloudflared()
    if not cf:
        print("[!] cloudflared not found - tunnel not started")
        return
    try:
        cmd = [cf, "tunnel", "--url", "http://localhost:8001"]
        print("[*] Starting quick tunnel on port 8001...")
        _tunnel_process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        def _reader():
            global TUNNEL_URL
            pattern = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")
            for line in iter(_tunnel_process.stdout.readline, b""):
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    print(f"[tunnel] {text}")
                m = pattern.search(text)
                if m:
                    TUNNEL_URL = m.group(0)
                    print(f"\n{'='*50}")
                    print(f"  PUBLIC URL: {TUNNEL_URL}")
                    print(f"{'='*50}\n")
        t = threading.Thread(target=_reader, daemon=True)
        t.start()
    except Exception as e:
        print(f"[!] Failed to start cloudflared: {e}")


def _stop_tunnel():
    global _tunnel_process
    if _tunnel_process:
        try:
            _tunnel_process.terminate()
            _tunnel_process.wait(timeout=3)
        except Exception:
            try:
                _tunnel_process.kill()
            except Exception:
                pass
        _tunnel_process = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    _start_tunnel()
    print("[*] T-Blocks Leaderboard started on http://localhost:8001")
    yield
    _stop_tunnel()


app = FastAPI(title="T-Blocks Leaderboard", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/version")
def get_version_endpoint():
    return {"version": get_version()}


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        client_ip = _get_client_ip(request)
        if not _check_rate_limit(client_ip):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
    response = await call_next(request)
    return response


class ScoreRequest(BaseModel):
    name: str
    mode: str
    score: int
    lines: int = 0
    level: int = 1

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return v.strip()[:32] if v.strip() else "Anonymous"

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        return v if v in ("classic", "marathon", "boss", "ultra") else "classic"

    @field_validator("score")
    @classmethod
    def validate_score(cls, v: int) -> int:
        return max(0, v)

    @field_validator("lines", "level")
    @classmethod
    def validate_non_negative(cls, v: int) -> int:
        return max(0, v)


VALID_MODES = ("classic", "marathon", "boss", "ultra")


@app.post("/api/score")
def submit_score(req: ScoreRequest):
    db = SessionLocal()
    try:
        s = Score(name=req.name, mode=req.mode, score=req.score, lines=req.lines, level=req.level)
        db.add(s)
        db.commit()

        position = db.query(func.count(Score.id)).filter(
            Score.mode == req.mode,
            Score.score > req.score,
        ).scalar() + 1

        return {"ok": True, "position": position, "id": s.id}
    finally:
        db.close()


@app.get("/api/leaderboard")
def get_leaderboard(mode: str = "classic", limit: int = Query(50, ge=1, le=100)):
    mode = mode if mode in VALID_MODES else "classic"
    db = SessionLocal()
    try:
        rows = (
            db.query(Score)
            .filter(Score.mode == mode)
            .order_by(Score.score.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "rank": i + 1,
                "name": r.name,
                "score": r.score,
                "lines": r.lines,
                "level": r.level,
                "date": r.created_at.isoformat() if r.created_at else None,
            }
            for i, r in enumerate(rows)
        ]
    finally:
        db.close()


@app.get("/api/leaderboard/position")
def get_position(mode: str, score: int):
    mode = mode if mode in VALID_MODES else "classic"
    score = max(0, score)
    db = SessionLocal()
    try:
        position = db.query(func.count(Score.id)).filter(
            Score.mode == mode,
            Score.score > score,
        ).scalar() + 1
        total = db.query(func.count(Score.id)).filter(Score.mode == mode).scalar()
        return {"position": position, "total": total}
    finally:
        db.close()


@app.get("/api/stats")
def get_stats():
    db = SessionLocal()
    try:
        total_games = db.query(func.count(Score.id)).scalar() or 0
        total_players = db.query(func.count(func.distinct(Score.name))).scalar() or 0
        best_scores = {}
        for mode in VALID_MODES:
            row = db.query(func.max(Score.score)).filter(Score.mode == mode).scalar()
            best_scores[mode] = row or 0
        return {
            "total_games": total_games,
            "total_players": total_players,
            "best_scores": best_scores,
            "uptime_seconds": int(time.time() - HOST_START_TIME),
        }
    finally:
        db.close()


@app.get("/api/tunnel-url")
def get_tunnel_url():
    return {"url": TUNNEL_URL}


# ---- VISITOR COUNTER & ONLINE ----

_visitors_today = 0
_visitors_date = ""
_online_users: dict[str, float] = {}
_ONLINE_TIMEOUT = 60


@app.post("/api/visit")
def record_visit(request: Request, page: str = "/", ip: str = ""):
    global _visitors_today, _visitors_date
    client_ip = ip or _get_client_ip(request)
    page = page[:128]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _visitor_lock:
        if _visitors_date != today:
            _visitors_today = 0
            _visitors_date = today
        _visitors_today += 1
        db = SessionLocal()
        try:
            db.add(VisitorLog(ip=client_ip[:64], page=page))
            db.commit()
        finally:
            db.close()
    return {"ok": True, "visitors_today": _visitors_today}


@app.get("/api/visitors")
def get_visitors():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db = SessionLocal()
    try:
        total = db.query(func.count(VisitorLog.id)).scalar() or 0
        today_count = db.query(func.count(VisitorLog.id)).filter(
            func.date(VisitorLog.timestamp) == today
        ).scalar() or 0
    finally:
        db.close()
    return {"total": total, "today": today_count}


@app.post("/api/online")
def heartbeat_online(request: Request, session_id: str = ""):
    if not session_id:
        session_id = _get_client_ip(request)
    session_id = session_id[:32]
    with _online_lock:
        _online_users[session_id] = time.time()
        _cleanup_online_locked()
    return {"online": len(_online_users)}


def _cleanup_online_locked():
    now = time.time()
    expired = [k for k, v in _online_users.items() if now - v > _ONLINE_TIMEOUT]
    for k in expired:
        del _online_users[k]


@app.get("/api/online")
def get_online():
    with _online_lock:
        _cleanup_online_locked()
        return {"online": len(_online_users)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
