"""Localhost player — SPEC.md §15.3 (blind), §15.9 (player), §4.1 step 8 (early start).

The player never receives a score, rank, breakdown, sources or control flag, before or
after rating (v2). Artist, title and artwork arrive only once a track is rated. Audio is
served by track ID, so a file that moves when it is rated (to collection/ or data/undo/)
keeps playing.
"""

import contextlib
import io
import json
import subprocess
import threading
import time
from contextlib import asynccontextmanager
import traceback
from pathlib import Path

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from fastapi.staticfiles import StaticFiles

from . import analysis, db, find, jobs, library, ratings, reweight, sync, veto
from .config import CONFIG, path, resolve
from .results import results as results_data
from .scoring import pool_tracks

STATIC = Path(__file__).parent / "static"
UI = CONFIG["ui"]
@asynccontextmanager
async def lifespan(app):
    conn = db.connect()
    db.log_event(conn, "server_start", host=UI["host"], port=UI["port"],
                 hide_names_until_rated=UI["hide_names_until_rated"])
    analysis.drop_unfinished(conn)                  # v5: nobody waits for tracks queued for the PC before a restart
    worker.start_if_needed()
    for p in library.list_playlists(conn):          # v5: resume each started session's background work
        if find.started(p["id"]):
            token = db.SESSION.set(p["id"])         # re-score under the current rules (a playlist not listened to
            try:                                    # since an update would otherwise keep its old state)
                veto.update(db.connect(p["id"]))
            except Exception as e:  # noqa: BLE001 — never block start-up
                db.log_event(conn, "startup_rescore_failed", playlist_id=p["id"], error=str(e)[:200])
            finally:
                db.SESSION.reset(token)
            session_worker(p["id"]).start_if_needed()
    jobs.requeue_interrupted(conn)                  # v5: a job cut off by a restart runs again (jobs are re-runnable)
    threading.Thread(target=_maintenance, daemon=True, name="maintenance").start()
    job_runner.kick()
    from . import tempo
    threading.Thread(target=tempo.sync_all, daemon=True).start()   # Same BPM copies for every playlist
    yield
    db.log_event(conn, "server_stop")


app = FastAPI(title="music-hunter", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# --- background pool worker: first pull, unfinished fills and refills (SPEC.md §4.2) -------------

class Worker:
    """Background pulls, top-ups and audio restores for one Find music session (v5: one per playlist; None = the
    single database of earlier versions)."""

    def __init__(self, playlist_id: int | None = None):
        self.pid = playlist_id
        self.state, self.message = "idle", ""
        self.thread: threading.Thread | None = None

    def start_if_needed(self) -> None:
        if self.state == "running":
            return
        conn = db.connect(self.pid)
        job = self.job_needed(conn)
        conn.close()
        if job:
            self.state = "running"  # before the thread starts, so the gate never sees a false "idle"
            self.message = "Starting…"
            self.thread = threading.Thread(target=self._in_session, args=(job,), daemon=True)
            self.thread.start()

    def _in_session(self, job: str) -> None:
        db.SESSION.set(self.pid)                    # threads don't inherit the request's session
        self._run(job)

    @staticmethod
    def job_needed(conn) -> str | None:
        """The background job due now, if any (SPEC.md §17.2, §17.8):
        'pull' — the first pull, or a refill: 5 seeds not yet pulled for, or at least one after refill_min_plays plays,
                 or the playable pool short with no candidates left;
        'fill' — top the playable pool up to target_size from every pull's remaining candidates;
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
        left = conn.execute("SELECT count(*) FROM candidates WHERE status IN ('candidate', 'not_selected')"
                            ).fetchone()[0]          # any pull's leftovers (pool.buckets)
        played_since = conn.execute("SELECT count(*) FROM plays WHERE generation = ?", (gen,)).fetchone()[0]
        waiting = unpulled_seeds(conn)
        from .pull import is_v5_session
        if waiting and is_v5_session(conn):
            return "pull_new"                 # v5 (§18.3): new Adds are pulled for once, now (stars changed in the
                                              # playlist are carried over at the next pull)
        if played_since and (len(waiting) >= pool_cfg["refill_new_seeds"]
                             or (waiting and played_since >= pool_cfg["refill_min_plays"])):
            return "pull"                     # placeholder Add-seed rule (§17.8, to be discussed); before top-ups,
                                              # which are due after almost every rating and would starve it
        if left and playable < pool_cfg["target_size"]:
            return "fill"
        from .analysis import youtube_paused
        if missing_audio(conn) and not youtube_paused():
            return "restore"
        if played_since and playable < pool_cfg["refill_below"]:
            return "pull"
        return None

    def _run(self, job: str) -> None:
        if not HEAVY.acquire(blocking=False):         # one session's downloads at a time (paced, no bursts)
            self.message = "Waiting for another playlist's downloads to finish…"
            HEAVY.acquire()
        try:
            self._run_locked(job)
        finally:
            HEAVY.release()

    def _run_locked(self, job: str) -> None:
        conn = db.connect(self.pid)
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
        if job in ("pull", "pull_new"):
            if self.pid is not None:              # v5: carry over stars changed in the playlist since the last pull
                find.sync_seeds(db.connect_library(), conn, self.pid)
            first = conn.execute("SELECT count(*) FROM generations").fetchone()[0] == 0
            head = ("Finding candidates" if first else "Finding candidates for new seeds" if job == "pull_new"
                    else "Refilling the pool: finding candidates")
            self.message = f"{head} (Last.fm, YouTube Music, Deezer and TIDAL)…"
            yt = YouTubeMusic()
            seed_list = match_seeds(conn, yt)
            if job == "pull_new":
                from .pool import unpulled_seeds
                waiting = set(unpulled_seeds(conn))
                seed_list = [s for s in seed_list if s.track_id in waiting]
            gen = Pull(conn, yt, LastFM(), Deezer(), optional(Tidal)).run(
                seed_list, progress=lambda text: setattr(self, "message", f"{head}: {text}…"))
        else:
            gen = conn.execute("SELECT max(id) FROM generations").fetchone()[0]
        self.message = "Downloading and analysing tracks…"
        fill(conn, gen, progress=lambda text: setattr(self, "message", f"Downloading and analysing: {text}"))
        self.state, self.message = "idle", ""


HEAVY = threading.Lock()
worker = Worker(None)                       # the single session of earlier versions (and tests)
workers: dict[int | None, Worker] = {None: worker}


def session_worker(playlist_id=None) -> Worker:
    """The worker for a playlist's session (default: the current request's session)."""
    pid = db.SESSION.get() if playlist_id is None else playlist_id
    if pid not in workers:
        workers[pid] = Worker(pid)
    return workers[pid]


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


class TagsBody(BaseModel):
    play_id: int
    tags: list[str] = []


class ClientEvent(BaseModel):
    kind: str
    play_id: int | None = None
    data: dict = {}


CLIENT_EVENTS = {"playing", "stall", "audio_error", "ended", "page_error", "back", "forward", "restart", "autoplay_stuck"}


def _to_playlist(conn, play_id: int) -> None:
    """v5: an Add joins the playlist's Added section; a rating changed away from Add takes it out again."""
    pid = db.SESSION.get()
    if pid is not None:
        lib = db.connect_library()
        find.sync_add(lib, conn, pid, play_id)
        _sync_tempo(lib, pid)                      # an Added track gets the playlist's Same BPM copy too
        from . import tempo
        tempo.sync_session(conn, pid)              # Find music: drop copies of tracks played a while ago


def _sync_tempo(lib, playlist_id: int) -> None:
    """Keep a playlist's Same BPM copies in step with its tracks (never fails a request)."""
    from . import tempo

    try:
        tempo.sync_playlist(lib, playlist_id)
    except Exception as e:  # noqa: BLE001 — playback extras must not break rating or editing
        db.log_event(lib, "tempo_sync_failed", playlist_id=playlist_id, error=str(e)[:200])


def _base() -> str:
    """URL prefix of the current session's listening endpoints ('' for the single database of earlier versions)."""
    pid = db.SESSION.get()
    return "" if pid is None else f"/find/{pid}"


def _find_rate(conn, bpm) -> dict:
    """Same BPM in Find music: the rate this track plays at, a short note, and its tempo as shown (the reading
    nearest the playlist's typical tempo; shown even while the track is hidden — it doesn't say which track it is)."""
    from . import tempo

    if db.SESSION.get() is None:
        return {"rate": 1.0, "tempo_note": "", "copy_rate": 1.0, "bpm_shown": round(bpm) if bpm else None}
    s = tempo.session_settings(conn)
    rate, note = tempo.rate_for(bpm, s)
    return {"rate": rate, "tempo_note": note, "copy_rate": tempo.rate_for(bpm, s, ignore_off=True)[0],
            "bpm_shown": tempo.shown_bpm(bpm, s["target"])}


def track_info(conn, play) -> dict:
    """Everything the player is ever told about a play (SPEC.md §15.3).

    Never a score, rank, breakdown, source or control flag. Artist, title and artwork only
    once the play is rated (when [ui] hide_names_until_rated is on).
    """
    t = conn.execute("SELECT * FROM tracks WHERE id = ?", (play["track_id"],)).fetchone()
    hidden = UI["hide_names_until_rated"] and play["rating"] is None
    return {"play_id": play["id"], "track_id": t["id"],
            "artist": None if hidden else t["artist"], "title": None if hidden else t["title"],
            "art": None if hidden or not t["art_path"] else f"{_base()}/art/{t['id']}",
            "rating": play["rating"], "duration_s": t["duration_s"], "gain_db": t["gain_db"] or 0.0,
            "bpm": t["bpm"], **_find_rate(conn, t["bpm"]),
            "tags": json.loads(play["tags"] or "[]") if play["rating"] is not None else [],
            "tag_options": ratings.tag_options(play["rating"]),
            "audio": f"{_base()}/audio/{t['id']}"}


def _page(name: str, request: Request | None = None) -> HTMLResponse:
    """A page, with its /static/ files' addresses carrying their version: a browser kept an old style.css (no cache
    rules were sent, so it guessed) under a new page, and every button looked broken on the computer (2026-11-11)."""
    import re

    def versioned(m):
        f = STATIC / m.group(2)
        return f'{m.group(1)}="/static/{m.group(2)}?v={f.stat().st_mtime_ns if f.exists() else 0}"'
    # In a browser, a page opened directly (not in a frame) gets the shell, which shows it in a frame and keeps the
    # playing page alive while I move around (static/shell.js); the phone app is its own shell.
    if request is not None and request.headers.get("sec-fetch-dest") == "document" \
            and "MusicHunterApp" not in request.headers.get("user-agent", ""):
        name = "shell.html"
    html = re.sub(r'(src|href)="/static/([^"?]+)"', versioned, (STATIC / name).read_text())
    return HTMLResponse(html, headers={"Cache-Control": "no-cache", "Vary": "Sec-Fetch-Dest, User-Agent"})


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> str:
    """v5: the playlist library (SPEC.md §18.1)."""
    return _page("home.html", request)


@app.get("/playlist/{playlist_id}", response_class=HTMLResponse)
def playlist_page(playlist_id: int, request: Request) -> str:
    return _page("playlist.html", request)


@app.get("/listen", response_class=HTMLResponse)
def index(request: Request) -> str:
    """The listening interface (Find music; per playlist from milestone 2)."""
    return _page("index.html", request)


# --- v5: playlists and the library (SPEC.md §18.1) -------------------------------------------

job_runner = jobs.Runner()


class NewPlaylist(BaseModel):
    name: str | None = None
    spotify_url: str | None = None


class TempoBody(BaseModel):
    on: bool
    bpm: float | None = None


def _tempo_view(s: dict) -> dict:
    return {"on": s["on"], "bpm": s["bpm"], "target": s["target"], "auto": s.get("auto"),
            "max_stretch": CONFIG["bpm"]["max_stretch"]}


class StarsBody(BaseModel):
    stars: int | None = None


class AddTrackBody(BaseModel):
    track_id: int


class RenameBody(BaseModel):
    name: str | None = None
    description: str | None = None


class FolderBody(BaseModel):
    name: str


class MoveBody(BaseModel):
    folder_id: int | None = None


class PlayFilterBody(BaseModel):
    min_stars: int = 0


def _playlist_or_404(conn, playlist_id: int):
    p = conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    if p is None:
        raise HTTPException(404, "No such playlist")
    return p


def _job_view(job: dict | None) -> dict | None:
    return None if job is None else {"id": job["id"], "kind": job["kind"], "state": job["state"],
                                     "progress": job["progress"], "error": job["error"],
                                     "payload_link": job["payload"].get("link")}


class FeedbackBody(BaseModel):
    text: str
    page: str | None = None
    context: dict = {}


@app.post("/api/feedback")
def send_feedback(body: FeedbackBody) -> dict:
    """A note from the feedback box (every page), kept in the library database for later review."""
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Nothing to send")
    lib = db.connect_library()
    with lib:
        fid = lib.execute("INSERT INTO feedback (page, context, text) VALUES (?, ?, ?)",
                          (body.page, json.dumps(body.context), text[:5000])).lastrowid
    db.log_event(lib, "feedback", feedback_id=fid, page=body.page, text=text[:500])
    return {"ok": True, "id": fid}


@app.get("/playlist/{playlist_id}/results", response_class=HTMLResponse)
def overview_page(playlist_id: int, request: Request) -> str:
    """A playlist's Find music results: Off and Add by score (SPEC.md §18.30)."""
    return _page("overview.html", request)


@app.get("/api/playlists/{playlist_id}/results")
def overview_data(playlist_id: int) -> dict:
    from .overview import overview
    return overview(playlist_id)


@app.get("/api/playlists")
def get_playlists() -> list[dict]:
    conn = db.connect()
    return [{**p, "job": _job_view(jobs.latest_for(conn, p["id"]))} for p in library.list_playlists(conn)]


# --- folders (2026-11-11): one level of grouping on the home page ---------------------------------------------------

@app.get("/api/folders")
def get_folders() -> list[dict]:
    return library.list_folders(db.connect())


@app.post("/api/folders")
def new_folder(body: FolderBody) -> dict:
    return {"folder_id": library.create_folder(db.connect(), body.name)}


@app.patch("/api/folders/{folder_id}")
def rename_folder(folder_id: int, body: FolderBody) -> dict:
    try:
        library.rename_folder(db.connect(), folder_id, body.name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.delete("/api/folders/{folder_id}")
def delete_folder(folder_id: int) -> dict:
    library.delete_folder(db.connect(), folder_id)
    return {"ok": True}


@app.put("/api/playlists/{playlist_id}/folder")
def move_playlist(playlist_id: int, body: MoveBody) -> dict:
    conn = db.connect()
    _playlist_or_404(conn, playlist_id)
    try:
        library.move_to_folder(conn, playlist_id, body.folder_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.put("/api/playlists/{playlist_id}/play-filter")
def set_play_filter(playlist_id: int, body: PlayFilterBody) -> dict:
    """Play and Shuffle only take tracks with at least this many stars (0: every track)."""
    conn = db.connect()
    _playlist_or_404(conn, playlist_id)
    if not 0 <= body.min_stars <= 5:
        raise HTTPException(400, "0–5 stars")
    db.set_setting(conn, f"min_stars:{playlist_id}", body.min_stars)
    return {"min_stars": body.min_stars}


@app.post("/api/playlists/{playlist_id}/cover/track/{track_id}")
def cover_from_track(playlist_id: int, track_id: int) -> dict:
    conn = db.connect()
    _playlist_or_404(conn, playlist_id)
    try:
        library.cover_from_track(conn, playlist_id, track_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except subprocess.SubprocessError:
        raise HTTPException(400, "Couldn't read that track's cover")
    sync.request()
    return {"ok": True}


@app.get("/api/playlists/{playlist_id}/sound-order/{track_id}")
def sound_order(playlist_id: int, track_id: int) -> dict:
    """The playlist's tracks by how close they sound to this one (feedback 2026-11-11 #6)."""
    conn = db.connect()
    _playlist_or_404(conn, playlist_id)
    if not conn.execute("SELECT 1 FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
                        (playlist_id, track_id)).fetchone():
        raise HTTPException(404, "That track isn't in this playlist")
    return {"order": library.sound_order(conn, playlist_id, track_id)}


@app.post("/api/playlists")
def new_playlist(body: NewPlaylist) -> dict:
    """An empty playlist, or one imported from a Spotify link (tracks download in the background)."""
    conn = db.connect()
    if body.spotify_url:
        try:
            info = library.spotify_playlist_info(body.spotify_url)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(502, f"Couldn't read the Spotify page: {e}")
        pid = library.create_playlist(conn, body.name or info["name"] or "Imported", info["id"], info["description"])
        library.save_cover(conn, pid, info["image"])
        job_id = jobs.enqueue(conn, "import", pid, link=body.spotify_url)
        job_runner.kick()
        return {"playlist_id": pid, "job_id": job_id}
    return {"playlist_id": library.create_playlist(conn, body.name or ""), "job_id": None}


@app.post("/api/playlists/{playlist_id}/import")
def retry_import(playlist_id: int) -> dict:
    """Run the playlist's Spotify import again ("Try again", "Refresh from Spotify"): new tracks on Spotify are added,
    tracks already here are kept, failed ones tried again; tracks removed from this playlist stay out."""
    conn = db.connect()
    _playlist_or_404(conn, playlist_id)
    last = jobs.latest_for(conn, playlist_id, "import")
    if last is None or not last["payload"].get("link"):
        raise HTTPException(400, "This playlist wasn't imported from Spotify")
    if last["state"] in ("queued", "running"):
        return {"job_id": last["id"]}
    job_id = jobs.enqueue(conn, "import", playlist_id, link=last["payload"]["link"])
    job_runner.kick()
    return {"job_id": job_id}


@app.get("/api/playlists/{playlist_id}")
def get_playlist(playlist_id: int) -> dict:
    conn = db.connect()
    p = _playlist_or_404(conn, playlist_id)
    from . import tempo

    tracks = library.playlist_tracks(conn, playlist_id)
    s = tempo.playlist_settings(conn, playlist_id)
    core = tempo.playlist_tempo(conn, playlist_id)
    around = core["center"] if core else tempo.median_bpm(t["bpm"] for t in tracks)

    def view(t):
        rate, note = tempo.rate_for(t["bpm"], s)
        if s["on"] and t["bpm_override"]:                       # this track's own tempo in this playlist wins
            rate = tempo.override_rate(t["bpm"], t["bpm_override"], around) or 1.0
            note = f"Set to {t['bpm_override']:g} BPM"
        return {**t, "audio": f"/audio/{t['id']}" if t["has_audio"] else None,
                "art": f"/art/{t['id']}" if t["has_art"] else None,
                "bpm_shown": tempo.shown_bpm(t["bpm"], around), "rate": rate, "tempo_note": note}
    return {"id": p["id"], "name": p["name"], "description": p["description"],
            "image": library.cover_url(p["id"], p["image_path"]),
            "job": _job_view(jobs.latest_for(conn, playlist_id)),
            "deleted": [{**d, "art": f"/art/{d['id']}" if d["has_art"] else None,
                         "audio": f"/audio/{d['id']}" if d["has_audio"] else None, "rate": 1.0}
                        for d in library.deleted_tracks(conn, playlist_id)],
            "can_refresh": bool((jobs.latest_for(conn, playlist_id, "import") or {}).get("payload", {}).get("link")),
            "tracks": [view(t) for t in tracks], "tempo": _tempo_view(s),
            "others": [{"id": o["id"], "name": o["name"]} for o in library.list_playlists(conn) if o["id"] != playlist_id],
            "max_stretch": CONFIG["bpm"]["max_stretch"], "phone_headroom_db": CONFIG["loudness"]["phone_headroom_db"],
            "back_restart_s": CONFIG["ui"]["back_restart_s"], "sync": sync.state(conn, playlist_id),
            "folder_id": p["folder_id"], "folders": library.list_folders(conn),
            "min_stars": db.get_setting(conn, f"min_stars:{playlist_id}", 0)}


class SyncBody(BaseModel):
    on: bool


@app.get("/api/playlists/{playlist_id}/sync")
def get_sync(playlist_id: int) -> dict:
    conn = db.connect()
    _playlist_or_404(conn, playlist_id)
    return sync.state(conn, playlist_id)


@app.put("/api/playlists/{playlist_id}/sync")
def set_sync(playlist_id: int, body: SyncBody) -> dict:
    """Keep this playlist in the folder Syncthing copies to the laptop."""
    conn = db.connect()
    _playlist_or_404(conn, playlist_id)
    sync.set_on(conn, playlist_id, body.on)
    return sync.state(conn, playlist_id)


@app.put("/api/playlists/{playlist_id}/tempo")
def set_playlist_tempo(playlist_id: int, body: TempoBody) -> dict:
    """The playlist's Same BPM: on/off and target. Every track's copy at that target is rendered in the background;
    copies at the old target are deleted (one target per playlist)."""
    from . import tempo

    conn = db.connect()
    _playlist_or_404(conn, playlist_id)
    if body.bpm is not None and not 60 <= body.bpm <= 200:
        raise HTTPException(400, "BPM between 60 and 200")
    bpm = body.bpm if "bpm" in body.model_fields_set else tempo.playlist_settings(conn, playlist_id)["bpm"]
    tempo.save(conn, tempo.playlist_key(playlist_id), body.on, bpm)      # bpm left out: keep the target
    view = _tempo_view(tempo.sync_playlist(conn, playlist_id))
    _after_membership_change(conn, playlist_id)     # a chosen BPM is the playlist's tempo: Find music's filter follows
    return view


class OverrideBody(BaseModel):
    bpm: float


@app.put("/api/playlists/{playlist_id}/tracks/{track_id}/bpm")
def set_track_bpm(playlist_id: int, track_id: int, body: OverrideBody) -> dict:
    """A track's own "Play at … BPM" in this playlist: used instead of the playlist's Set to BPM (while it's on).
    Kept once set; a new value replaces it (its old copy goes), it's never cleared."""
    from . import tempo

    conn = db.connect()
    t = conn.execute("SELECT t.bpm FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id"
                     " WHERE pt.playlist_id = ? AND pt.track_id = ?", (playlist_id, track_id)).fetchone()
    if t is None:
        raise HTTPException(404, "That track isn't in this playlist")
    if not t["bpm"]:
        raise HTTPException(400, "This track has no measured tempo")
    core = tempo.playlist_tempo(conn, playlist_id)
    around = core["center"] if core else t["bpm"]
    rate = tempo.override_rate(t["bpm"], body.bpm, around)
    limit = CONFIG["tempo"]["max_override_stretch"]
    if not 60 <= body.bpm <= 200 or abs(rate - 1) > limit:
        raise HTTPException(400, f"Pick a tempo within ±{round(limit * 100)}% of the track's own "
                                 f"({tempo.nearest(t['bpm'], around):.0f} BPM)")
    with conn:
        conn.execute("UPDATE playlist_tracks SET bpm_override = ? WHERE playlist_id = ? AND track_id = ?",
                     (round(body.bpm, 1), playlist_id, track_id))
    tempo.sync_playlist(conn, playlist_id)
    return {"ok": True, "bpm_override": round(body.bpm, 1)}


@app.post("/api/playlists/{playlist_id}/cover")
async def set_playlist_cover(playlist_id: int, request: Request) -> dict:
    """A picture of my own for the playlist (the file itself is the request's body)."""
    _playlist_or_404(db.connect(), playlist_id)
    limit = CONFIG["library"]["cover_max_mb"] * 1024 * 1024
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            raise HTTPException(413, "That picture is too big")
    if not library.cover_type(bytes(data[:16])):
        raise HTTPException(400, "That isn't a picture I can read (JPEG, PNG, WebP or GIF)")

    def save() -> str | None:
        conn = db.connect()
        library.set_cover(conn, playlist_id, bytes(data))
        p = _playlist_or_404(conn, playlist_id)
        return library.cover_url(playlist_id, p["image_path"])
    try:
        return {"image": await run_in_threadpool(save)}
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise HTTPException(400, "That picture couldn't be read")


@app.patch("/api/playlists/{playlist_id}")
def rename_playlist(playlist_id: int, body: RenameBody) -> dict:
    conn = db.connect()
    _playlist_or_404(conn, playlist_id)
    if body.name is not None:
        library.rename_playlist(conn, playlist_id, body.name)
    if "description" in body.model_fields_set:
        library.set_description(conn, playlist_id, body.description)
    sync.request()
    return {"ok": True}


@app.delete("/api/playlists/{playlist_id}")
def delete_playlist(playlist_id: int) -> dict:
    conn = db.connect()
    _playlist_or_404(conn, playlist_id)
    library.delete_playlist(conn, playlist_id)
    wait_follow_ups()                                  # its Find music may still be catching up with a change
    _delete_session(playlist_id)
    return {"ok": True}


def _delete_session(playlist_id: int) -> None:
    """A deleted playlist's Find music goes too: its worker is forgotten, its pool audio (unless another playlist
    shares it), stretched copies and session database are deleted."""
    from . import tempo

    workers.pop(playlist_id, None)
    if find.started(playlist_id):
        sess = db.connect(playlist_id)
        files = [r[0] for r in sess.execute("SELECT file_path FROM tracks WHERE origin = 'pool' AND file_path IS NOT NULL"
                                            " UNION SELECT undo_path FROM plays WHERE undo_path IS NOT NULL")]
        sess.close()
        base = db.session_path(playlist_id)
        for suffix in ("", "-wal", "-shm"):
            Path(str(base) + suffix).unlink(missing_ok=True)
        lib = db.connect_library()
        for rel in files:
            if rel.startswith(CONFIG["paths"]["cache"] + "/") or rel.startswith(CONFIG["paths"]["data"] + "/undo/"):
                if not db.audio_in_use(rel):
                    resolve(rel).unlink(missing_ok=True)
        db.log_event(lib, "session_deleted", playlist_id=playlist_id, files=len(files))
    for scope in (tempo.scope_for(playlist_id, None), tempo.scope_for(None, playlist_id)):
        tempo._prune(scope, set())


@app.put("/api/playlists/{playlist_id}/tracks/{track_id}")
def star_track(playlist_id: int, track_id: int, body: StarsBody) -> dict:
    conn = db.connect()
    try:
        library.set_stars(conn, playlist_id, track_id, body.stars)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _after_membership_change(conn, playlist_id)               # stars change the seeds straight away
    return {"ok": True}


_RESCORE = threading.Lock()


def _rescore_all(skip: int | None = None) -> None:
    """Re-score every other started Find music session in the background (a rating anywhere changes artist values
    everywhere, §18.29). Sessions whose own background work is running re-score themselves as it goes."""
    def run():
        if not _RESCORE.acquire(blocking=False):
            return                                    # one pass at a time; the next rating starts another
        try:
            for p in library.list_playlists(db.connect_library()):
                if p["id"] == skip or not find.started(p["id"]) or session_worker(p["id"]).state == "running":
                    continue
                token = db.SESSION.set(p["id"])
                try:
                    veto.update(db.connect(p["id"]))
                finally:
                    db.SESSION.reset(token)
        except Exception as e:  # noqa: BLE001
            db.log_event(db.connect_library(), "rescore_failed", error=str(e)[:200])
        finally:
            _RESCORE.release()
    threading.Thread(target=run, daemon=True, name="rescore").start()


def _maintenance() -> None:
    """Hourly, so nothing waits for me to open a page or rate a track (SPEC.md §18.26): every Find music session is
    re-scored (grace periods run out by time too) and its background work restarted if any is due; Recently deleted
    tracks past their 30 days go; stretched copies follow their playlists."""
    from . import tempo

    while True:
        time.sleep(CONFIG["ui"].get("maintenance_minutes", 60) * 60)
        try:
            lib = db.connect_library()
            library.purge_deleted(lib)
            for p in library.list_playlists(lib):
                if not find.started(p["id"]):
                    continue
                token = db.SESSION.set(p["id"])
                try:
                    if session_worker(p["id"]).state != "running":
                        veto.update(db.connect(p["id"]))
                finally:
                    db.SESSION.reset(token)
                session_worker(p["id"]).start_if_needed()
            tempo.sync_all()
            sync.request()
            # Playlist tracks due for their M4A (SPEC.md §19): the PC only wakes for work.
            if CONFIG["worker"]["mode"] == "remote" and analysis.quality_waiting(lib) \
                    and not analysis.worker_connected(lib):
                db.log_event(lib, "pc_wake", reason="quality", problem=analysis.wake())
        except Exception as e:  # noqa: BLE001 — keep the loop alive
            db.log_event(db.connect_library(), "maintenance_failed", error=str(e)[:200])


_FOLLOW = {}                      # playlist id -> {"lock", "thread", "again"}: Find music catching up, one at a time
_FOLLOW_GUARD = threading.Lock()


def _after_membership_change(conn, playlist_id: int) -> None:
    """Seeds changed (stars set or cleared, a track removed or restored): everything that depends on them follows
    straight away — tempo copies and the playlist's tempo range; in Find music the seeds (a newly starred track is
    pulled for, an unstarred one stops being a seed), the tracks they found (they leave with their seed unless
    another found them too, and return with it), and the veto's scores.

    Find music's part runs in the background (SPEC.md §19: on the Pi it took ~2 s, and the star waited for it). Taps
    in quick succession run it once more after the current pass, not once per tap."""
    _sync_tempo(conn, playlist_id)
    sync.request()                                    # the laptop's folder and ratings follow
    if not find.started(playlist_id):
        return
    with _FOLLOW_GUARD:
        f = _FOLLOW.setdefault(playlist_id, {"thread": None, "again": False})
        if f["thread"] is not None and f["thread"].is_alive():
            f["again"] = True
            return
        f["thread"] = threading.Thread(target=_follow_up, args=(playlist_id,), daemon=True)
        f["thread"].start()


def _follow_up(playlist_id: int) -> None:
    while True:
        token = db.SESSION.set(playlist_id)
        try:
            lib, sess = db.connect_library(), db.connect(playlist_id)
            find.sync_seeds(lib, sess, playlist_id)
            veto.update(sess)
            from . import tempo
            tempo.sync_session(sess, playlist_id)     # its default Set to BPM follows the playlist's tempo
        except Exception as e:                        # the next change or maintenance pass tries again
            db.log_event(db.connect_library(), "follow_up_failed", playlist_id=playlist_id, error=str(e)[:200])
        finally:
            db.SESSION.reset(token)
        session_worker(playlist_id).start_if_needed()        # pull for new seeds, top the pool up
        with _FOLLOW_GUARD:
            f = _FOLLOW[playlist_id]
            if not f["again"]:
                f["thread"] = None
                return
            f["again"] = False


def wait_follow_ups(timeout: float = 30) -> None:
    """Wait for Find music to catch up with playlist changes (tests, and deleting a playlist)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with _FOLLOW_GUARD:
            busy = [f["thread"] for f in _FOLLOW.values() if f["thread"] is not None]
        if not busy:
            return
        busy[0].join(max(0.0, end - time.monotonic()))


@app.delete("/api/playlists/{playlist_id}/tracks/{track_id}")
def remove_track(playlist_id: int, track_id: int) -> dict:
    """Move a track to the playlist's Recently deleted list."""
    conn = db.connect()
    try:
        library.remove_from_playlist(conn, playlist_id, track_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    _after_membership_change(conn, playlist_id)
    return {"ok": True}


@app.post("/api/playlists/{playlist_id}/deleted/{track_id}/restore")
def restore_track(playlist_id: int, track_id: int) -> dict:
    conn = db.connect()
    try:
        library.restore_track(conn, playlist_id, track_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    _after_membership_change(conn, playlist_id)
    return {"ok": True}


@app.delete("/api/playlists/{playlist_id}/deleted/{track_id}")
def delete_track_forever(playlist_id: int, track_id: int) -> dict:
    conn = db.connect()
    library.delete_forever(conn, playlist_id, track_id)
    return {"ok": True}


class LinkBody(BaseModel):
    link: str


@app.post("/api/playlists/{playlist_id}/add")
def add_song(playlist_id: int, body: LinkBody) -> dict:
    """Add one song from a Spotify track link or a YouTube / YouTube Music link (downloaded and analysed as a job)."""
    from . import spotify

    conn = db.connect()
    _playlist_or_404(conn, playlist_id)
    link = body.link.strip()
    ok = library.youtube_video_id(link) is not None
    if not ok:
        try:
            ok = spotify.parse(link)[0] == "track"
        except ValueError:
            ok = False
    if not ok:
        raise HTTPException(400, "Paste a Spotify track link or a YouTube / YouTube Music link")
    job_id = jobs.enqueue(conn, "add", playlist_id, link=link)
    job_runner.kick()
    return {"job_id": job_id}


@app.post("/api/playlists/{playlist_id}/tracks")
def add_track(playlist_id: int, body: AddTrackBody) -> dict:
    conn = db.connect()
    try:
        added = library.add_to_playlist(conn, playlist_id, body.track_id, "added")   # lands in "To rate"
        _after_membership_change(conn, playlist_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"added": added}


@app.post("/api/tracks/{track_id}/similar")
def similar_playlist(track_id: int) -> dict:
    """"Find similar" (feedback #39): a new playlist from this track, with its closest tracks to rate."""
    conn = db.connect()
    try:
        pid = library.similar_playlist(conn, track_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    _after_membership_change(conn, pid)
    return {"playlist_id": pid}


@app.get("/api/playlists/{playlist_id}/find")
def find_state(playlist_id: int) -> dict:
    """Find music for this playlist: none (not started) / preparing (progress) / ready / error."""
    conn = db.connect()
    _playlist_or_404(conn, playlist_id)
    seeds = conn.execute("SELECT count(*) FROM playlist_tracks WHERE playlist_id = ? AND stars IS NOT NULL",
                         (playlist_id,)).fetchone()[0]
    return {**find.state(playlist_id, workers.get(playlist_id)), "seeds": seeds}


@app.post("/api/playlists/{playlist_id}/find")
def start_find(playlist_id: int) -> dict:
    """First time: create the playlist's session, copy its starred tracks in as seeds, start pulling and filling."""
    conn = db.connect()
    _playlist_or_404(conn, playlist_id)
    if not conn.execute("SELECT 1 FROM playlist_tracks WHERE playlist_id = ? AND stars IS NOT NULL",
                        (playlist_id,)).fetchone():
        raise HTTPException(400, "Star at least one track first — starred tracks are the seeds")
    find.sync_seeds(conn, db.connect(playlist_id), playlist_id)
    session_worker(playlist_id).start_if_needed()
    return find_state(playlist_id)


# --- the PC worker's API (SPEC.md §18.4): it asks for tracks to download and analyse, and sends them back ---------

def worker_auth(request: Request) -> None:
    token = analysis.worker_token()
    if not token:
        raise HTTPException(503, "WORKER_TOKEN isn't set on this side")
    if request.headers.get("authorization") != f"Bearer {token}":
        raise HTTPException(401, "Wrong worker token")


@app.post("/api/worker/claim", dependencies=[Depends(worker_auth)])
def worker_claim():
    item = analysis.claim(db.connect_library())
    return item if item is not None else Response(status_code=204)


@app.post("/api/worker/{work_id}/file/{kind}", dependencies=[Depends(worker_auth)])
async def worker_file(work_id: int, kind: str, request: Request, ext: str = ""):
    if kind not in ("audio", "art", "embedding"):
        raise HTTPException(404, "Unknown file kind")
    body = await request.body()
    if not analysis.receive_file(db.connect_library(), work_id, kind, body, ext):
        raise HTTPException(409, "Not wanted any more")
    return {"ok": True}


# --- the Android app's updates (SPEC.md §19): the latest build lives on the Pi; the app checks it at launch ---------

def _app_dir() -> Path:
    return path("data") / "app"


@app.get("/app/latest.json")
def app_latest() -> dict:
    f = _app_dir() / "latest.json"
    if not f.exists():
        raise HTTPException(404, "No app published yet")
    return {**json.loads(f.read_text()), "url": "/app/music-hunter.apk"}


@app.get("/app/music-hunter.apk")
def app_apk() -> FileResponse:
    f = _app_dir() / "music-hunter.apk"
    if not f.exists():
        raise HTTPException(404, "No app published yet")
    return FileResponse(f, media_type="application/vnd.android.package-archive", filename="music-hunter.apk")


@app.post("/api/admin/app", dependencies=[Depends(worker_auth)])
async def app_publish(request: Request, version_code: int, version_name: str):
    """A new build of the app, sent from the PC (`music-hunter pi app`)."""
    import hashlib

    body = await request.body()
    if body[:2] != b"PK":
        raise HTTPException(400, "That isn't an APK")
    folder = _app_dir()
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / "music-hunter.apk.part"
    tmp.write_bytes(body)
    tmp.replace(folder / "music-hunter.apk")
    info = {"version_code": version_code, "version_name": version_name, "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest()}
    (folder / "latest.json").write_text(json.dumps(info))
    db.log_event(db.connect_library(), "app_published", **info)
    return info


@app.post("/api/worker/quality/claim", dependencies=[Depends(worker_auth)])
def quality_claim():
    item = analysis.quality_claim(db.connect_library())
    return item if item else Response(status_code=204)


@app.post("/api/worker/quality/{track_id}", dependencies=[Depends(worker_auth)])
async def quality_file(track_id: int, request: Request, ext: str, quality: int, lufs: float, peak: float, gain: float,
                       why: str = ""):
    body = await request.body()
    if not analysis.quality_receive(db.connect_library(), track_id, body, ext, quality, lufs, peak, gain, why):
        raise HTTPException(409, "Not wanted any more")
    sync.request()                                    # now it can go to the laptop
    return {"ok": True}


class WorkDone(BaseModel):
    duration_s: float
    lufs: float
    peak: float
    gain: float
    model: str
    bpm: float | None = None
    published: str | None = None
    timings: dict = {}
    notes: dict = {}


@app.post("/api/worker/{work_id}/done", dependencies=[Depends(worker_auth)])
def worker_done(work_id: int, body: WorkDone):
    try:
        ok = analysis.finish(db.connect_library(), work_id, body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(409, "Not wanted any more")
    return {"ok": True}


class WorkFailed(BaseModel):
    stage: str
    error: str
    timings: dict = {}


@app.post("/api/worker/{work_id}/failed", dependencies=[Depends(worker_auth)])
def worker_failed(work_id: int, body: WorkFailed):
    if not analysis.finish(db.connect_library(), work_id, {"stage": body.stage, "timings": body.timings}, body.error):
        raise HTTPException(409, "Not wanted any more")
    return {"ok": True}


@app.post("/api/worker/{work_id}/release", dependencies=[Depends(worker_auth)])
def worker_release(work_id: int, pause_s: float = 0):
    if not analysis.release(db.connect_library(), work_id, pause_s):
        raise HTTPException(409, "Not wanted any more")
    return {"ok": True}


@app.get("/api/worker")
def worker_status() -> dict:
    return analysis.status(db.connect_library())


# --- admin, for the PC (SPEC.md §18.11): moving data in, snapshots out for analysis, log, restart -------------------

def _restart_soon() -> None:
    """Exit shortly; the add-on's start script starts the app again (unpacking an uploaded bundle first)."""
    import os

    threading.Timer(1.0, os._exit, [0]).start()


@app.get("/api/admin/snapshot", dependencies=[Depends(worker_auth)])
def admin_snapshot():
    import tempfile

    from starlette.background import BackgroundTask

    from . import transfer
    from .config import path as cpath

    tmp = Path(tempfile.mkstemp(suffix=".tar", dir=cpath("data"))[1])
    transfer.bundle(tmp, audio=False, log=True)
    return FileResponse(tmp, media_type="application/x-tar", filename="snapshot.tar",
                        background=BackgroundTask(tmp.unlink, missing_ok=True))


@app.post("/api/admin/restore", dependencies=[Depends(worker_auth)])
async def admin_restore(request: Request, force: int = 0):
    from . import config

    if config.ROOT == config.PROJECT:
        raise HTTPException(400, "Only in the add-on (a separate data folder)")
    if library.list_playlists(db.connect_library()) and not force:
        raise HTTPException(409, "This app already has playlists; send with force to replace them")
    incoming = config.ROOT / "incoming.tar"
    part = incoming.with_suffix(".part")
    with part.open("wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    part.rename(incoming)
    _restart_soon()
    return {"ok": True, "bytes": incoming.stat().st_size}


@app.get("/api/admin/feedback", dependencies=[Depends(worker_auth)])
def admin_feedback() -> list[dict]:
    rows = db.connect_library().execute("SELECT * FROM feedback ORDER BY id").fetchall()
    return [{**dict(r), "context": json.loads(r["context"] or "{}")} for r in rows]


@app.post("/api/admin/artist-history", dependencies=[Depends(worker_auth)])
def admin_artist_history(rows: list[dict] = Body(...)):
    """Ratings from archived runs, for the artist part of the veto score (SPEC.md §18.29); replaces each source's."""
    from . import artist

    n = artist.store(db.connect_library(), rows)
    _rescore_all()
    return {"stored": n}


def _code_status() -> dict:
    """Which code runs: the release, or code pushed from the PC (`pi push`, kept in <data>/code)."""
    import os

    from . import config

    def label(folder):
        f = folder / "PUSHED.json"
        return json.loads(f.read_text()) if f.exists() else None
    release = Path(os.environ.get("MUSIC_HUNTER_RELEASE", "/app")) / "VERSION"
    pushed = label(config.PROJECT) if config.PROJECT == config.ROOT / "code" else None
    return {"release": release.read_text().strip() if release.exists() else "?", "pushed": pushed,
            "failed": None if pushed else label(config.ROOT / "code.failed")}


@app.get("/api/admin/code", dependencies=[Depends(worker_auth)])
def admin_code() -> dict:
    return _code_status()


@app.post("/api/admin/code", dependencies=[Depends(worker_auth)])
async def admin_push_code(request: Request):
    """Code pushed from the PC (`music-hunter pi push`): the start script unpacks it and runs the app from it."""
    from . import config

    if config.ROOT == config.PROJECT:
        raise HTTPException(400, "Only in the add-on (a separate data folder)")
    incoming = config.ROOT / "code-incoming.tar"
    part = incoming.with_suffix(".part")
    with part.open("wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    part.rename(incoming)
    (config.ROOT / "code-revert").unlink(missing_ok=True)
    _restart_soon()
    return {"ok": True}


@app.delete("/api/admin/code", dependencies=[Depends(worker_auth)])
def admin_revert_code():
    """Back to the released code (`music-hunter pi revert`)."""
    from . import config

    if config.ROOT == config.PROJECT:
        raise HTTPException(400, "Only in the add-on (a separate data folder)")
    (config.ROOT / "code-incoming.tar").unlink(missing_ok=True)
    (config.ROOT / "code-revert").touch()
    _restart_soon()
    return {"ok": True}


@app.post("/api/admin/restart", dependencies=[Depends(worker_auth)])
def admin_restart():
    _restart_soon()
    return {"ok": True}


@app.get("/api/admin/log", dependencies=[Depends(worker_auth)])
def admin_log(lines: int = 200):
    from fastapi.responses import PlainTextResponse

    from .config import path as cpath

    f = cpath("data") / "music-hunter.log"
    text = f.read_text(errors="replace").splitlines()[-lines:] if f.exists() else []
    return PlainTextResponse("\n".join(text))


@app.get("/find/{playlist_id}", response_class=HTMLResponse)
def find_page(playlist_id: int, request: Request):
    from fastapi.responses import RedirectResponse

    if not find.started(playlist_id):
        return RedirectResponse(f"/playlist/{playlist_id}")
    return _page("index.html", request)


@app.get("/cover/{playlist_id}")
def cover(playlist_id: int) -> FileResponse:
    p = _playlist_or_404(db.connect(), playlist_id)
    if not p["image_path"] or not resolve(p["image_path"]).exists():
        raise HTTPException(404, "No cover")
    return FileResponse(resolve(p["image_path"]))


router = APIRouter()


@router.get("/api/state")
def state() -> dict:
    conn = db.connect()
    unplayed = len(ratings.playable_ids(conn))          # v4: playable tracks with audio ready
    plays = conn.execute("SELECT count(*), count(rating) FROM plays").fetchone()
    first_pull = plays[0] == 0
    complete = ratings.run_complete(conn)
    ready = not complete and unplayed > 0 and \
        (not first_pull or unplayed >= UI["early_start_tracks"] or session_worker().state != "running")
    last = ratings.last_rated(conn)
    return {
        "ready": ready, "run_complete": complete, "unplayed": unplayed, "early_start": UI["early_start_tracks"],
        "pool_target": CONFIG["pool"]["target_size"],
        "plays": plays[0], "rated": plays[1],
        "worker": {"state": session_worker().state, "message": session_worker().message},
        "last": track_info(conn, last) if last else None,
        "labels": ratings.LABELS, "hints": CONFIG["ratings"]["hints"], "add": ratings.ADD,
        "play_to_end": UI["play_to_end"], "preload_next": UI["preload_next"], "stall_log_ms": UI["stall_log_ms"],
        "back_restart_s": UI["back_restart_s"],
        "seeds": conn.execute("SELECT count(*) FROM tracks WHERE origin = 'original_reference'").fetchone()[0],
    }


@router.post("/api/next")
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
            session_worker().start_if_needed()
            raise HTTPException(409, "No playable tracks in the pool")
        if not restore_audio(conn, track_id):          # an injected vetoed track's audio, if not fetched ahead
            db.set_setting(conn, "next_draw", None, log=False)
            raise HTTPException(503, "Couldn't fetch the next track's audio; try again")
        play_id = ratings.show_next(conn)
        play = conn.execute("SELECT * FROM plays WHERE id = ?", (play_id,)).fetchone()
    return track_info(conn, play)


@router.get("/api/upcoming")
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
    bpm = conn.execute("SELECT bpm FROM tracks WHERE id = ?", (track_id,)).fetchone()[0]
    return {"track_id": track_id, "audio": f"{_base()}/audio/{track_id}", **_find_rate(conn, bpm)}


_fetching: set[tuple] = set()


def _fetch_in_background(track_id: int) -> None:
    from .pool import restore_audio

    pid = db.SESSION.get()
    if (pid, track_id) in _fetching:
        return
    _fetching.add((pid, track_id))

    def run():
        db.SESSION.set(pid)                      # threads don't inherit the request's session
        try:
            restore_audio(db.connect(), track_id)
        finally:
            _fetching.discard((pid, track_id))
    threading.Thread(target=run, daemon=True).start()


@router.get("/api/previous")
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


@router.post("/api/rate")
def rate(body: RateBody) -> dict:
    conn = db.connect()
    try:
        ratings.rate(conn, body.play_id, body.rating, body.played_seconds, body.played_bpm, body.playback_rate)
    except ValueError as e:
        raise HTTPException(400, str(e))
    reweight.after_rating(conn)
    veto.update(conn)                         # likes changed: re-score, move tracks across the line
    _to_playlist(conn, body.play_id)
    session_worker().start_if_needed()
    _rescore_all(skip=db.SESSION.get())          # the artist's value changed in the other playlists too
    return track_info(conn, conn.execute("SELECT * FROM plays WHERE id = ?", (body.play_id,)).fetchone())


@router.post("/api/skip")
def skip(body: SkipBody) -> dict:
    """Skipped, or ended without a rating: counts as [ratings] unrated_counts_as (Close), marked automatic.
    With 0, the old rule: the track counts as shown and teaches nothing."""
    conn = db.connect()
    auto = CONFIG["ratings"]["unrated_counts_as"]
    if not auto:
        ratings.skip(conn, body.play_id, body.played_seconds)
        session_worker().start_if_needed()
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
        _rescore_all(skip=db.SESSION.get())
    session_worker().start_if_needed()
    return {**track_info(conn, conn.execute("SELECT * FROM plays WHERE id = ?", (body.play_id,)).fetchone()),
            "rated": True}


@router.post("/api/change")
def change(body: ChangeBody) -> dict:
    conn = db.connect()
    try:
        play_id = ratings.change_last_rating(conn, body.rating)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _to_playlist(conn, play_id)                   # an Add joins the playlist / a changed-away Add leaves it
    if db.SESSION.get() is not None:              # …and the tracks it found follow it
        find.sync_seeds(db.connect_library(), conn, db.SESSION.get())
    veto.update(conn)
    _rescore_all(skip=db.SESSION.get())
    session_worker().start_if_needed()
    return track_info(conn, conn.execute("SELECT * FROM plays WHERE id = ?", (play_id,)).fetchone())


@router.post("/api/tags")
def set_tags(body: TagsBody) -> dict:
    """Optional reason tags for the most recent rating (logged only)."""
    conn = db.connect()
    try:
        ratings.set_tags(conn, body.play_id, body.tags)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return track_info(conn, conn.execute("SELECT * FROM plays WHERE id = ?", (body.play_id,)).fetchone())


@router.get("/api/settings")
def get_settings() -> dict:
    from .scoring import target_bpm, tempo_settings

    conn = db.connect()
    target, source = target_bpm(conn)
    return {**tempo_settings(conn), "effective_target_bpm": target, "target_source": source,
            "max_stretch": CONFIG["bpm"]["max_stretch"]}


@router.post("/api/settings")
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


@router.post("/api/event")
def client_event(body: ClientEvent) -> dict:
    """Player-side events: start latency, stalls, audio errors, track ends, script errors."""
    if body.kind not in CLIENT_EVENTS:
        raise HTTPException(400, f"Unknown event kind {body.kind!r}")
    db.log_event(db.connect(), f"player_{body.kind}", body.play_id, **body.data)
    return {"ok": True}


@router.get("/api/results")
def results() -> dict:
    return results_data(db.connect())


@router.get("/audio/{track_id}")
def audio(track_id: int) -> FileResponse:
    conn = db.connect()
    t = conn.execute("SELECT file_path FROM tracks WHERE id = ?", (track_id,)).fetchone()
    held = conn.execute("SELECT undo_path FROM plays WHERE track_id = ? AND undo_path IS NOT NULL",
                        (track_id,)).fetchone()
    rel = (t and t["file_path"]) or (held and held["undo_path"])
    if not rel or not resolve(rel).exists():
        raise HTTPException(404, "Audio not available")
    return FileResponse(resolve(rel))


def _audio_path(track_id: int) -> str | None:
    conn = db.connect()
    t = conn.execute("SELECT file_path FROM tracks WHERE id = ?", (track_id,)).fetchone()
    held = conn.execute("SELECT undo_path FROM plays WHERE track_id = ? AND undo_path IS NOT NULL",
                        (track_id,)).fetchone()
    rel = (t and t["file_path"]) or (held and held["undo_path"])
    return rel if rel and resolve(rel).exists() else None


@router.get("/tempo/{track_id}")
def tempo_status(track_id: int, rate: float, pl: int | None = None, ahead: int = 0) -> dict:
    """Same BPM: is the stretched copy ready? Queues it if not — first in line unless `ahead` (SPEC.md §18.13).
    `pl` = the playlist, for the playlist page (Find music's session is known from the URL)."""
    from . import tempo

    if not 0.5 <= rate <= 2:
        raise HTTPException(400, "Rate out of range")
    if db.SESSION.get() is None and pl is None:
        raise HTTPException(400, "Which playlist?")
    rel = _audio_path(track_id)
    if rel is None:
        raise HTTPException(404, "Audio not available")
    scope = tempo.scope_for(db.SESSION.get(), pl)
    st = tempo.status(scope, track_id, rel, rate, urgent=not ahead)
    q = f"rate={rate}" + (f"&pl={pl}" if pl is not None else "")
    return {"state": st, "url": f"{_base()}/tempo/{track_id}/audio?{q}" if st == "ready" else None}


@router.get("/tempo/{track_id}/audio")
def tempo_audio(track_id: int, rate: float, pl: int | None = None) -> FileResponse:
    from . import tempo

    f = tempo.target(tempo.scope_for(db.SESSION.get(), pl), track_id, rate)
    if not f.exists():
        raise HTTPException(404, "Not rendered yet")
    return FileResponse(f, media_type="audio/webm")


@router.get("/api/tempo")
def find_tempo() -> dict:
    """Find music's own Same BPM setting (separate from the playlist's)."""
    from . import tempo

    return _tempo_view(tempo.session_settings(db.connect()))


@router.put("/api/tempo")
def set_find_tempo(body: TempoBody) -> dict:
    from . import tempo

    if db.SESSION.get() is None:
        raise HTTPException(404, "Only in a playlist's Find music")
    if body.bpm is not None and not 60 <= body.bpm <= 200:
        raise HTTPException(400, "BPM between 60 and 200")
    conn = db.connect()
    bpm = body.bpm if "bpm" in body.model_fields_set else tempo.session_settings(conn)["bpm"]
    tempo.save(conn, "same_bpm", body.on, bpm)
    tempo.sync_session(conn, db.SESSION.get())               # copies at another target are deleted
    pid = db.SESSION.get()

    def rescore():                                           # a chosen BPM moves the tempo filter
        token = db.SESSION.set(pid)
        try:
            veto.update(db.connect(pid))
        finally:
            db.SESSION.reset(token)
    threading.Thread(target=rescore, daemon=True).start()
    return _tempo_view(tempo.session_settings(conn))


@router.get("/api/tempo/{track_id}")
def find_track_tempo(track_id: int) -> dict:
    from . import tempo

    conn = db.connect()
    t = conn.execute("SELECT bpm FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if t is None:
        raise HTTPException(404, "No such track")
    rate, note = tempo.rate_for(t[0], tempo.session_settings(conn))
    return {"rate": rate, "tempo_note": note}


@router.get("/art/{track_id}")
def art(track_id: int) -> FileResponse:
    """Cover art: library tracks always; Find music pool tracks only once rated (blind until then)."""
    conn = db.connect()
    t = conn.execute("SELECT art_path, rating, origin FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if not t or not t["art_path"] or (UI["hide_names_until_rated"] and t["rating"] is None and t["origin"] == "pool"):
        raise HTTPException(404, "No artwork")
    if not resolve(t["art_path"]).exists():
        raise HTTPException(404, "No artwork")
    return FileResponse(resolve(t["art_path"]))


# The listening interface's endpoints: at the root for the single database of earlier versions (and tests), and per
# playlist under /find/<id>/… for v5 sessions (SPEC.md §18.2).
async def use_session(playlist_id: int) -> None:
    if not find.started(playlist_id):
        raise HTTPException(404, "Find music hasn't been started for this playlist")
    db.SESSION.set(playlist_id)


app.include_router(router)
app.include_router(router, prefix="/find/{playlist_id}", dependencies=[Depends(use_session)])


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
        print(f"Player: http://{host}:{UI['port']}")
    print("Log: data/music-hunter.log")
    uvicorn.Server(uvicorn.Config(app, log_level="warning", timeout_keep_alive=75)).run(sockets=sockets)
