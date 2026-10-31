"""Same BPM playback: tracks time-stretched on the server, pitch kept (v5).

The browser's own pitch-keeping speed change (playbackRate) sounded crackly, so a stretched copy is rendered with
ffmpeg's Rubber Band filter instead (much cleaner on music) and cached in cache/tempo/. Rendering a 7-minute track
takes ~6 s on the PC (longer on the Pi), so it runs in the background: the player plays the original until the
stretched copy is ready, and asks for the next track's copy ahead of time.
"""

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db
from .config import CONFIG, path, resolve

CFG = CONFIG["tempo"]
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tempo")
_pending: set[Path] = set()
_failed: dict[Path, str] = {}
_lock = threading.Lock()


def _rate(rate: float) -> float:
    return round(float(rate), 3)


def target(session: int | None, track_id: int, rate: float) -> Path:
    return path("cache") / "tempo" / f"{'lib' if session is None else session}-{track_id}-{_rate(rate):.3f}.webm"


def status(session: int | None, track_id: int, source: str, rate: float) -> str:
    """'ready', 'rendering' (started now if needed) or 'failed'."""
    dest = target(session, track_id, rate)
    if dest.exists():
        return "ready"
    with _lock:
        if dest in _failed:
            return "failed"
        if dest not in _pending:
            _pending.add(dest)
            _pool.submit(_render, resolve(source), dest, _rate(rate))
    return "rendering"


def _render(src: Path, dest: Path, rate: float) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part.webm")
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vn",
                        "-af", f"rubberband=tempo={rate}:pitchq=quality",
                        "-c:a", "libopus", "-b:a", f"{CFG['bitrate_kbps']}k", str(part)],
                       check=True, capture_output=True, text=True)
        part.rename(dest)
        _trim()
    except subprocess.CalledProcessError as e:
        with _lock:
            _failed[dest] = (e.stderr or "")[-300:]
        part.unlink(missing_ok=True)
        db.log_event(db.connect_library(), "tempo_failed", file=str(src.name), rate=rate, error=_failed[dest])
    finally:
        with _lock:
            _pending.discard(dest)


def _trim() -> None:
    """Keep the cache under [tempo] cache_mb, oldest first."""
    files = sorted((path("cache") / "tempo").glob("*.webm"), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    while files and total > CFG["cache_mb"] * 1e6:
        f = files.pop(0)
        total -= f.stat().st_size
        f.unlink(missing_ok=True)
