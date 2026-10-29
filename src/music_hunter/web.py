"""Localhost player — SPEC.md §11 (player view), §8 (blind), §4.1 step 8 (early start).

The page never receives a score, breakdown or control flag for a track until it has
been rated. Audio is served by track ID, so a file that moves when it is rated (to
references/, keepers/ or data/undo/) keeps playing.
"""

import contextlib
import io
import json
import threading
import time
from contextlib import asynccontextmanager
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from fastapi.staticfiles import StaticFiles

from . import db, ratings, reweight
from .config import CONFIG, resolve
from .results import results as results_data
from .scoring import pool_tracks

STATIC = Path(__file__).parent / "static"
UI = CONFIG["ui"]
@asynccontextmanager
async def lifespan(app):
    conn = db.connect()
    db.log_event(conn, "server_start", host=UI["host"], port=UI["port"])
    worker.start_if_needed()
    yield
    db.log_event(conn, "server_stop")


app = FastAPI(title="music-hunter", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# --- background pool worker: first pull, unfinished fills and refills (SPEC.md §4.2) -------------

class Worker:
    def __init__(self):
        self.state, self.message = "idle", ""
        self.thread: threading.Thread | None = None

    def start_if_needed(self) -> None:
        if self.state == "running":
            return
        conn = db.connect()
        job = self.job_needed(conn)
        conn.close()
        if job:
            self.state = "running"  # before the thread starts, so the gate never sees a false "idle"
            self.message = "Starting…"
            self.thread = threading.Thread(target=self._run, args=(job,), daemon=True)
            self.thread.start()

    @staticmethod
    def job_needed(conn) -> str | None:
        """'pull' for the first pull or a refill, 'fill' to finish an interrupted fill, else None.

        A refill needs at least one play since the latest pull started, so a pull that
        finds little new can't trigger another straight away.
        """
        from .pool import unrated_pool_size
        from .scoring import score_pool

        pool_cfg = CONFIG["pool"]
        gen = conn.execute("SELECT max(id) FROM generations").fetchone()[0]
        if gen is None:
            return "pull"
        unplayed = unrated_pool_size(conn)
        left = conn.execute("SELECT count(*) FROM candidates WHERE generation = ? AND status = 'candidate'",
                            (gen,)).fetchone()[0]
        if left and unplayed < pool_cfg["target_size"]:
            return "fill"
        played_since = conn.execute("SELECT count(*) FROM plays WHERE generation = ?", (gen,)).fetchone()[0]
        if not played_since:
            return None
        if unplayed < pool_cfg["refill_below"]:
            return "pull"
        if pool_cfg["refill_min_best_score"] > 0:
            scored = score_pool(conn)
            if scored and scored[0]["score"] < pool_cfg["refill_min_best_score"]:
                return "pull"
        return None

    def _run(self, job: str) -> None:
        conn = db.connect()
        started = time.monotonic()
        db.log_event(conn, "job_start", job=job)
        try:
            with contextlib.redirect_stdout(LogWriter()):   # pull/fill progress lines go to the log file
                self._job(conn, job)
            db.log_event(conn, "job_done", job=job, seconds=round(time.monotonic() - started, 1))
        except Exception as e:
            traceback.print_exc()
            self.state, self.message = "error", f"{type(e).__name__}: {e}"
            db.log_event(conn, "job_error", job=job, error=self.message, traceback=traceback.format_exc(),
                         seconds=round(time.monotonic() - started, 1))

    def _job(self, conn, job: str) -> None:
        from .pool import fill
        from .pull import Pull, match_seeds
        from .sources import LastFM, YouTubeMusic

        if job == "pull":
            first = conn.execute("SELECT count(*) FROM generations").fetchone()[0] == 0
            self.message = ("Finding candidates" if first else "Refilling the pool: finding candidates") + \
                " (Last.fm and YouTube Music)…"
            yt = YouTubeMusic()
            gen = Pull(conn, yt, LastFM()).run(match_seeds(conn, yt))
        else:
            gen = conn.execute("SELECT max(id) FROM generations").fetchone()[0]
        self.message = "Downloading and analysing tracks…"
        fill(conn, gen)
        self.state, self.message = "idle", ""


class LogWriter(io.TextIOBase):
    """File-like object that sends printed lines to the log file."""

    def write(self, text: str) -> int:
        for line in text.splitlines():
            if line.strip():
                db.log.info("worker: %s", line.rstrip())
        return len(text)


worker = Worker()




# --- API -----------------------------------------------------------------------------------

class RateBody(BaseModel):
    play_id: int
    rating: int
    played_seconds: float | None = None


class SkipBody(BaseModel):
    play_id: int
    played_seconds: float | None = None


class ChangeBody(BaseModel):
    rating: int


class ClientEvent(BaseModel):
    kind: str
    play_id: int | None = None
    data: dict = {}


CLIENT_EVENTS = {"playing", "stall", "audio_error", "ended", "page_error"}


def track_info(conn, play) -> dict:
    """What the player may know before rating: no score, no sources, no control flag."""
    t = conn.execute("SELECT * FROM tracks WHERE id = ?", (play["track_id"],)).fetchone()
    return {"play_id": play["id"], "track_id": t["id"], "artist": t["artist"], "title": t["title"],
            "duration_s": t["duration_s"], "gain_db": t["gain_db"] or 0.0, "audio": f"/audio/{t['id']}"}


def reveal(conn, play_id: int) -> dict:
    """Everything about a rated play, for display after rating."""
    play = conn.execute("SELECT * FROM plays WHERE id = ?", (play_id,)).fetchone()
    weights = json.loads(conn.execute("SELECT weights FROM weight_sets WHERE id = ?",
                                      (play["weight_set_id"],)).fetchone()[0])
    components = json.loads(play["components"])
    return {
        **track_info(conn, play),
        "rating": play["rating"], "control": bool(play["is_control"]),
        "score": play["score"], "rank": play["rank"], "pool_size": play["pool_size"],
        "components": [{"name": c, "norm": v["norm"], "raw": v["raw"], "weight": weights.get(c, 0.0),
                        "contribution": weights.get(c, 0.0) * v["norm"]} for c, v in components.items()],
        "provenance": json.loads(play["provenance"]),
    }


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text()


@app.get("/api/state")
def state() -> dict:
    conn = db.connect()
    unplayed = len(pool_tracks(conn))
    plays = conn.execute("SELECT count(*), count(rating) FROM plays").fetchone()
    first_pull = plays[0] == 0
    ready = unplayed > 0 and (not first_pull or unplayed >= UI["early_start_tracks"] or worker.state != "running")
    last = ratings.last_rated(conn)
    return {
        "ready": ready, "unplayed": unplayed, "early_start": UI["early_start_tracks"],
        "plays": plays[0], "rated": plays[1],
        "worker": {"state": worker.state, "message": worker.message},
        "last": reveal(conn, last["id"]) if last else None,
    }


@app.post("/api/next")
def next_track() -> dict:
    """The play awaiting a rating, or a new one: top-scored, or median-scored for a control (§8)."""
    conn = db.connect()
    play = ratings.current_play(conn)
    if play is None:
        if not pool_tracks(conn):
            worker.start_if_needed()
            raise HTTPException(409, "No unplayed tracks in the pool")
        play_id = ratings.show(conn, control=ratings.next_is_control(conn))
        play = conn.execute("SELECT * FROM plays WHERE id = ?", (play_id,)).fetchone()
    return track_info(conn, play)


@app.post("/api/rate")
def rate(body: RateBody) -> dict:
    conn = db.connect()
    try:
        ratings.rate(conn, body.play_id, body.rating, body.played_seconds)
    except ValueError as e:
        raise HTTPException(400, str(e))
    reweight.after_rating(conn)
    worker.start_if_needed()
    return reveal(conn, body.play_id)


@app.post("/api/skip")
def skip(body: SkipBody) -> dict:
    ratings.skip(db.connect(), body.play_id, body.played_seconds)
    worker.start_if_needed()
    return {"ok": True}


@app.post("/api/change")
def change(body: ChangeBody) -> dict:
    conn = db.connect()
    try:
        play_id = ratings.change_last_rating(conn, body.rating)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return reveal(conn, play_id)


@app.post("/api/event")
def client_event(body: ClientEvent) -> dict:
    """Player-side events: start latency, stalls, audio errors, track ends, script errors."""
    if body.kind not in CLIENT_EVENTS:
        raise HTTPException(400, f"Unknown event kind {body.kind!r}")
    db.log_event(db.connect(), f"player_{body.kind}", body.play_id, **body.data)
    return {"ok": True}


@app.get("/results", response_class=HTMLResponse)
def results_page() -> str:
    return (STATIC / "results.html").read_text()


@app.get("/api/results")
def results() -> dict:
    return results_data(db.connect())


@app.get("/audio/{track_id}")
def audio(track_id: int) -> FileResponse:
    conn = db.connect()
    t = conn.execute("SELECT file_path FROM tracks WHERE id = ?", (track_id,)).fetchone()
    held = conn.execute("SELECT undo_path FROM plays WHERE track_id = ? AND undo_path IS NOT NULL",
                        (track_id,)).fetchone()
    rel = (t and t["file_path"]) or (held and held["undo_path"])
    if not rel or not resolve(rel).exists():
        raise HTTPException(404, "Audio not available")
    return FileResponse(resolve(rel))


def serve() -> None:
    import uvicorn

    db.setup_logging()
    print(f"Player: http://{UI['host']}:{UI['port']}")
    print(f"Results: http://{UI['host']}:{UI['port']}/results")
    print("Log: data/music-hunter.log")
    uvicorn.run(app, host=UI["host"], port=UI["port"], log_level="warning")
