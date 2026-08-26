import os
import time
import json
import subprocess
import threading
import re
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, DateTime, func
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, timezone
from contextlib import asynccontextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "leaderboard.db")
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class Score(Base):
    __tablename__ = "scores"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(32), nullable=False)
    mode = Column(String(16), nullable=False, index=True)
    score = Column(Integer, nullable=False)
    lines = Column(Integer, default=0)
    level = Column(Integer, default=1)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


Base.metadata.create_all(bind=engine)


TUNNEL_URL: Optional[str] = None
_tunnel_process: Optional[subprocess.Popen] = None
HOST_START_TIME = time.time()


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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ScoreRequest(BaseModel):
    name: str
    mode: str
    score: int
    lines: int = 0
    level: int = 1


@app.post("/api/score")
def submit_score(req: ScoreRequest):
    name = req.name.strip()[:32] if req.name.strip() else "Anonymous"
    mode = req.mode if req.mode in ("classic", "marathon", "boss", "ultra") else "classic"
    score = max(0, req.score)

    db = SessionLocal()
    try:
        s = Score(name=name, mode=mode, score=score, lines=req.lines, level=req.level)
        db.add(s)
        db.commit()

        position = db.query(func.count(Score.id)).filter(
            Score.mode == mode,
            Score.score > score,
        ).scalar() + 1

        return {"ok": True, "position": position, "id": s.id}
    finally:
        db.close()


@app.get("/api/leaderboard")
def get_leaderboard(mode: str = "classic", limit: int = Query(50, le=100)):
    mode = mode if mode in ("classic", "marathon", "boss", "ultra") else "classic"
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
    mode = mode if mode in ("classic", "marathon", "boss", "ultra") else "classic"
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
        for mode in ("classic", "marathon", "boss", "ultra"):
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

_visitor_db_lock = threading.Lock()
_visitors_today = 0
_visitors_date = ""
_online_users = {}
_ONLINE_TIMEOUT = 60

def _get_visitor_db():
    path = os.path.join(os.path.dirname(__file__), "visitors.db")
    eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base2 = declarative_base()
    class VisitorLog(Base2):
        __tablename__ = "visitors"
        id = Column(Integer, primary_key=True, index=True)
        ip = Column(String(64))
        page = Column(String(128))
        timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    Base2.metadata.create_all(bind=eng)
    return sessionmaker(bind=eng), VisitorLog

_visitor_session, VisitorLog = _get_visitor_db()


@app.post("/api/visit")
def record_visit(page: str = "/", ip: str = ""):
    global _visitors_today, _visitors_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _visitor_db_lock:
        if _visitors_date != today:
            _visitors_today = 0
            _visitors_date = today
        _visitors_today += 1
        db = _visitor_session()
        try:
            db.add(VisitorLog(ip=ip[:64], page=page[:128]))
            db.commit()
        finally:
            db.close()
    return {"ok": True, "visitors_today": _visitors_today}


@app.get("/api/visitors")
def get_visitors():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db = _visitor_session()
    try:
        total = db.query(func.count(VisitorLog.id)).scalar() or 0
        today_count = db.query(func.count(VisitorLog.id)).filter(
            func.date(VisitorLog.timestamp) == today
        ).scalar() or 0
    finally:
        db.close()
    return {"total": total, "today": today_count}


@app.post("/api/online")
def heartbeat_online(session_id: str = ""):
    _online_users[session_id[:32]] = time.time()
    _cleanup_online()
    return {"online": len(_online_users)}


def _cleanup_online():
    now = time.time()
    expired = [k for k, v in _online_users.items() if now - v > _ONLINE_TIMEOUT]
    for k in expired:
        del _online_users[k]


@app.get("/api/online")
def get_online():
    _cleanup_online()
    return {"online": len(_online_users)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
