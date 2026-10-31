"""Same BPM playback: tracks time-stretched on the server, pitch kept (v5, SPEC.md §18.13).

The browser's own pitch-keeping speed change sounded crackly, so stretched copies are rendered with ffmpeg's Rubber
Band filter and kept in cache/tempo/. Each playlist and each Find music session has its own setting (on/off and a
target BPM; default: the playlist's median BPM), stored on the server:
- a playlist with Same BPM on gets a copy of every track at its target, rendered in the background (new tracks too);
- Find music gets copies on demand: the track playing first, the next one ahead;
- copies are made at the target even while Same BPM is off, so turning it on is instant;
- only one target per playlist / session is kept: changing it deletes the old copies (turning it off doesn't), and
  Find music keeps copies only for tracks not yet played or among the last few plays. [tempo] cache_mb caps the
  folder as a backstop.
Until a track's copy is ready the original plays, and the player swaps the copy in at the same point in the music.
"""

import re
import subprocess
import threading
from collections import deque
from pathlib import Path

from . import db
from .config import CONFIG, path, resolve

CFG = CONFIG["tempo"]
FOLDS = (1, 2, 0.5, 1.5, 2 / 3)       # a measured BPM can be a half, double or 2:3 reading of the real tempo
KEEP_PLAYED = 3                         # Find music: copies kept for the last few plays (the back button)


# --- settings and rates ---------------------------------------------------------------------------------------------

def median_bpm(bpms) -> float:
    b = sorted(x for x in bpms if x)
    return round(b[len(b) // 2] * 2) / 2 if b else 128.0


def shown_bpm(bpm: float | None, around: float) -> int | None:
    """A track's tempo for display: the reading closest to `around` (the playlist's median), so a 2:3 or half
    reading shows as the tempo it really plays at (Love Again: read 93.6, shown 140)."""
    if not bpm:
        return None
    return round(min((bpm * f for f in FOLDS), key=lambda r: abs(around / r - 1)))


def settings(conn, key: str, default_target: float) -> dict:
    s = {"on": False, "bpm": None, **db.get_setting(conn, key, {})}
    return {**s, "target": s["bpm"] or default_target}


def rate_for(bpm: float | None, s: dict, ignore_off: bool = False) -> tuple[float, str]:
    """(playback rate, note) for a track: the reading of its BPM closest to the target; 1 if off or too far.
    `ignore_off`: the rate its copy is made at (copies are kept ready even while Same BPM is off)."""
    if not s["on"] and not ignore_off:
        return 1.0, ""
    target, max_stretch = s["target"], CONFIG["bpm"]["max_stretch"]
    if not bpm or not target:
        return 1.0, "tempo unknown: original speed"
    reading = min((bpm * f for f in FOLDS), key=lambda r: abs(target / r - 1))
    r = target / reading
    if abs(r - 1) > max_stretch:
        return 1.0, f"original tempo ({round(reading)} BPM is beyond ±{round(max_stretch * 100)}%)"
    rate = round(r, 3)
    pct = round((rate - 1) * 100, 1)
    return rate, f"{target:g} BPM ({'+' if pct >= 0 else ''}{pct:g}%)"


def playlist_key(pid: int) -> str:
    return f"same_bpm:{pid}"


def playlist_settings(lib, pid: int) -> dict:
    bpms = [r[0] for r in lib.execute("SELECT t.bpm FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id"
                                      " WHERE pt.playlist_id = ?", (pid,))]
    return settings(lib, playlist_key(pid), median_bpm(bpms))


def session_settings(sess) -> dict:
    """Find music: its own setting; the default target is the playlist's (its seeds') median BPM."""
    bpms = [r[0] for r in sess.execute("SELECT bpm FROM tracks WHERE library_id IS NOT NULL")]
    return settings(sess, "same_bpm", median_bpm(bpms))


# --- the copies -----------------------------------------------------------------------------------------------------

def scope_for(session: int | None, playlist: int | None) -> str:
    return f"find{session}" if session is not None else f"pl{playlist}"


def target(scope: str, track_id: int, rate: float) -> Path:
    return path("cache") / "tempo" / f"{scope}-{track_id}-{round(rate, 3):.3f}.webm"


NAME = re.compile(r"^(?P<scope>[a-z]+\d+)-(?P<track>\d+)-(?P<rate>[\d.]+)\.webm$")


def _copies(scope: str):
    folder = path("cache") / "tempo"
    for f in folder.glob(f"{scope}-*.webm") if folder.exists() else []:
        m = NAME.match(f.name)
        if m and m["scope"] == scope:
            yield f, int(m["track"]), float(m["rate"])


class Renderer:
    """One render at a time (the Pi has four cores; a render keeps one busy); urgent ones (the track playing) first."""

    def __init__(self):
        self.queue: deque = deque()
        self.pending: set[Path] = set()
        self.failed: dict[Path, str] = {}
        self.current: Path | None = None                 # being rendered now
        self.cancelled: set[Path] = set()                # … and no longer wanted (deleted when it finishes)
        self.cv = threading.Condition()
        self.thread: threading.Thread | None = None

    def submit(self, src: Path, dest: Path, rate: float, urgent: bool) -> str:
        if dest.exists():
            return "ready"
        with self.cv:
            if dest in self.failed:
                return "failed"
            if dest in self.pending:
                if urgent:                                   # move it to the front
                    for job in list(self.queue):
                        if job[1] == dest:
                            self.queue.remove(job)
                            self.queue.appendleft(job)
                return "rendering"
            self.pending.add(dest)
            (self.queue.appendleft if urgent else self.queue.append)((src, dest, rate))
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, daemon=True, name="tempo")
                self.thread.start()
            self.cv.notify()
        return "rendering"

    def drop(self, dests: set[Path]) -> None:
        with self.cv:
            for job in [j for j in self.queue if j[1] in dests]:
                self.queue.remove(job)
                self.pending.discard(job[1])
            if self.current in dests:
                self.cancelled.add(self.current)

    def _loop(self) -> None:
        while True:
            with self.cv:
                if not self.queue:
                    return
                src, dest, rate = self.queue.popleft()
                self.current = dest
            try:
                _render(src, dest, rate)
            except subprocess.CalledProcessError as e:
                with self.cv:
                    self.failed[dest] = (e.stderr or "")[-300:]
                db.log_event(db.connect_library(), "tempo_failed", file=src.name, rate=rate, error=self.failed[dest])
            finally:
                with self.cv:
                    self.pending.discard(dest)
                    self.current = None
                    if dest in self.cancelled:
                        self.cancelled.discard(dest)
                        dest.unlink(missing_ok=True)


RENDERER = Renderer()


def _render(src: Path, dest: Path, rate: float) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.stem + ".part.webm")
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vn",
                        "-af", f"rubberband=tempo={rate}:pitchq=quality",
                        "-c:a", "libopus", "-b:a", f"{CFG['bitrate_kbps']}k", str(part)],
                       check=True, capture_output=True, text=True)
        part.rename(dest)
    finally:
        part.unlink(missing_ok=True)
    _trim()


def _trim() -> None:
    """Backstop: keep the folder under [tempo] cache_mb, oldest first."""
    files = sorted((path("cache") / "tempo").glob("*.webm"), key=lambda f: f.stat().st_mtime)
    files = [f for f in files if ".part." not in f.name]
    total = sum(f.stat().st_size for f in files)
    while files and total > CFG["cache_mb"] * 1e6:
        f = files.pop(0)
        total -= f.stat().st_size
        f.unlink(missing_ok=True)


def status(scope: str, track_id: int, source: str, rate: float, urgent: bool = True) -> str:
    """'ready', 'rendering' (queued now if needed) or 'failed'."""
    return RENDERER.submit(resolve(source), target(scope, track_id, rate), round(rate, 3), urgent)


def _prune(scope: str, wanted: set[tuple[int, float]]) -> None:
    """Delete this scope's copies that aren't wanted (other targets, tracks gone), and stop queued renders of them."""
    unwanted = {f for f, tid, rate in _copies(scope) if (tid, rate) not in wanted}
    stale = set()
    for job in list(RENDERER.queue) + ([(None, RENDERER.current, None)] if RENDERER.current else []):
        m = NAME.match(job[1].name)
        if m and m["scope"] == scope and (int(m["track"]), float(m["rate"])) not in wanted:
            stale.add(job[1])
    RENDERER.drop(stale)
    for f in unwanted:
        f.unlink(missing_ok=True)


def sync_playlist(lib, pid: int) -> dict:
    """A playlist's copies: every track with audio at the current target (queued in the background); anything else
    in its scope deleted. Called when the setting changes, tracks are added or removed, and at start-up."""
    s = playlist_settings(lib, pid)
    scope = scope_for(None, pid)
    wanted: set[tuple[int, float]] = set()
    for t in lib.execute("SELECT t.id, t.bpm, t.file_path FROM playlist_tracks pt JOIN tracks t"
                         " ON t.id = pt.track_id WHERE pt.playlist_id = ? AND t.file_path IS NOT NULL", (pid,)):
        rate, _ = rate_for(t["bpm"], s, ignore_off=True)
        if rate != 1 and resolve(t["file_path"]).exists():
            wanted.add((t["id"], rate))
            status(scope, t["id"], t["file_path"], rate, urgent=False)
    _prune(scope, wanted)
    return s


def sync_session(sess, session: int) -> None:
    """Find music's copies: only at the current target, and only for tracks not played yet or among the last few."""
    s = session_settings(sess)
    scope = scope_for(session, None)
    recent = {r[0] for r in sess.execute("SELECT track_id FROM plays ORDER BY id DESC LIMIT ?", (KEEP_PLAYED,))}
    played = {r[0] for r in sess.execute("SELECT DISTINCT track_id FROM plays")} | \
        {r[0] for r in sess.execute("SELECT id FROM tracks WHERE veto_state = 'dropped'")}
    wanted = set()
    for f, tid, rate in _copies(scope):
        if tid in played and tid not in recent:
            continue
        bpm = sess.execute("SELECT bpm FROM tracks WHERE id = ?", (tid,)).fetchone()
        if bpm and rate_for(bpm[0], s, ignore_off=True)[0] == rate:
            wanted.add((tid, rate))
    _prune(scope, wanted)


def sync_all() -> None:
    """At start-up: every playlist's copies (and nothing left over from deleted playlists)."""
    lib = db.connect_library()
    pids = [r[0] for r in lib.execute("SELECT id FROM playlists")]
    for pid in pids:
        sync_playlist(lib, pid)
    folder = path("cache") / "tempo"
    if folder.exists():
        for f in folder.glob("pl*-*.webm"):
            m = NAME.match(f.name)
            if not m or int(m["scope"][2:]) not in pids:
                f.unlink(missing_ok=True)
        for f in folder.glob("*.part.webm"):
            f.unlink(missing_ok=True)


def save(conn, key: str, on: bool, bpm: float | None) -> None:
    db.set_setting(conn, key, {"on": bool(on), "bpm": bpm}, log=True)

