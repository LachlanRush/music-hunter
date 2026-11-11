"""Same BPM playback: tracks time-stretched on the server, pitch kept (v5, SPEC.md §18.13).

The browser's own pitch-keeping speed change sounded crackly, so stretched copies are rendered with ffmpeg's Rubber
Band filter and kept in cache/tempo/. Each playlist and each Find music session has its own setting (on/off and a
target BPM; default: the playlist's median BPM), stored on the server:
- a playlist with Same BPM on gets a copy of every track at its target, rendered in the background (new tracks too);
- Find music gets copies on demand: the track playing first, the next one ahead;
- copies are made only once Same BPM has been turned on at least once (never before); after that they're kept
  ready at the target even while it's off, so turning it back on is instant;
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


def nearest(bpm: float, around: float) -> float:
    return min((bpm * f for f in FOLDS), key=lambda r: abs(around / r - 1))


def core_from(items) -> dict | None:
    """The tempo range of a set of (bpm, stars) — 4–5 star tracks (all starred if too few have 4–5; all given if
    none is starred): {'center' (median), 'lo', 'hi'}, each read at the tempo nearest the plain median."""
    v = CONFIG["veto"]
    items = [(b, s) for b, s in items if b]
    pick = [b for b, s in items if s and s >= v["tempo_core_stars"]]
    if len(pick) < v["tempo_core_min"]:
        pick = [b for b, s in items if s] or [b for b, _ in items]
    if not pick:
        return None
    first = float(sorted(pick)[len(pick) // 2])
    folded = sorted(nearest(b, first) for b in pick)
    return {"center": round(folded[len(folded) // 2], 1), "lo": round(folded[0], 1), "hi": round(folded[-1], 1)}


def shift_to(core: dict | None, center: float | None) -> dict | None:
    """A tempo range moved to centre on a chosen BPM, keeping its width (2026-11-11: my own Set to BPM replaces the
    automatic median)."""
    if not center:
        return core
    half_lo, half_hi = (core["center"] - core["lo"], core["hi"] - core["center"]) if core else (0.0, 0.0)
    return {"center": round(center, 1), "lo": round(center - half_lo, 1), "hi": round(center + half_hi, 1),
            "manual": True}


def playlist_tempo(lib, pid: int) -> dict | None:
    """The playlist's tempo, shared by the playlist page, its Find music and the veto: the BPM I set in Set to BPM if
    I chose one (the range of its 4–5 star tracks moved to centre on it), else the typical tempo and range of its
    4–5 star tracks."""
    return shift_to(auto_tempo(lib, pid), db.get_setting(lib, playlist_key(pid), {}).get("bpm"))


def auto_tempo(lib, pid: int) -> dict | None:
    """The typical tempo and range of the playlist's 4–5 star tracks. Kept steady: it only moves when the centre or an
    end changes by tempo_hysteresis_bpm."""
    fresh = core_from(lib.execute("SELECT t.bpm, pt.stars FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id"
                                  " WHERE pt.playlist_id = ? AND pt.stars IS NOT NULL", (pid,)).fetchall())
    key, step = f"tempo_core:{pid}", CONFIG["veto"]["tempo_hysteresis_bpm"]
    kept = db.get_setting(lib, key)
    if fresh and (not kept or any(abs(fresh[k] - kept[k]) >= step for k in ("center", "lo", "hi"))):
        db.set_setting(lib, key, fresh)
        return fresh
    return kept or fresh


def settings(conn, key: str, default_target: float) -> dict:
    saved = db.get_setting(conn, key, {})
    # Settings saved before `engaged` existed (0.5.4) came from someone using the switch: count them as engaged.
    s = {"on": False, "bpm": None, "engaged": bool(saved), **saved}
    return {**s, "target": s["bpm"] or default_target, "auto": default_target}


def rate_for(bpm: float | None, s: dict, ignore_off: bool = False) -> tuple[float, str]:
    """(playback rate, note) for a track: the reading of its BPM closest to the target; 1 if off or too far.
    `ignore_off`: the rate its copy is made at (copies are kept ready even while Same BPM is off)."""
    if not s["on"] and not (ignore_off and s.get("engaged")):
        return 1.0, ""                                   # never turned on: no copies at all
    target, max_stretch = s["target"], CONFIG["bpm"]["max_stretch"]
    if not bpm or not target:
        return 1.0, "Own tempo (unknown BPM)"
    reading = min((bpm * f for f in FOLDS), key=lambda r: abs(target / r - 1))
    r = target / reading
    if abs(r - 1) > max_stretch:
        return 1.0, f"Own tempo ({round(reading)} BPM, too far from {target:g})"
    return round(r, 3), f"Set to {target:g} BPM"


def playlist_key(pid: int) -> str:
    return f"same_bpm:{pid}"


def playlist_settings(lib, pid: int) -> dict:
    """The playlist's Set to BPM; its default target is the playlist's typical tempo."""
    core = auto_tempo(lib, pid)
    if core is None:
        bpms = [r[0] for r in lib.execute("SELECT t.bpm FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id"
                                          " WHERE pt.playlist_id = ?", (pid,))]
    return settings(lib, playlist_key(pid), core["center"] if core else median_bpm(bpms))


def session_settings(sess, session: int | None = None) -> dict:
    """Find music: its own setting; the default target is its playlist's typical tempo."""
    pid = db.SESSION.get() if session is None else session
    core = playlist_tempo(db.connect_library(), pid) if pid is not None else None
    if core is None:
        bpms = [r[0] for r in sess.execute("SELECT bpm FROM tracks WHERE library_id IS NOT NULL")]
    return settings(sess, "same_bpm", core["center"] if core else median_bpm(bpms))


def override_rate(bpm: float | None, override: float | None, around: float | None = None) -> float | None:
    """The rate for a track's own "Play at … BPM" in a playlist, or None. The track is read at the tempo shown for
    it (its reading nearest the playlist's typical tempo, `around`), so a 2:3 misread doesn't change the meaning."""
    if not bpm or not override:
        return None
    return round(override / nearest(bpm, around or override), 3)


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
    core = playlist_tempo(lib, pid)
    scope = scope_for(None, pid)
    wanted: set[tuple[int, float]] = set()
    for t in lib.execute("SELECT t.id, t.bpm, t.file_path, pt.bpm_override FROM playlist_tracks pt JOIN tracks t"
                         " ON t.id = pt.track_id WHERE pt.playlist_id = ? AND t.file_path IS NOT NULL", (pid,)):
        if not resolve(t["file_path"]).exists():
            continue
        # At most two copies per track: the playlist's Set to BPM and the track's own override (kept once set).
        rates = {rate_for(t["bpm"], s, ignore_off=True)[0]} if s.get("engaged") or s["on"] else set()
        if t["bpm_override"]:
            rates.add(override_rate(t["bpm"], t["bpm_override"], core and core["center"]) or 1.0)
        for rate in rates - {1.0}:
            wanted.add((t["id"], rate))
            status(scope, t["id"], t["file_path"], rate, urgent=False)
    _prune(scope, wanted)
    return s


def sync_session(sess, session: int) -> None:
    """Find music's copies: only at the current target, and only for tracks not played yet or among the last few."""
    s = session_settings(sess, session)
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
    """`engaged` remembers that it has been turned on at least once: from then on copies are kept ready."""
    engaged = bool(on) or bool(db.get_setting(conn, key, {}).get("engaged"))
    db.set_setting(conn, key, {"on": bool(on), "bpm": bpm, "engaged": engaged}, log=True)

