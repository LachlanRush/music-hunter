"""Localhost player — SPEC.md §15.3 (blind), §15.9 (player), §4.1 step 8 (early start).

The player never receives a score, rank, breakdown, sources or control flag, before or
after rating (v2). Artist, title and artwork arrive only once a track is rated. Audio is
served by track ID, so a file that moves when it is rated (to collection/ or data/undo/)
keeps playing.
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

from . import db, ratings, reweight, veto
from .config import CONFIG, resolve
from .results import results as results_data
from .scoring import pool_tracks

STATIC = Path(__file__).parent / "static"
UI = CONFIG["ui"]
@asynccontextmanager
async def lifespan(app):
    conn = db.connect()
    db.log_event(conn, "server_start", host=UI["host"], port=UI["port"],
                 hide_names_until_rated=UI["hide_names_until_rated"])
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
        """The background job due now, if any (SPEC.md §17.2, §17.8):
        'pull' — the first pull, or a refill: 5 seeds not yet pulled for, or at least one after refill_min_plays plays,
                 or the playable pool short with no candidates left;
        'fill' — top the playable pool up to target_size from the latest pull's remaining candidates;
        'restore' — download audio again for vetoed tracks that became playable.
        """
        from .pool import missing_audio, unpulled_seeds, unrated_pool_size

        pool_cfg = CONFIG["pool"]
        if not conn.execute("SELECT count(*) FROM tracks WHERE origin = 'original_reference'").fetchone()[0]:
            return None                       # nothing to pull from until seeds are added
        gen = conn.execute("SELECT max(id) FROM generations").fetchone()[0]
        if gen is None:
            return "pull"
        playable = unrated_pool_size(conn)
        left = conn.execute("SELECT count(*) FROM candidates WHERE generation = ?"
                            " AND status IN ('candidate', 'not_selected')",
                            (gen,)).fetchone()[0]
        played_since = conn.execute("SELECT count(*) FROM plays WHERE generation = ?", (gen,)).fetchone()[0]
        waiting = unpulled_seeds(conn)
        if played_since and (len(waiting) >= pool_cfg["refill_new_seeds"]
                             or (waiting and played_since >= pool_cfg["refill_min_plays"])):
            return "pull"                     # placeholder Add-seed rule (§17.8, to be discussed); before top-ups,
                                              # which are due after almost every rating and would starve it
        if left and playable < pool_cfg["target_size"]:
            return "fill"
        if missing_audio(conn):
            return "restore"
        if played_since and playable < pool_cfg["refill_below"]:
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
        from .pool import fill, missing_audio, restore_audio
        from .pull import Pull, match_seeds, optional
        from .sources import Deezer, LastFM, Tidal, YouTubeMusic

        if job == "restore":
            todo = missing_audio(conn)
            for i, tid in enumerate(todo, 1):
                self.message = f"Bringing back {i} of {len(todo)} tracks that passed the veto again…"
                restore_audio(conn, tid)
                time.sleep(CONFIG["pool"]["download_delay_seconds"])
            self.state, self.message = "idle", ""
            return
        if job == "pull":
            first = conn.execute("SELECT count(*) FROM generations").fetchone()[0] == 0
            head = "Finding candidates" if first else "Refilling the pool: finding candidates"
            self.message = f"{head} (Last.fm, YouTube Music, Deezer and TIDAL)…"
            yt = YouTubeMusic()
            gen = Pull(conn, yt, LastFM(), Deezer(), optional(Tidal)).run(
                match_seeds(conn, yt), progress=lambda text: setattr(self, "message", f"{head}: {text}…"))
        else:
            gen = conn.execute("SELECT max(id) FROM generations").fetchone()[0]
        self.message = "Downloading and analysing tracks…"
        fill(conn, gen, progress=lambda text: setattr(self, "message", f"Downloading and analysing: {text}"))
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
    played_bpm: float | None = None        # tempo it was played at (target-BPM playback)
    playback_rate: float | None = None


class SettingsBody(BaseModel):
    target_bpm: float | None = None        # None: use the default (median of liked tracks)
    match_tempo: bool = False              # play tracks stretched to the target BPM


class SkipBody(BaseModel):
    play_id: int
    played_seconds: float | None = None


class ChangeBody(BaseModel):
    rating: int


class ClientEvent(BaseModel):
    kind: str
    play_id: int | None = None
    data: dict = {}


CLIENT_EVENTS = {"playing", "stall", "audio_error", "ended", "page_error", "back", "forward"}


def track_info(conn, play) -> dict:
    """Everything the player is ever told about a play (SPEC.md §15.3).

    Never a score, rank, breakdown, source or control flag. Artist, title and artwork only
    once the play is rated (when [ui] hide_names_until_rated is on).
    """
    t = conn.execute("SELECT * FROM tracks WHERE id = ?", (play["track_id"],)).fetchone()
    hidden = UI["hide_names_until_rated"] and play["rating"] is None
    return {"play_id": play["id"], "track_id": t["id"],
            "artist": None if hidden else t["artist"], "title": None if hidden else t["title"],
            "art": None if hidden or not t["art_path"] else f"/art/{t['id']}",
            "rating": play["rating"], "duration_s": t["duration_s"], "gain_db": t["gain_db"] or 0.0,
            "bpm": t["bpm"],
            "audio": f"/audio/{t['id']}"}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text()


@app.get("/api/state")
def state() -> dict:
    conn = db.connect()
    unplayed = len(ratings.playable_ids(conn))          # v4: playable tracks with audio ready
    plays = conn.execute("SELECT count(*), count(rating) FROM plays").fetchone()
    first_pull = plays[0] == 0
    complete = ratings.run_complete(conn)
    ready = not complete and unplayed > 0 and \
        (not first_pull or unplayed >= UI["early_start_tracks"] or worker.state != "running")
    last = ratings.last_rated(conn)
    return {
        "ready": ready, "run_complete": complete, "unplayed": unplayed, "early_start": UI["early_start_tracks"],
        "pool_target": CONFIG["pool"]["target_size"],
        "plays": plays[0], "rated": plays[1],
        "worker": {"state": worker.state, "message": worker.message},
        "last": track_info(conn, last) if last else None,
        "labels": ratings.LABELS, "hints": CONFIG["ratings"]["hints"], "add": ratings.ADD,
        "play_to_end": UI["play_to_end"], "preload_next": UI["preload_next"], "stall_log_ms": UI["stall_log_ms"],
        "seeds": conn.execute("SELECT count(*) FROM tracks WHERE origin = 'original_reference'").fetchone()[0],
    }


@app.post("/api/next")
def next_track() -> dict:
    """The play awaiting a rating, or a new one: a random playable track, or (a control) a random vetoed one."""
    from .pool import restore_audio

    conn = db.connect()
    play = ratings.current_play(conn)
    if play is None:
        if ratings.run_complete(conn):
            raise HTTPException(409, "Run complete")
        track_id = ratings.upcoming(conn)
        if track_id is None:
            worker.start_if_needed()
            raise HTTPException(409, "No playable tracks in the pool")
        if not restore_audio(conn, track_id):          # an injected vetoed track's audio, if not fetched ahead
            db.set_setting(conn, "next_draw", None, log=False)
            raise HTTPException(503, "Couldn't fetch the next track's audio; try again")
        play_id = ratings.show_next(conn)
        play = conn.execute("SELECT * FROM plays WHERE id = ?", (play_id,)).fetchone()
    return track_info(conn, play)


@app.get("/api/upcoming")
def upcoming() -> dict:
    """The track most likely to play next (never names or anything else), for fetching its audio early."""
    conn = db.connect()
    if not UI["preload_next"] or ratings.run_complete(conn):
        raise HTTPException(404, "Nothing to preload")
    track_id = ratings.upcoming(conn)
    if track_id is None:
        raise HTTPException(404, "No unplayed tracks")
    if not conn.execute("SELECT file_path FROM tracks WHERE id = ?", (track_id,)).fetchone()[0]:
        _fetch_in_background(track_id)                 # an injected vetoed track: audio fetched just in time
        raise HTTPException(404, "Audio still being fetched")
    return {"track_id": track_id, "audio": f"/audio/{track_id}"}


_fetching: set[int] = set()


def _fetch_in_background(track_id: int) -> None:
    from .pool import restore_audio

    if track_id in _fetching:
        return
    _fetching.add(track_id)

    def run():
        try:
            restore_audio(db.connect(), track_id)
        finally:
            _fetching.discard(track_id)
    threading.Thread(target=run, daemon=True).start()


@app.get("/api/previous")
def previous(exclude: int | None = None) -> dict:
    """The track the back button returns to (`exclude`: the track on screen, if already rated).

    can_change: its rating can still be changed, or given late if it was skipped. A skipped
    track stays blind (no names) because it has not been rated.
    """
    conn = db.connect()
    play = ratings.previous_play(conn, exclude)
    if play is None:
        raise HTTPException(404, "No previous track yet")
    latest = ratings.previous_play(conn)
    can_change = latest is not None and latest["id"] == play["id"]
    return {**track_info(conn, play), "can_change": can_change}


@app.post("/api/rate")
def rate(body: RateBody) -> dict:
    conn = db.connect()
    try:
        ratings.rate(conn, body.play_id, body.rating, body.played_seconds, body.played_bpm, body.playback_rate)
    except ValueError as e:
        raise HTTPException(400, str(e))
    reweight.after_rating(conn)
    veto.update(conn)                         # likes changed: re-score, move tracks across the line
    worker.start_if_needed()
    return track_info(conn, conn.execute("SELECT * FROM plays WHERE id = ?", (body.play_id,)).fetchone())


@app.post("/api/skip")
def skip(body: SkipBody) -> dict:
    """Skipped, or ended without a rating: counts as [ratings] unrated_counts_as (Close), marked automatic.
    With 0, the old rule: the track counts as shown and teaches nothing."""
    conn = db.connect()
    auto = CONFIG["ratings"]["unrated_counts_as"]
    if not auto:
        ratings.skip(conn, body.play_id, body.played_seconds)
        worker.start_if_needed()
        return {"ok": True, "rated": False}
    play = conn.execute("SELECT * FROM plays WHERE id = ?", (body.play_id,)).fetchone()
    if play is None:
        raise HTTPException(404, "No such play")
    if play["rating"] is None:
        try:
            ratings.rate(conn, body.play_id, auto, body.played_seconds, auto=True)
        except ValueError as e:
            raise HTTPException(400, str(e))
        db.log_event(conn, "auto_rated", body.play_id, rating=auto)
        reweight.after_rating(conn)
        veto.update(conn)                     # likes changed: re-score, move tracks across the line
    worker.start_if_needed()
    return {**track_info(conn, conn.execute("SELECT * FROM plays WHERE id = ?", (body.play_id,)).fetchone()),
            "rated": True}


@app.post("/api/change")
def change(body: ChangeBody) -> dict:
    conn = db.connect()
    try:
        play_id = ratings.change_last_rating(conn, body.rating)
    except ValueError as e:
        raise HTTPException(400, str(e))
    veto.update(conn)
    worker.start_if_needed()
    return track_info(conn, conn.execute("SELECT * FROM plays WHERE id = ?", (play_id,)).fetchone())


@app.get("/api/settings")
def get_settings() -> dict:
    from .scoring import target_bpm, tempo_settings

    conn = db.connect()
    target, source = target_bpm(conn)
    return {**tempo_settings(conn), "effective_target_bpm": target, "target_source": source,
            "max_stretch": CONFIG["bpm"]["max_stretch"]}


@app.post("/api/settings")
def set_settings(body: SettingsBody) -> dict:
    """Target BPM steers ranking (bpm_match) and, with match_tempo, playback speed. Changes are logged.

    Refused while [tempo] locked is on (held steady for the evaluation run; change them in config.toml)."""
    if CONFIG["tempo"]["locked"]:
        raise HTTPException(409, "Tempo settings are locked for this run (config.toml [tempo])")
    conn = db.connect()
    if body.target_bpm is not None and not (40 <= body.target_bpm <= 220):
        raise HTTPException(400, "Target BPM must be between 40 and 220")
    if db.get_setting(conn, "target_bpm") != body.target_bpm:
        db.set_setting(conn, "target_bpm", body.target_bpm)
    if db.get_setting(conn, "match_tempo", False) != body.match_tempo:
        db.set_setting(conn, "match_tempo", body.match_tempo)
    return get_settings()


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


@app.get("/art/{track_id}")
def art(track_id: int) -> FileResponse:
    """Cover art, only for rated tracks (blind until then)."""
    conn = db.connect()
    t = conn.execute("SELECT art_path, rating FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if not t or not t["art_path"] or (UI["hide_names_until_rated"] and t["rating"] is None):
        raise HTTPException(404, "No artwork")
    if not resolve(t["art_path"]).exists():
        raise HTTPException(404, "No artwork")
    return FileResponse(resolve(t["art_path"]))


def serve() -> None:
    """Listens on [ui] host and any extra_hosts (e.g. the Tailscale address, for the phone)."""
    import socket

    import uvicorn

    db.setup_logging()
    sockets = []
    for host in [UI["host"], *UI.get("extra_hosts", [])]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, UI["port"]))
        except OSError as e:                       # e.g. Tailscale not running: carry on without that address
            print(f"Can't listen on {host}:{UI['port']} ({e}); skipping it")
            sock.close()
            continue
        sockets.append(sock)
        print(f"Player: http://{host}:{UI['port']}   Results: http://{host}:{UI['port']}/results")
    print("Log: data/music-hunter.log")
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=sockets)
