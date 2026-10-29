"""Original seeds — SPEC.md §2 and §15.1.

Seeds come from Spotify links (matched on YouTube Music, downloaded into seeds/) or
from local files placed in seeds/. Either way they are embedded with CLAP and stored
as original references (treated as the top rating).
"""

import shutil
import sqlite3
from pathlib import Path

import numpy as np

from . import audio, db, names
from .config import CONFIG, path, relative
from .ratings import ADD, _safe, _unique

TOLERANCE = CONFIG["sources"]["duration_tolerance_seconds"]


def _store(conn, embedder, file: Path, artist: str, title: str, **extra) -> int:
    sr = CONFIG["clap"]["sample_rate"]
    samples = audio.decode(file, sr)
    emb = embedder.embed_track(samples)
    from .bpm import estimate
    cols = {"artist": artist, "title": title, "file_path": relative(file), "duration_s": len(samples) / sr,
            "origin": "original_reference", "rating": ADD, "bpm": estimate(samples, sr), **extra}
    with conn:
        tid = conn.execute(f"INSERT INTO tracks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                           list(cols.values())).lastrowid
        db.store_embedding(conn, tid, embedder.model_name, emb)
        if extra.get("video_id"):
            conn.execute("INSERT OR REPLACE INTO seed_matches (track_id, video_id, yt_artist, yt_title, yt_duration_s)"
                         " VALUES (?, ?, ?, ?, ?)", (tid, extra["video_id"], artist, title, len(samples) / sr))
    print(f"  seed added: {artist} – {title}  ({len(samples) / sr / 60:.1f} min, {len(emb.chunks)} chunks)")
    return tid


def refresh_seed_bpms(conn: sqlite3.Connection) -> None:
    """Seeds' BPM in two passes: plain readings give the median, then each seed is re-read with
    that median as the 3:2 tie-break (one misread seed barely moves the median)."""
    from .bpm import estimate
    from .config import resolve

    sr = CONFIG["clap"]["sample_rate"]
    rows = conn.execute("SELECT id, artist, title, file_path FROM tracks WHERE origin = 'original_reference'"
                        " AND file_path IS NOT NULL").fetchall()
    decoded = {r["id"]: audio.decode(resolve(r["file_path"]), sr) for r in rows}
    plain = {tid: estimate(y, sr) for tid, y in decoded.items()}
    known = [b for b in plain.values() if b]
    around = float(np.median(known)) if known else None
    for r in rows:
        tempo = estimate(decoded[r["id"]], sr, around)
        with conn:
            conn.execute("UPDATE tracks SET bpm = ? WHERE id = ?", (tempo, r["id"]))
        note = f" (plain reading {plain[r['id']]})" if plain[r["id"]] and tempo and abs(plain[r["id"]] - tempo) > 1 else ""
        print(f"  {tempo:>7} BPM  {r['artist']} – {r['title']}{note}")


def add_from_spotify(conn: sqlite3.Connection, links: list[str]) -> None:
    from . import spotify
    from .clap import Embedder
    from .pool import download
    from .sources import YouTubeMusic

    yt, embedder = YouTubeMusic(), None
    folder = path("seeds")
    folder.mkdir(exist_ok=True)
    for sid in spotify.expand(links):
        if conn.execute("SELECT 1 FROM tracks WHERE spotify_id = ?", (sid,)).fetchone():
            print(f"  already a seed: spotify:{sid}")
            continue
        try:
            t = spotify.track(sid)
        except Exception as e:
            db.log_event(conn, "seed_failed", spotify_id=sid, reason=f"spotify: {e}")
            print(f"  FAILED spotify:{sid}: {e}")
            continue
        artist = ", ".join(t.artists)
        target = names.song(t.artists, t.title)
        results = yt.search_songs(f"{t.artists[0]} {t.title}")
        same = [r for r in results if r.duration_s and names.song(r.artist, r.title).same_song(target)]
        close = [r for r in same if abs(r.duration_s - t.duration_s) <= TOLERANCE]
        if not close:
            reason = "no_name_match" if not same else (
                f"duration_mismatch (Spotify {t.duration_s:.0f}s; YouTube Music "
                + ", ".join(f"{r.duration_s:.0f}s" for r in same) + ")")
            db.log_event(conn, "seed_failed", spotify_id=sid, artist=artist, title=t.title, reason=reason)
            print(f"  FAILED {artist} – {t.title}: {reason}")
            continue
        best = min(close, key=lambda r: abs(r.duration_s - t.duration_s))
        downloaded = download(best.video_id)
        dest = _unique(folder / f"{_safe(artist)} - {_safe(t.title)}{downloaded.suffix}", downloaded)
        shutil.move(downloaded, dest)
        embedder = embedder or Embedder()
        tid = _store(conn, embedder, dest, artist, t.title, video_id=best.video_id, spotify_id=sid,
                     release_date=t.release_date)
        db.log_event(conn, "seed_added", track_id=tid, spotify_id=sid, video_id=best.video_id,
                     spotify_duration_s=t.duration_s, youtube_duration_s=best.duration_s)
    refresh_seed_bpms(conn)


def add_local_files(conn: sqlite3.Connection, force: bool = False) -> None:
    """Embed audio files placed in seeds/ by hand (tagged, or named 'Artist - Title')."""
    from .clap import Embedder

    folder = path("seeds")
    folder.mkdir(exist_ok=True)
    embedder = None
    for file in audio.audio_files(folder):
        row = conn.execute("SELECT id FROM tracks WHERE file_path = ?", (relative(file),)).fetchone()
        if row and not force:
            continue
        if row:
            with conn:
                conn.execute("DELETE FROM chunk_embeddings WHERE track_id = ?", (row["id"],))
                conn.execute("DELETE FROM track_embeddings WHERE track_id = ?", (row["id"],))
                conn.execute("DELETE FROM seed_matches WHERE track_id = ?", (row["id"],))
                conn.execute("DELETE FROM tracks WHERE id = ?", (row["id"],))
        artist, title = audio.artist_title(file)
        embedder = embedder or Embedder()
        _store(conn, embedder, file, artist, title)
    if embedder:
        refresh_seed_bpms(conn)
