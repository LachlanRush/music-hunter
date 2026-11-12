"""Extended mixes for playlist songs (2026-11-11, feedback #41: the playlists are for DJing, and an extended mix is
always best). Every song in a playlist is checked once: if YouTube Music has the same song as an extended mix, longer
than the version I have, the PC downloads and analyses it, and if it sounds like the same recording (its MAEST
fingerprint close to the one I have) it replaces the song everywhere: what plays in the app, the laptop copy, the
256 kbps fetch (done again for the new video). Stars, playlists and the song's place stay as they were.

It runs in the background, one song at a time, and gives way while any playlist's Find music is busy.

2026-11-12: the same machinery also brings songs stuck at ~130 kbps up to 256 kbps: such a song sits on an upload
(a label's or a fan's video), and YouTube only offers 256 kbps for official tracks, so it's swapped for the official
version of the same recording (`upgrade`), the longest found, up to `upgrade_max_shorter` shorter than mine.
Either swap needs the fingerprints to agree closely (`min_similarity`): the same recording, never a remix or edit.
"""

import shutil
import threading
import time

import numpy as np

from . import db, names
from .config import CONFIG, path, relative, resolve

CFG = CONFIG["extended"]


OFFICIAL = {"ATV", "OMV"}


def due_upgrade(lib) -> list[int]:
    """Playlist songs that only got the ~130 kbps file (format 141 isn't offered for their video), not checked yet."""
    return [r[0] for r in lib.execute(
        "SELECT DISTINCT t.id FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id"
        " JOIN track_embeddings e ON e.track_id = t.id"
        " WHERE t.file_path IS NOT NULL AND t.video_id IS NOT NULL AND t.quality = 1 AND t.quality_tried_at IS NOT NULL"
        " AND t.upgrade_checked_at IS NULL ORDER BY t.id")]


def find_official(yt, artist: str, title: str, duration_s: float | None, video_id: str) -> list:
    """Official versions of the same song (not another artist's remix or edit), longest first, none shorter than
    mine by more than `upgrade_max_shorter`."""
    me = names.song(artist, title)
    clean = names.strip_versions(title)[0]
    query = f"{artist.split(',')[0].strip()} {clean}"
    floor = (duration_s or 0) * (1 - CFG["upgrade_max_shorter"])
    seen, out = {video_id}, []
    for r in yt.search_songs(query) + yt.search_videos(query):
        if (r.video_id and r.video_id not in seen and r.kind in OFFICIAL and r.duration_s and r.duration_s >= floor
                and names.song(r.artist, r.title).same_song(me)):
            seen.add(r.video_id)
            out.append(r)
    return sorted(out, key=lambda r: -r.duration_s)


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
             and names.song(r.artist, r.title).same_song(me) and names.song(r.artist, r.title).is_extended
             and r.kind in OFFICIAL]                   # an upload has no 256 kbps file (2026-11-12): official only
    return min(found, key=lambda r: r.duration_s) if found else None   # the shortest extended: the plain one


def check(lib, track_id: int, yt, upgrade: bool = False) -> str:
    """Check one song for its extended mix (or, `upgrade`, for an official 256 kbps version); returns what happened
    ('none', 'replaced', 'rejected', 'failed')."""
    from .analysis import AnalysisFailed, DownloadFailed, backend
    from .ratings import _safe, _unique

    t = lib.execute("SELECT id, artist, title, duration_s, video_id, file_path FROM tracks WHERE id = ?",
                    (track_id,)).fetchone()
    kind = "upgrade" if upgrade else "extended"
    with lib:
        lib.execute(f"UPDATE tracks SET {kind}_checked_at = datetime('now') WHERE id = ?", (track_id,))
    if upgrade:
        cands = find_official(yt, t["artist"], t["title"], t["duration_s"], t["video_id"])
    else:
        c = find_candidate(yt, t["artist"], t["title"], t["duration_s"])
        cands = [c] if c is not None and c.video_id != t["video_id"] else []
    if not cands:
        return "none"
    old = db.from_blob(lib.execute("SELECT embedding FROM track_embeddings WHERE track_id = ?",
                                   (track_id,)).fetchone()[0])
    res = cand = None
    for c in cands[:3]:                               # the longest that is the same recording
        try:
            r = backend().analyse(c.video_id)
        except (DownloadFailed, AnalysisFailed) as e:
            db.log_event(lib, f"{kind}_failed", track_id=track_id, video_id=c.video_id, error=str(e)[:200])
            continue
        new = np.asarray(r.embedding.track, dtype=np.float32)
        sim = float(old @ new / (np.linalg.norm(old) * np.linalg.norm(new) or 1))
        if sim >= CFG["min_similarity"]:
            res, cand = r, c
            break
        r.file.unlink(missing_ok=True)                # a different recording under the same name: keep mine
        if r.art:
            r.art.unlink(missing_ok=True)
        db.log_event(lib, f"{kind}_rejected", track_id=track_id, video_id=c.video_id, similarity=round(sim, 3))
    if res is None:
        return "rejected"
    other = lib.execute("SELECT id FROM tracks WHERE video_id = ? AND origin = 'library' AND id != ?",
                        (cand.video_id, track_id)).fetchone()
    folder = path("library")
    folder.mkdir(parents=True, exist_ok=True)
    suffix = "" if upgrade else " (Extended)"
    dest = _unique(folder / f"{_safe(t['artist'])} - {_safe(t['title'])}{suffix}{res.file.suffix}", res.file)
    shutil.move(res.file, dest)
    if res.art:
        res.art.unlink(missing_ok=True)               # the cover stays the one I have
    with lib:
        lib.execute("UPDATE tracks SET file_path = ?, duration_s = ?, loudness_lufs = ?, true_peak_db = ?, gain_db = ?,"
                    " video_id = ?, quality = NULL, quality_tried_at = NULL WHERE id = ?",
                    (relative(dest), res.duration_s, res.lufs, res.peak, res.gain, cand.video_id, track_id))
        db.store_embedding(lib, track_id, res.model, res.embedding)
    db.release_audio(lib, t["file_path"])             # the version replaced, unless something else uses it
    db.log_event(lib, f"{kind}_replaced", track_id=track_id, old_video=t["video_id"], video_id=cand.video_id,
                 old_s=round(t["duration_s"] or 0), new_s=round(res.duration_s), similarity=round(sim, 3))
    if other is not None:                             # that video was already in the library: one song, not two
        from .library import merge_tracks
        merge_tracks(lib, keep=track_id, drop=other[0])
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
        from .library import merge_duplicates
        merge_duplicates(lib)
        for tid, upgrade in [(t, False) for t in due(lib)] + [(t, True) for t in due_upgrade(lib)]:
            while busy() or youtube_paused():
                time.sleep(30)
            yt = yt or YouTubeMusic()
            try:
                check(lib, tid, yt, upgrade)
            except Exception as e:  # noqa: BLE001 — one song's trouble doesn't stop the rest
                db.log_event(lib, "extended_failed", track_id=tid, error=str(e)[:200])
            time.sleep(CFG["pause_seconds"])
    finally:
        _lock.release()


def request(busy=lambda: False) -> None:
    threading.Thread(target=run, args=(busy,), daemon=True, name="extended").start()
