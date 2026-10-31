"""Find music per playlist — SPEC.md §18.2–18.3 (v5).

Each playlist's session has its own database (db.session_path), where the v4 machinery runs unchanged. This module
connects the two:
- sync_seeds: the playlist's starred tracks are copied into the session as seeds (track, fingerprint, stars); stars
  changed in the playlist are carried over at the start of every pull; unstarred tracks stop being seeds.
- sync_add: a track Added in the session joins the playlist's "Added — waiting for stars" section (its audio in
  library/, its fingerprint copied to the library); if that rating is changed away from Add, it leaves again.
- state: none / preparing / ready, for the playlist page's button and progress bar.
"""

import json
import sqlite3

from . import db
from .config import CONFIG, path, relative, resolve

COPY = ("artist", "title", "file_path", "duration_s", "video_id", "spotify_id", "release_date", "bpm",
        "loudness_lufs", "true_peak_db", "gain_db", "art_path")


def _copy_embedding(src: sqlite3.Connection, src_id: int, dst: sqlite3.Connection, dst_id: int) -> None:
    e = src.execute("SELECT * FROM track_embeddings WHERE track_id = ?", (src_id,)).fetchone()
    if e is None:
        return
    cols = [k for k in e.keys() if k != "track_id"]
    dst.execute(f"INSERT OR REPLACE INTO track_embeddings (track_id, {', '.join(cols)}) VALUES (?{', ?' * len(cols)})",
                (dst_id, *[e[k] for k in cols]))
    for c in src.execute("SELECT * FROM chunk_embeddings WHERE track_id = ?", (src_id,)).fetchall():
        ccols = [k for k in c.keys() if k != "track_id"]
        dst.execute(f"INSERT OR REPLACE INTO chunk_embeddings (track_id, {', '.join(ccols)}) VALUES (?{', ?' * len(ccols)})",
                    (dst_id, *[c[k] for k in ccols]))


def sync_seeds(lib: sqlite3.Connection, sess: sqlite3.Connection, playlist_id: int) -> dict:
    """Make the session's seeds match the playlist's starred tracks. Returns {'seeds', 'added', 'unstarred'}."""
    starred = lib.execute("SELECT t.*, pt.stars AS playlist_stars FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id"
                          " WHERE pt.playlist_id = ? AND pt.stars IS NOT NULL", (playlist_id,)).fetchall()
    by_lib = {r["library_id"]: r for r in sess.execute("SELECT id, library_id, stars FROM tracks WHERE library_id IS NOT NULL")}
    added = 0
    with sess:
        for t in starred:
            if t["id"] in by_lib:
                sess.execute("UPDATE tracks SET stars = ? WHERE id = ?", (t["playlist_stars"], by_lib[t["id"]]["id"]))
                continue
            vals = {k: t[k] for k in COPY}
            sid = sess.execute(
                f"INSERT INTO tracks ({', '.join(vals)}, origin, rating, stars, library_id)"
                f" VALUES ({', '.join('?' * len(vals))}, 'original_reference', ?, ?, ?)",
                (*vals.values(), CONFIG["ratings"]["add"], t["playlist_stars"], t["id"])).lastrowid
            _copy_embedding(lib, t["id"], sess, sid)
            if t["video_id"]:
                sess.execute("INSERT OR REPLACE INTO seed_matches (track_id, video_id, yt_artist, yt_title, yt_duration_s)"
                             " VALUES (?, ?, ?, ?, ?)", (sid, t["video_id"], t["artist"], t["title"], t["duration_s"]))
            added += 1
        keep = [t["id"] for t in starred] or [-1]
        unstarred = sess.execute(f"UPDATE tracks SET stars = NULL WHERE library_id IS NOT NULL AND stars IS NOT NULL"
                                 f" AND library_id NOT IN ({','.join('?' * len(keep))})", keep).rowcount
    return {"seeds": len(starred), "added": added, "unstarred": unstarred}


def sync_add(lib: sqlite3.Connection, sess: sqlite3.Connection, playlist_id: int, play_id: int) -> None:
    """After a rating (or a change): an Add joins the playlist's Added section; a changed-away Add leaves it."""
    p = sess.execute("SELECT p.rating, t.* FROM plays p JOIN tracks t ON t.id = p.track_id WHERE p.id = ?",
                     (play_id,)).fetchone()
    if p is None:
        return
    if p["rating"] == CONFIG["ratings"]["add"]:
        lid = p["library_id"]
        if lid is None or not lib.execute("SELECT 1 FROM tracks WHERE id = ?", (lid,)).fetchone():
            existing = lib.execute("SELECT id FROM tracks WHERE video_id = ? ORDER BY id LIMIT 1",
                                   (p["video_id"],)).fetchone() if p["video_id"] else None
            if existing:
                lid = existing[0]
                if p["file_path"]:
                    with lib:
                        lib.execute("UPDATE tracks SET file_path = coalesce(file_path, ?) WHERE id = ?", (p["file_path"], lid))
            else:
                from .library import keep_art

                vals = {k: p[k] for k in COPY}
                vals["art_path"] = keep_art(vals["art_path"])
                with lib:
                    lid = lib.execute(f"INSERT INTO tracks ({', '.join(vals)}, origin) VALUES"
                                      f" ({', '.join('?' * len(vals))}, 'library')", list(vals.values())).lastrowid
                    _copy_embedding(sess, p["id"], lib, lid)
            with sess:
                sess.execute("UPDATE tracks SET library_id = ? WHERE id = ?", (lid, p["id"]))
        with lib:
            lib.execute("INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, status) VALUES (?, ?, 'added')",
                        (playlist_id, lid))
            if p["file_path"]:
                lib.execute("UPDATE tracks SET file_path = ? WHERE id = ?", (p["file_path"], lid))
    elif p["library_id"]:
        # Changed away from Add: leave the playlist if it's still only waiting there (never starred by me).
        with lib:
            n = lib.execute("DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ? AND status = 'added'"
                            " AND stars IS NULL", (playlist_id, p["library_id"])).rowcount
            if n and not lib.execute("SELECT 1 FROM playlist_tracks WHERE track_id = ?", (p["library_id"],)).fetchone():
                lib.execute("UPDATE tracks SET file_path = NULL WHERE id = ?", (p["library_id"],))


def started(playlist_id: int) -> bool:
    return db.session_path(playlist_id).exists()


def state(playlist_id: int, worker=None) -> dict:
    """{'state': 'none' | 'preparing' | 'ready' | 'error', 'message', 'playable', 'early_start'} for a playlist."""
    early = CONFIG["ui"]["early_start_tracks"]
    if not started(playlist_id):
        return {"state": "none", "message": "", "playable": 0, "early_start": early}
    from .ratings import playable_ids

    sess = db.connect(playlist_id)
    playable = len(playable_ids(sess))
    running = worker is not None and worker.state == "running"
    if worker is not None and worker.state == "error":
        st = "error"
    elif playable >= early or (playable and not running):
        st = "ready"
    else:
        st = "preparing"
    return {"state": st, "message": worker.message if worker else "", "playable": playable, "early_start": early,
            "pool_target": CONFIG["pool"]["target_size"]}
