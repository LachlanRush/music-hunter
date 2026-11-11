"""Extended mixes for playlist songs (2026-11-11, feedback #41: the playlists are for DJing, and an extended mix is
always best). Every song in a playlist is checked once: if YouTube Music has the same song as an extended mix, longer
than the version I have, the PC downloads and analyses it, and if it sounds like the same recording (its MAEST
fingerprint close to the one I have) it replaces the song everywhere: what plays in the app, the laptop copy, the
256 kbps fetch (done again for the new video). Stars, playlists and the song's place stay as they were.

It runs in the background, one song at a time, and gives way while any playlist's Find music is busy.
"""

import shutil
import threading
import time

import numpy as np

from . import db, names
from .config import CONFIG, path, relative, resolve

CFG = CONFIG["extended"]


def due(lib) -> list[int]:
    """Playlist songs not checked yet (with audio, a YouTube video and a fingerprint)."""
    return [r[0] for r in lib.execute(
        "SELECT DISTINCT t.id FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id"
        " JOIN track_embeddings e ON e.track_id = t.id"
        " WHERE t.file_path IS NOT NULL AND t.video_id IS NOT NULL AND t.extended_checked_at IS NULL ORDER BY t.id")]


def find_candidate(yt, artist: str, title: str, duration_s: float | None):
    """The same song as an extended mix on YouTube Music (songs, then uploads), longer than mine; or None."""
    me = names.song(artist, title)
    if me.is_extended:
        return None                                    # already the extended mix
    clean = names.strip_versions(title)[0]
    query = f"{artist.split(',')[0].strip()} {clean} extended mix"
    found = [r for r in yt.search_songs(query) + yt.search_videos(query)
             if r.video_id and r.duration_s and r.duration_s >= (duration_s or 0) + CFG["min_longer_s"]
             and names.song(r.artist, r.title).same_song(me) and names.song(r.artist, r.title).is_extended]
    return min(found, key=lambda r: r.duration_s) if found else None   # the shortest extended: the plain one


def check(lib, track_id: int, yt) -> str:
    """Check one song; returns what happened ('none', 'replaced', 'rejected', 'failed')."""
    from .analysis import AnalysisFailed, DownloadFailed, backend
    from .ratings import _safe, _unique

    t = lib.execute("SELECT id, artist, title, duration_s, video_id, file_path FROM tracks WHERE id = ?",
                    (track_id,)).fetchone()
    with lib:
        lib.execute("UPDATE tracks SET extended_checked_at = datetime('now') WHERE id = ?", (track_id,))
    cand = find_candidate(yt, t["artist"], t["title"], t["duration_s"])
    if cand is None or cand.video_id == t["video_id"]:
        return "none"
    try:
        res = backend().analyse(cand.video_id)
    except (DownloadFailed, AnalysisFailed) as e:
        db.log_event(lib, "extended_failed", track_id=track_id, video_id=cand.video_id, error=str(e)[:200])
        return "failed"
    old = db.from_blob(lib.execute("SELECT embedding FROM track_embeddings WHERE track_id = ?",
                                   (track_id,)).fetchone()[0])
    new = np.asarray(res.embedding.track, dtype=np.float32)
    sim = float(old @ new / (np.linalg.norm(old) * np.linalg.norm(new) or 1))
    if sim < CFG["min_similarity"]:                   # a different recording under the same name: keep mine
        res.file.unlink(missing_ok=True)
        if res.art:
            res.art.unlink(missing_ok=True)
        db.log_event(lib, "extended_rejected", track_id=track_id, video_id=cand.video_id, similarity=round(sim, 3))
        return "rejected"
    folder = path("library")
    folder.mkdir(parents=True, exist_ok=True)
    dest = _unique(folder / f"{_safe(t['artist'])} - {_safe(t['title'])} (Extended){res.file.suffix}", res.file)
    shutil.move(res.file, dest)
    if res.art:
        res.art.unlink(missing_ok=True)               # the cover stays the one I have
    with lib:
        lib.execute("UPDATE tracks SET file_path = ?, duration_s = ?, loudness_lufs = ?, true_peak_db = ?, gain_db = ?,"
                    " video_id = ?, quality = NULL, quality_tried_at = NULL WHERE id = ?",
                    (relative(dest), res.duration_s, res.lufs, res.peak, res.gain, cand.video_id, track_id))
        db.store_embedding(lib, track_id, res.model, res.embedding)
    db.release_audio(lib, t["file_path"])             # the shorter version's file, unless something else uses it
    db.log_event(lib, "extended_replaced", track_id=track_id, old_video=t["video_id"], video_id=cand.video_id,
                 old_s=round(t["duration_s"] or 0), new_s=round(res.duration_s), similarity=round(sim, 3))
    return "replaced"


_lock = threading.Lock()


def run(busy=lambda: False) -> None:
    """Check every song not checked yet, one at a time, waiting while `busy()` (Find music working)."""
    if not CFG["enabled"] or not _lock.acquire(blocking=False):
        return
    try:
        from .analysis import youtube_paused
        from .sources import YouTubeMusic

        lib, yt = db.connect_library(), None
        for tid in due(lib):
            while busy() or youtube_paused():
                time.sleep(30)
            yt = yt or YouTubeMusic()
            try:
                check(lib, tid, yt)
            except Exception as e:  # noqa: BLE001 — one song's trouble doesn't stop the rest
                db.log_event(lib, "extended_failed", track_id=tid, error=str(e)[:200])
            time.sleep(CFG["pause_seconds"])
    finally:
        _lock.release()


def request(busy=lambda: False) -> None:
    threading.Thread(target=run, args=(busy,), daemon=True, name="extended").start()
