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
import time
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
    # The tempo they agree on best: every reading is folded (half, double, 2:3) to its nearest point to a candidate,
    # and the candidate with the least spread wins. (The plain median of the readings could itself be a misreading:
    # Interesting d&b's 174 BPM tracks read as 173, 116 (2:3) and 87 (half); their median, 116, folded the 87s to
    # 130, and every track showed ~116. Fixed 2026-11-11.)
    import math

    def spread(c):
        return sum(min(abs(math.log(c / (b * f))) for f in FOLDS) for b in pick)
    # Candidates within the range tempos are measured in ([bpm] min–max, a little over for d&b's 174–176): with the
    # ties going to the faster candidate, a 60–200 range let one 130 BPM house track read as 195 (2:3), and Liked,
    # blend and Disco House were all set to ~190 (#135, 2026-11-25).
    lo, hi = CONFIG["bpm"]["min"], CONFIG["bpm"]["max"] + 2
    candidates = {round(b * f, 1) for b in pick for f in FOLDS if lo <= b * f <= hi}
    first = min(candidates or {float(sorted(pick)[len(pick) // 2])}, key=lambda c: (round(spread(c), 3), -c))
    folded = sorted(nearest(b, first) for b in pick)
    center = folded[len(folded) // 2]
    # A song far off the others' tempo doesn't stretch the range (2026-11-18, #71: No More's 4★ Good 4 U at 127 BPM
    # pulled the range from ~136 down to 127, so slow house got through); there must still be tempo_core_min left.
    far = v.get("tempo_core_outlier_pct", 0) / 100
    if far:
        near = [b for b in folded if abs(b / center - 1) <= far]
        if len(near) >= v["tempo_core_min"]:
            folded = near
            center = folded[len(folded) // 2]
    return {"center": round(center, 1), "lo": round(folded[0], 1), "hi": round(folded[-1], 1)}


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
    saved = db.get_setting(lib, playlist_key(pid), {})
    bpm = saved.get("bpm")
    if bpm and saved.get("below") is not None:         # set: fixed, range and all (#136)
        return {"center": round(bpm, 1), "lo": round(bpm - saved["below"], 1), "hi": round(bpm + saved["above"], 1),
                "manual": True}
    return shift_to(auto_tempo(lib, pid), bpm)


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
    target, max_stretch = s["target"], CONFIG["tempo"].get("play_max_stretch", CONFIG["bpm"]["max_stretch"])
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
    s = settings(lib, playlist_key(pid), core["center"] if core else median_bpm(bpms))
    if s["on"] and not s["bpm"]:
        # Once Set to BPM is on, its number stays fixed (#136, 2026-11-25: with no number typed, it followed the
        # playlist's typical tempo, which moved as songs were starred). The number shown is kept.
        save(lib, playlist_key(pid), True, s["target"])
        s = {**s, "bpm": s["target"]}
    return s


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


NOW, NEXT, LATER = 0, 1, 2                           # the track playing, the one after it, everything else


class Renderer:
    """Stretched copies, most wanted first: the track playing (NOW), then the one after it (NEXT), then every other
    playlist copy kept ready (LATER). Two lanes (2026-11-18): one only ever takes NOW and NEXT, so the player's copy
    never waits behind a background render (the Pi has four cores; a render keeps one busy); the other takes
    anything, the player's first. Background renders run at a lower CPU priority."""

    def __init__(self):
        self.queues: list[deque] = [deque(), deque(), deque()]
        self.level: dict[Path, int] = {}                 # queued, by priority
        self.failed: dict[Path, str] = {}
        self.running: dict[str, Path] = {}               # lane -> being rendered now
        self.cancelled: set[Path] = set()                # … and no longer wanted (deleted when it finishes)
        self.cv = threading.Condition()
        self.threads: dict[str, threading.Thread] = {}

    # (kept for _prune's view of what's queued or running)
    @property
    def queue(self) -> list:
        return [j for q in self.queues for j in q]

    @property
    def current(self) -> Path | None:
        return next(iter(self.running.values()), None)

    @property
    def pending(self) -> set[Path]:
        return set(self.level) | set(self.running.values())

    def submit(self, src: Path, dest: Path, rate: float, urgent: bool | int) -> str:
        level = urgent if isinstance(urgent, int) and not isinstance(urgent, bool) else (NOW if urgent else LATER)
        if dest.exists():
            return "ready"
        with self.cv:
            if dest in self.failed:
                return "failed"
            if dest in self.running.values():
                return "rendering"
            if dest in self.level:
                if level < self.level[dest] or level == NOW:     # wanted sooner: move it up (NOW: to the front)
                    q = self.queues[self.level[dest]]
                    found = [j for j in q if j[1] == dest]
                    for j in found:
                        q.remove(j)
                    if found:
                        (self.queues[level].appendleft if level == NOW else self.queues[level].append)(found[0])
                        self.level[dest] = level
                        self.cv.notify_all()
                return "rendering"
            job = (src, dest, rate, time.monotonic())
            (self.queues[level].appendleft if level == NOW else self.queues[level].append)(job)
            self.level[dest] = level
            for lane in ("player", "any"):
                t = self.threads.get(lane)
                if t is None or not t.is_alive():
                    self.threads[lane] = threading.Thread(target=self._loop, args=(lane,), daemon=True,
                                                          name=f"tempo-{lane}")
                    self.threads[lane].start()
            self.cv.notify_all()
        return "rendering"

    def drop(self, dests: set[Path]) -> None:
        with self.cv:
            for q in self.queues:
                for job in [j for j in q if j[1] in dests]:
                    q.remove(job)
                    self.level.pop(job[1], None)
            for dest in self.running.values():
                if dest in dests:
                    self.cancelled.add(dest)

    def _take(self, lane: str):
        levels = (NOW, NEXT) if lane == "player" else (NOW, NEXT, LATER)
        for lv in levels:
            if self.queues[lv]:
                job = self.queues[lv].popleft()
                self.level.pop(job[1], None)
                return lv, job
        return None, None

    def _loop(self, lane: str) -> None:
        while True:
            with self.cv:
                lv, job = self._take(lane)
                if job is None:
                    if lane == "player":                 # stays a moment: the next player request is often close
                        self.cv.wait(30)
                        lv, job = self._take(lane)
                    if job is None:
                        self.threads.pop(lane, None)
                        return
                src, dest, rate, queued_at = job
                self.running[lane] = dest
            t0 = time.monotonic()
            try:
                _render(src, dest, rate, background=lv == LATER)
                db.log_event(db.connect_library(), "tempo_rendered", file=dest.name, level=lv, lane=lane,
                             waited_s=round(t0 - queued_at, 1), seconds=round(time.monotonic() - t0, 1))
            except subprocess.CalledProcessError as e:
                with self.cv:
                    self.failed[dest] = (e.stderr or "")[-300:]
                db.log_event(db.connect_library(), "tempo_failed", file=src.name, rate=rate, error=self.failed[dest])
            except Exception as e:  # noqa: BLE001 — a missing source etc.: the lane carries on
                with self.cv:
                    self.failed[dest] = str(e)[-300:]
                db.log_event(db.connect_library(), "tempo_failed", file=src.name, rate=rate, error=str(e)[-300:])
            finally:
                with self.cv:
                    self.running.pop(lane, None)
                    if dest in self.cancelled:
                        self.cancelled.discard(dest)
                        dest.unlink(missing_ok=True)


RENDERER = Renderer()


def _render(src: Path, dest: Path, rate: float, background: bool = False) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(f"{dest.stem}.{threading.get_ident()}.part.webm")   # two lanes: never the same part file
    try:
        subprocess.run(["nice", "-n", "15"] * background + ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vn",
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


def status(scope: str, track_id: int, source: str, rate: float, urgent: bool | int = True) -> str:
    """'ready', 'rendering' (queued now if needed) or 'failed'. `urgent`: True (the track playing), False (kept
    ready in the background), or a level (NOW, NEXT, LATER)."""
    return RENDERER.submit(resolve(source), target(scope, track_id, rate), round(rate, 3), urgent)


def _prune(scope: str, wanted: set[tuple[int, float]]) -> None:
    """Delete this scope's copies that aren't wanted (other targets, tracks gone), and stop queued renders of them."""
    unwanted = {f for f, tid, rate in _copies(scope) if (tid, rate) not in wanted}
    stale = set()
    for dest in [j[1] for j in RENDERER.queue] + list(RENDERER.running.values()):
        m = NAME.match(dest.name)
        if m and m["scope"] == scope and (int(m["track"]), float(m["rate"])) not in wanted:
            stale.add(dest)
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
        saved = db.get_setting(lib, playlist_key(pid), {})
        if saved.get("bpm") and saved.get("below") is None:     # set before 2026-11-25: its range is fixed now (#136)
            save(lib, playlist_key(pid), saved.get("on", False), saved["bpm"])
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
    """`engaged` remembers that it has been turned on at least once: from then on copies are kept ready. A playlist's
    chosen BPM keeps the tempo range it had when it was set (its width either side), so stars given later don't move
    the tempo filter either (#136, 2026-11-25)."""
    saved = db.get_setting(conn, key, {})
    engaged = bool(on) or bool(saved.get("engaged"))
    value = {"on": bool(on), "bpm": bpm, "engaged": engaged}
    if bpm and key.startswith("same_bpm:"):
        if bpm == saved.get("bpm") and saved.get("below") is not None:
            value.update(below=saved["below"], above=saved["above"])
        else:
            core = auto_tempo(conn, int(key.split(":")[1]))
            if core:
                value.update(below=round(core["center"] - core["lo"], 1), above=round(core["hi"] - core["center"], 1))
    db.set_setting(conn, key, value, log=True)

