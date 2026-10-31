"""Playlists and the shared track library — SPEC.md §18.1 (v5).

A playlist holds library tracks; a track can be in several playlists. Stars (1–5) on a playlist track make it a
seed of that playlist; no stars = not a seed. Tracks Added in Find music sit in an "Added — waiting for stars"
section (status 'added') until they're starred or removed.

Importing a Spotify playlist reads its public page (§15.1), finds each track on YouTube Music (same song, length
within tolerance), downloads it into library/ and analyses it (loudness, MAEST fingerprint, BPM, cover art).
Tracks already in the library (same Spotify ID or YouTube video) are reused, not downloaded again.
"""

import shutil
import sqlite3

import requests

from . import db, names
from .config import CONFIG, path, relative, resolve

TOLERANCE = CONFIG["sources"]["duration_tolerance_seconds"]


# --- playlists ---------------------------------------------------------------------------

def create_playlist(conn: sqlite3.Connection, name: str, spotify_id: str | None = None,
                    description: str | None = None) -> int:
    name = (name or "").strip() or "Untitled playlist"
    with conn:
        return conn.execute("INSERT INTO playlists (name, spotify_id, description) VALUES (?, ?, ?)",
                            (name, spotify_id, description)).lastrowid


def rename_playlist(conn: sqlite3.Connection, playlist_id: int, name: str) -> None:
    with conn:
        conn.execute("UPDATE playlists SET name = ? WHERE id = ?", ((name or "").strip() or "Untitled playlist",
                                                                    playlist_id))


def delete_playlist(conn: sqlite3.Connection, playlist_id: int) -> None:
    """Delete a playlist; audio of tracks left in no playlist is deleted (fingerprints are kept)."""
    tracks = [r[0] for r in conn.execute("SELECT track_id FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))]
    with conn:
        conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
        conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
    for tid in tracks:
        _drop_if_orphaned(conn, tid)


def list_playlists(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT p.id, p.name, p.description, p.image_path IS NOT NULL AS has_image,"
        " count(pt.track_id) AS tracks, count(pt.stars) AS seeds,"
        " sum(pt.status = 'added') AS waiting"
        " FROM playlists p LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id GROUP BY p.id ORDER BY p.created_at")]


def playlist_tracks(conn: sqlite3.Connection, playlist_id: int) -> list[dict]:
    """Added (waiting for stars) first, newest first; then by stars, 5 at the top and unstarred last."""
    return [dict(r) for r in conn.execute(
        "SELECT t.id, t.artist, t.title, t.duration_s, t.gain_db, t.bpm, t.release_date,"
        " t.art_path IS NOT NULL AS has_art, t.file_path IS NOT NULL AS has_audio, pt.stars, pt.status, pt.added_at"
        " FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id WHERE pt.playlist_id = ?"
        " ORDER BY pt.status = 'added' DESC, CASE WHEN pt.status = 'added' THEN pt.added_at END DESC,"
        " pt.stars IS NULL, pt.stars DESC, pt.added_at", (playlist_id,))]


def set_stars(conn: sqlite3.Connection, playlist_id: int, track_id: int, stars: int | None) -> None:
    """1–5 stars (a seed), or None (not a seed). Starring an Added track moves it into the main list."""
    if stars is not None and stars not in range(1, 6):
        raise ValueError("Stars must be 1–5, or none")
    with conn:
        n = conn.execute("UPDATE playlist_tracks SET stars = ?, status = CASE WHEN ? IS NULL THEN status ELSE 'member' END"
                         " WHERE playlist_id = ? AND track_id = ?", (stars, stars, playlist_id, track_id)).rowcount
    if not n:
        raise ValueError("That track isn't in this playlist")


def add_to_playlist(conn: sqlite3.Connection, playlist_id: int, track_id: int, status: str = "member") -> bool:
    """Add a library track to a playlist (no stars). False if it was already there."""
    if not conn.execute("SELECT 1 FROM playlists WHERE id = ?", (playlist_id,)).fetchone():
        raise ValueError("No such playlist")
    with conn:
        return conn.execute("INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, status) VALUES (?, ?, ?)",
                            (playlist_id, track_id, status)).rowcount > 0


def remove_from_playlist(conn: sqlite3.Connection, playlist_id: int, track_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?", (playlist_id, track_id))
    _drop_if_orphaned(conn, track_id)


def _drop_if_orphaned(conn: sqlite3.Connection, track_id: int) -> None:
    """A library track in no playlist loses its audio (the fingerprint and ratings stay for learning)."""
    if conn.execute("SELECT 1 FROM playlist_tracks WHERE track_id = ?", (track_id,)).fetchone():
        return
    t = conn.execute("SELECT file_path FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if t and t["file_path"] and t["file_path"].startswith(CONFIG["paths"]["library"] + "/"):
        resolve(t["file_path"]).unlink(missing_ok=True)
        with conn:
            conn.execute("UPDATE tracks SET file_path = NULL WHERE id = ?", (track_id,))


# --- importing from Spotify ----------------------------------------------------------------

def spotify_playlist_info(link: str) -> dict:
    """{'kind', 'id', 'name', 'description', 'image'} for a Spotify playlist or track link."""
    from . import spotify

    kind, sid = spotify.parse(link)
    if kind == "track":
        return {"kind": kind, "id": sid, "name": None, "description": None, "image": None}
    page = spotify._page(f"https://open.spotify.com/playlist/{sid}")
    return {"kind": kind, "id": sid, "name": spotify._meta(page, "og:title"),
            "description": spotify._meta(page, "og:description"), "image": spotify._meta(page, "og:image")}


def save_cover(conn: sqlite3.Connection, playlist_id: int, image_url: str | None) -> None:
    if not image_url:
        return
    try:
        resp = requests.get(image_url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        db.log_event(conn, "cover_failed", playlist_id=playlist_id, reason=str(e)[:200])
        return
    folder = path("library") / "covers"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{playlist_id}.jpg"
    dest.write_bytes(resp.content)
    with conn:
        conn.execute("UPDATE playlists SET image_path = ? WHERE id = ?", (relative(dest), playlist_id))


def match_on_youtube(yt, artists: list[str], title: str, duration_s: float):
    """The same song on YouTube Music, length within tolerance: (best match, None) or (None, reason)."""
    target = names.song(artists, title)
    results = yt.search_songs(f"{artists[0]} {title}")
    same = [r for r in results if r.duration_s and names.song(r.artist, r.title).same_song(target)]
    close = [r for r in same if abs(r.duration_s - duration_s) <= TOLERANCE]
    if not close:
        return None, ("not on YouTube Music under this artist and title" if not same else
                      f"length differs (Spotify {duration_s:.0f}s; YouTube Music "
                      + ", ".join(f"{r.duration_s:.0f}s" for r in same) + ")")
    return min(close, key=lambda r: abs(r.duration_s - duration_s)), None


def keep_art(art_path: str | None) -> str | None:
    """A library copy of a cover (library/art/), so it survives the cache being cleared. Returns the new path."""
    if not art_path or not resolve(art_path).exists():
        return art_path
    if art_path.startswith(CONFIG["paths"]["library"] + "/"):
        return art_path
    dest = path("library") / "art" / resolve(art_path).name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(resolve(art_path), dest)
    return relative(dest)


def store(conn: sqlite3.Connection, res, file, artist: str, title: str, origin: str, **extra) -> int:
    """Add an analysed track (analysis.Result, its audio now at `file`) to the library."""
    for what, reason in res.notes.items():
        db.log_event(conn, what, video_id=extra.get("video_id"), reason=reason)
    art = keep_art(res.art and relative(res.art))
    if art and res.art and resolve(art) != res.art:
        res.art.unlink(missing_ok=True)
    cols = {"artist": artist, "title": title, "file_path": relative(file), "duration_s": res.duration_s,
            "origin": origin, "loudness_lufs": res.lufs, "true_peak_db": res.peak, "gain_db": res.gain, "bpm": res.bpm,
            "art_path": art, **extra}
    cols["release_date"] = cols.get("release_date") or res.published
    with conn:
        tid = conn.execute(f"INSERT INTO tracks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                           list(cols.values())).lastrowid
        db.store_embedding(conn, tid, res.model, res.embedding)
    return tid


def import_spotify(conn: sqlite3.Connection, link: str, playlist_id: int, progress=None) -> dict:
    """Import a Spotify playlist's (or track's) tracks into a playlist. Safe to re-run: tracks already in the
    library are reused. `progress(done, total, message)` is told how far it's got. Returns
    {'added', 'reused', 'failures': [{'artist', 'title', 'reason'}]}."""
    from . import spotify
    from .analysis import AnalysisFailed, DownloadFailed, backend
    from .ratings import _safe, _unique
    from .sources import YouTubeMusic

    report = lambda done, total, msg: progress and progress(done, total, msg)
    ids = spotify.expand([link])
    yt, folder = None, path("library")
    folder.mkdir(parents=True, exist_ok=True)
    out = {"added": 0, "reused": 0, "failures": []}
    for i, sid in enumerate(ids):
        report(i, len(ids), "Reading Spotify")
        row = conn.execute("SELECT id FROM tracks WHERE spotify_id = ? ORDER BY id LIMIT 1", (sid,)).fetchone()
        if row:
            out["reused"] += add_to_playlist(conn, playlist_id, row[0])
            continue
        try:
            t = spotify.track(sid)
        except Exception as e:
            out["failures"].append({"artist": "?", "title": f"spotify:{sid}", "reason": f"couldn't read Spotify ({e})"})
            continue
        artist = ", ".join(t.artists)
        report(i, len(ids), f"{artist} – {t.title}")
        yt = yt or YouTubeMusic()
        best, reason = match_on_youtube(yt, t.artists, t.title, t.duration_s)
        if best is None:
            out["failures"].append({"artist": artist, "title": t.title, "reason": reason})
            db.log_event(conn, "import_failed", playlist_id=playlist_id, spotify_id=sid, reason=reason)
            continue
        row = conn.execute("SELECT id FROM tracks WHERE video_id = ? AND file_path IS NOT NULL ORDER BY id LIMIT 1",
                           (best.video_id,)).fetchone()
        if row:                                          # already downloaded (e.g. a pool track): reuse it
            out["reused"] += add_to_playlist(conn, playlist_id, row[0])
            continue
        try:
            res = backend().analyse(best.video_id, status=lambda text: report(i, len(ids), f"{artist} – {t.title}: {text}"))
        except DownloadFailed as e:          # (or AnalysisFailed)
            what = "analysis" if isinstance(e, AnalysisFailed) else "download"
            out["failures"].append({"artist": artist, "title": t.title, "reason": f"{what} failed ({str(e)[:120]})"})
            continue
        dest = _unique(folder / f"{_safe(artist)} - {_safe(t.title)}{res.file.suffix}", res.file)
        shutil.move(res.file, dest)
        tid = store(conn, res, dest, artist, t.title, "library", video_id=best.video_id, spotify_id=sid,
                    release_date=t.release_date)
        add_to_playlist(conn, playlist_id, tid)
        out["added"] += 1
        db.log_event(conn, "track_imported", playlist_id=playlist_id, track_id=tid, spotify_id=sid,
                     video_id=best.video_id)
    report(len(ids), len(ids), "Done")
    return out
