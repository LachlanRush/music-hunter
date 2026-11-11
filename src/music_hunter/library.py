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
import subprocess

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
    tracks = [r[0] for r in conn.execute("SELECT track_id FROM playlist_tracks WHERE playlist_id = ? UNION"
                                         " SELECT track_id FROM deleted_tracks WHERE playlist_id = ?",
                                         (playlist_id, playlist_id))]
    with conn:
        conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
        conn.execute("DELETE FROM deleted_tracks WHERE playlist_id = ?", (playlist_id,))
        conn.execute("DELETE FROM removed_spotify WHERE playlist_id = ?", (playlist_id,))
        conn.execute("DELETE FROM removed_tracks WHERE playlist_id = ?", (playlist_id,))
        conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
    for tid in tracks:
        _drop_if_orphaned(conn, tid)


def list_playlists(conn: sqlite3.Connection) -> list[dict]:
    return [{**dict(r), "image": cover_url(r["id"], r["image_path"])} for r in conn.execute(
        "SELECT p.id, p.name, p.description, p.image_path,"
        " count(pt.track_id) AS tracks, count(pt.stars) AS seeds,"
        " sum(pt.status = 'added') AS waiting"
        " FROM playlists p LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id GROUP BY p.id ORDER BY p.created_at")]


def playlist_tracks(conn: sqlite3.Connection, playlist_id: int) -> list[dict]:
    """Added (waiting for stars) first, newest first; then by stars, 5 at the top and unstarred last; within the same
    stars, the most recently starred first (then the most recently added)."""
    return [dict(r) for r in conn.execute(
        "SELECT t.id, t.artist, t.title, t.duration_s, t.gain_db, t.bpm, t.release_date,"
        " t.art_path IS NOT NULL AS has_art, t.file_path IS NOT NULL AS has_audio, pt.stars, pt.status, pt.added_at,"
        " pt.bpm_override, t.quality"
        " FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id WHERE pt.playlist_id = ?"
        " ORDER BY pt.status = 'added' DESC, CASE WHEN pt.status = 'added' THEN pt.added_at END DESC,"
        " pt.stars IS NULL, pt.stars DESC, coalesce(pt.starred_at, pt.added_at) DESC, pt.added_at DESC, t.id DESC",
        (playlist_id,))]


def set_stars(conn: sqlite3.Connection, playlist_id: int, track_id: int, stars: int | None) -> None:
    """1–5 stars (a seed), or None (not a seed). Starring an Added track moves it into the main list."""
    if stars is not None and stars not in range(1, 6):
        raise ValueError("Stars must be 1–5, or none")
    with conn:
        n = conn.execute("UPDATE playlist_tracks SET stars = ?, status = CASE WHEN ? IS NULL THEN status ELSE 'member' END,"
                         " starred_at = strftime('%Y-%m-%d %H:%M:%f', 'now')"
                         " WHERE playlist_id = ? AND track_id = ?", (stars, stars, playlist_id, track_id)).rowcount
    if not n:
        raise ValueError("That track isn't in this playlist")
    db.log_event(conn, "stars_changed", playlist_id=playlist_id, track_id=track_id, stars=stars)   # for replays


def add_to_playlist(conn: sqlite3.Connection, playlist_id: int, track_id: int, status: str = "member") -> bool:
    """Add a library track to a playlist (no stars). False if it was already there."""
    if not conn.execute("SELECT 1 FROM playlists WHERE id = ?", (playlist_id,)).fetchone():
        raise ValueError("No such playlist")
    with conn:
        conn.execute("DELETE FROM deleted_tracks WHERE playlist_id = ? AND track_id = ?", (playlist_id, track_id))
        conn.execute("DELETE FROM removed_tracks WHERE playlist_id = ? AND track_id = ?", (playlist_id, track_id))
        return conn.execute("INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, status) VALUES (?, ?, ?)",
                            (playlist_id, track_id, status)).rowcount > 0


def remove_from_playlist(conn: sqlite3.Connection, playlist_id: int, track_id: int) -> None:
    """Move a track to the playlist's Recently deleted list (stars and audio kept, restorable)."""
    with conn:
        n = conn.execute("INSERT OR REPLACE INTO deleted_tracks (playlist_id, track_id, stars, status, added_at,"
                         " starred_at, bpm_override) SELECT playlist_id, track_id, stars, status, added_at, starred_at,"
                         " bpm_override FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
                         (playlist_id, track_id)).rowcount
        conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?", (playlist_id, track_id))
    if not n:
        raise ValueError("That track isn't in this playlist")


def restore_track(conn: sqlite3.Connection, playlist_id: int, track_id: int) -> None:
    """Put a recently deleted track back, with its stars."""
    with conn:
        n = conn.execute("INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, stars, status, added_at,"
                         " starred_at, bpm_override) SELECT playlist_id, track_id, stars, status,"
                         " coalesce(added_at, strftime('%Y-%m-%d %H:%M:%f', 'now')), starred_at, bpm_override"
                         " FROM deleted_tracks WHERE playlist_id = ? AND track_id = ?", (playlist_id, track_id)).rowcount
        conn.execute("DELETE FROM deleted_tracks WHERE playlist_id = ? AND track_id = ?", (playlist_id, track_id))
    if not n:
        raise ValueError("That track isn't in Recently deleted")


def delete_forever(conn: sqlite3.Connection, playlist_id: int, track_id: int) -> None:
    """Remove a recently deleted track for good: its audio goes once it's in no playlist (fingerprint kept), and a
    refresh from Spotify won't bring it back to this playlist."""
    sid = conn.execute("SELECT spotify_id FROM tracks WHERE id = ?", (track_id,)).fetchone()
    with conn:
        conn.execute("DELETE FROM deleted_tracks WHERE playlist_id = ? AND track_id = ?", (playlist_id, track_id))
        conn.execute("INSERT OR IGNORE INTO removed_tracks (playlist_id, track_id) VALUES (?, ?)", (playlist_id, track_id))
        if sid and sid[0]:
            conn.execute("INSERT OR IGNORE INTO removed_spotify (playlist_id, spotify_id) VALUES (?, ?)",
                         (playlist_id, sid[0]))
    _drop_if_orphaned(conn, track_id)


def similar_playlist(conn: sqlite3.Connection, track_id: int) -> int:
    """"Find similar" (feedback #39): a new playlist "<title> radio" with this track at 5 stars, and the closest-sounding
    tracks from my playlists in its To rate section. Closeness is the MAEST fingerprint's cosine similarity alone
    (stars play no part, 2026-11-11)."""
    import numpy as np

    t = conn.execute("SELECT t.artist, t.title, t.art_path, e.embedding FROM tracks t"
                     " LEFT JOIN track_embeddings e ON e.track_id = t.id WHERE t.id = ?", (track_id,)).fetchone()
    if not t:
        raise ValueError("No such track")
    rows = conn.execute(
        "SELECT t.id, t.artist, t.title, e.embedding FROM playlist_tracks pt"
        " JOIN tracks t ON t.id = pt.track_id JOIN track_embeddings e ON e.track_id = t.id"
        " WHERE t.id != ? GROUP BY t.id", (track_id,)).fetchall() if t["embedding"] else []
    me = names.song(t["artist"], t["title"])
    rows = [r for r in rows if not names.song(r["artist"], r["title"]).same_song(me)]   # not another version of it
    picks = []
    if rows:
        sims = np.stack([db.from_blob(r["embedding"]) for r in rows]) @ db.from_blob(t["embedding"])
        picks = [rows[i]["id"] for i in np.argsort(-sims)[:CONFIG["library"]["similar_count"]]]

    pid = create_playlist(conn, f"{t['title']} radio")
    add_to_playlist(conn, pid, track_id)
    set_stars(conn, pid, track_id, 5)
    for rank, i in enumerate(picks):             # To rate shows the newest first: the closest at the top
        add_to_playlist(conn, pid, i, "added")
        with conn:
            conn.execute("UPDATE playlist_tracks SET added_at = strftime('%Y-%m-%d %H:%M:%f', 'now', ?)"
                         " WHERE playlist_id = ? AND track_id = ?", (f"-{rank} seconds", pid, i))
    if t["art_path"] and resolve(t["art_path"]).exists():
        try:
            set_cover(conn, pid, resolve(t["art_path"]).read_bytes())
        except (subprocess.SubprocessError, OSError):
            pass                                 # the playlist works without a picture
    db.log_event(conn, "similar_playlist", playlist_id=pid, track_id=track_id, picks=picks)
    return pid


def deleted_tracks(conn: sqlite3.Connection, playlist_id: int) -> list[dict]:
    purge_deleted(conn)
    return [dict(r) for r in conn.execute(
        "SELECT t.id, t.artist, t.title, t.duration_s, t.art_path IS NOT NULL AS has_art, d.stars, d.deleted_at"
        " FROM deleted_tracks d JOIN tracks t ON t.id = d.track_id WHERE d.playlist_id = ? ORDER BY d.deleted_at DESC",
        (playlist_id,))]


def purge_deleted(conn: sqlite3.Connection) -> int:
    """Delete for good everything in Recently deleted for longer than [library] deleted_days."""
    old = conn.execute("SELECT playlist_id, track_id FROM deleted_tracks WHERE deleted_at < datetime('now', ?)",
                       (f"-{CONFIG['library']['deleted_days']} days",)).fetchall()
    for pid, tid in old:
        delete_forever(conn, pid, tid)
    return len(old)


def _drop_if_orphaned(conn: sqlite3.Connection, track_id: int) -> None:
    """A library track in no playlist (nor any Recently deleted list) loses its audio (fingerprint and ratings stay)."""
    if conn.execute("SELECT 1 FROM playlist_tracks WHERE track_id = ? UNION SELECT 1 FROM deleted_tracks"
                    " WHERE track_id = ?", (track_id, track_id)).fetchone():
        return
    t = conn.execute("SELECT file_path FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if t and t["file_path"] and t["file_path"].startswith(CONFIG["paths"]["library"] + "/"):
        with conn:
            conn.execute("UPDATE tracks SET file_path = NULL WHERE id = ?", (track_id,))
        db.release_audio(conn, t["file_path"])        # unless a Find music session still plays it (e.g. back)


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


def cover_type(data: bytes) -> str | None:
    """The picture formats accepted for a playlist, by their first bytes."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def set_cover(conn: sqlite3.Connection, playlist_id: int, data: bytes) -> None:
    """A picture I chose for the playlist: square and small, cut from the middle, as the page would show it."""
    size = CONFIG["library"]["cover_size"]
    folder = path("library") / "covers"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{playlist_id}.jpg"
    tmp = folder / f"{playlist_id}.part.jpg"
    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "image2pipe", "-i", "pipe:0",
             "-vf", f"scale={size}:{size}:force_original_aspect_ratio=increase,crop={size}:{size}",
             "-frames:v", "1", "-q:v", "3", "-update", "1", str(tmp)],
            input=data, check=True, capture_output=True, timeout=60)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    with conn:
        conn.execute("UPDATE playlists SET image_path = ? WHERE id = ?", (relative(dest), playlist_id))


def cover_url(playlist_id: int, image_path: str | None) -> str | None:
    """Changes when the picture does, so nothing keeps showing the old one."""
    if not image_path or not resolve(image_path).exists():
        return None
    return f"/cover/{playlist_id}?v={resolve(image_path).stat().st_mtime_ns}"


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


def import_spotify(conn: sqlite3.Connection, link: str, playlist_id: int, progress=None, skip_removed: bool = True,
                   status: str = "member") -> dict:
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
    out = {"added": 0, "reused": 0, "skipped": 0, "failures": []}
    removed = {r[0] for r in conn.execute(
        "SELECT spotify_id FROM removed_spotify WHERE playlist_id = ? UNION SELECT t.spotify_id FROM deleted_tracks d"
        " JOIN tracks t ON t.id = d.track_id WHERE d.playlist_id = ? AND t.spotify_id IS NOT NULL",
        (playlist_id, playlist_id))} if skip_removed else set()
    for i, sid in enumerate(ids):
        report(i, len(ids), "Reading Spotify")
        if sid in removed:                               # removed from this playlist: a refresh leaves it out
            out["skipped"] += 1
            continue
        row = conn.execute("SELECT id FROM tracks WHERE spotify_id = ? ORDER BY id LIMIT 1", (sid,)).fetchone()
        if row:
            out["reused"] += add_to_playlist(conn, playlist_id, row[0], status)
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
            out["reused"] += add_to_playlist(conn, playlist_id, row[0], status)
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
        add_to_playlist(conn, playlist_id, tid, status)
        out["added"] += 1
        db.log_event(conn, "track_imported", playlist_id=playlist_id, track_id=tid, spotify_id=sid,
                     video_id=best.video_id)
    report(len(ids), len(ids), "Done")
    return out


def youtube_video_id(link: str) -> str | None:
    """The video ID in a YouTube / YouTube Music link (watch?v=…, youtu.be/…), or None."""
    import re
    from urllib.parse import parse_qs, urlparse

    u = urlparse(link.strip())
    host = (u.hostname or "").lower()
    if host.endswith("youtu.be"):
        vid = u.path.lstrip("/").split("/")[0]
    elif "youtube.com" in host:
        vid = parse_qs(u.query).get("v", [""])[0]
    else:
        return None
    return vid if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid or "") else None


def add_song(conn: sqlite3.Connection, link: str, playlist_id: int, progress=None) -> dict:
    """Add one song to a playlist from a Spotify track link or a YouTube / YouTube Music link (added by hand, so it
    comes back even if it was removed from this playlist before). It lands in "To rate", like every added track."""
    from .analysis import AnalysisFailed, DownloadFailed, backend
    from .ratings import _safe, _unique
    from .sources import YouTubeMusic

    vid = youtube_video_id(link)
    if vid is None:
        return import_spotify(conn, link, playlist_id, progress, skip_removed=False, status="added")
    report = lambda done, total, msg: progress and progress(done, total, msg)
    out = {"added": 0, "reused": 0, "skipped": 0, "failures": []}
    row = conn.execute("SELECT id FROM tracks WHERE video_id = ? AND file_path IS NOT NULL ORDER BY id LIMIT 1",
                       (vid,)).fetchone()
    if row:
        out["reused"] += add_to_playlist(conn, playlist_id, row[0], "added")
        report(1, 1, "Done")
        return out
    yt = YouTubeMusic()
    details = yt.api.get_song(vid).get("videoDetails", {})
    title = details.get("title") or vid
    artist = (details.get("author") or "").removesuffix(" - Topic") or "Unknown artist"
    report(0, 1, f"{artist} – {title}")
    try:
        res = backend().analyse(vid, status=lambda text: report(0, 1, f"{artist} – {title}: {text}"))
    except DownloadFailed as e:
        what = "analysis" if isinstance(e, AnalysisFailed) else "download"
        out["failures"].append({"artist": artist, "title": title, "reason": f"{what} failed ({str(e)[:120]})"})
        return out
    folder = path("library")
    folder.mkdir(parents=True, exist_ok=True)
    dest = _unique(folder / f"{_safe(artist)} - {_safe(title)}{res.file.suffix}", res.file)
    shutil.move(res.file, dest)
    tid = store(conn, res, dest, artist, title, "library", video_id=vid)
    add_to_playlist(conn, playlist_id, tid, "added")
    out["added"] += 1
    db.log_event(conn, "track_imported", playlist_id=playlist_id, track_id=tid, video_id=vid)
    report(1, 1, "Done")
    return out
