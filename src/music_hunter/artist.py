"""Artist history as a part of the veto score (SPEC.md §18.29).

Every rating of a song by one of a track's credited artists counts — Add +1, Close 0, Off −1 — and the artist value is
their plain average, once there are artist_min_ratings of them. A track with several artists pools the ratings of all
of them (each rating once, even if it shares several of the artists); the track's own song never counts. Ratings
come from every playlist's Find music on this machine and from archived runs (`artist_history`, imported with
`music-hunter artist-history`).
"""

import sqlite3
from pathlib import Path

import numpy as np

from . import db, names
from .config import CONFIG, path

VALUE = {1: -1, 2: 0, 3: 1}                 # Off, Close, Add (v5 sessions)


def _db_file(conn: sqlite3.Connection) -> Path | None:
    f = conn.execute("PRAGMA database_list").fetchone()[2]
    return Path(f).resolve() if f else None


def history(conn: sqlite3.Connection) -> list[tuple[frozenset, str, int]]:
    """Every rating known: (artists, song title key, value)."""
    out = []

    def add(artist, title, value):
        s = names.song(artist, title)
        if s.artists:
            out.append((s.artists, s.title, value))

    seen = set()
    sources = [conn] + [sqlite3.connect(f, timeout=30) for f in sorted((path("data") / "sessions").glob("playlist-*.db"))]
    for c in sources:
        f = _db_file(c)
        if f in seen:
            continue
        seen.add(f)
        if c.execute("SELECT name FROM sqlite_master WHERE name = 'plays'").fetchone():
            for artist, title, rating in c.execute(
                    "SELECT t.artist, t.title, p.rating FROM plays p JOIN tracks t ON t.id = p.track_id"
                    " WHERE p.rating IS NOT NULL"):
                if rating in VALUE:
                    add(artist, title, VALUE[rating])
        if c is not conn:
            c.close()
    for artist, title, value in db.connect_library().execute("SELECT artist, title, value FROM artist_history"):
        add(artist, title, value)
    return out


def values(conn: sqlite3.Connection, tracks) -> tuple[np.ndarray, np.ndarray]:
    """(artist value or NaN, number of ratings it rests on) for each (artist, title)."""
    hist = history(conn)
    by_artist: dict[str, list[int]] = {}
    for i, (artists, _, _) in enumerate(hist):
        for a in artists:
            by_artist.setdefault(a, []).append(i)
    vals, counts = np.full(len(tracks), np.nan), np.zeros(len(tracks), int)
    for k, (artist, title) in enumerate(tracks):
        s = names.song(artist, title)
        idx = {i for a in s.artists for i in by_artist.get(a, ()) if hist[i][1] != s.title}
        counts[k] = len(idx)
        if len(idx) >= CONFIG["veto"]["artist_min_ratings"]:
            vals[k] = np.mean([hist[i][2] for i in idx])
    return vals, counts


def archived(folder: Path, source: str) -> list[dict]:
    """Ratings of an archived run (runs/v1–v3), as Add +1 / Close 0 / Off −1 (v1: 4–5 / 3 / 1–2; v2: 4 / 2–3 / 1)."""
    f = folder / "data" / "music_hunter.db"
    if not f.exists():
        return []
    c = sqlite3.connect(f)
    top = c.execute("SELECT max(rating) FROM plays").fetchone()[0] or 3
    def val(r):
        if top >= 5:
            return -1 if r <= 2 else (0 if r == 3 else 1)
        if top == 4:
            return -1 if r == 1 else (1 if r == 4 else 0)
        return VALUE[r]
    rows = [{"source": source, "artist": a, "title": t, "value": val(r), "rated_at": at} for a, t, r, at in c.execute(
        "SELECT t.artist, t.title, p.rating, p.rated_at FROM plays p JOIN tracks t ON t.id = p.track_id"
        " WHERE p.rating IS NOT NULL ORDER BY p.id")]
    c.close()
    return rows


def store(lib: sqlite3.Connection, rows: list[dict]) -> int:
    """Replace the archived history of each source in `rows`."""
    with lib:
        for src in {r["source"] for r in rows}:
            lib.execute("DELETE FROM artist_history WHERE source = ?", (src,))
        lib.executemany("INSERT INTO artist_history (source, artist, title, value, rated_at)"
                        " VALUES (:source, :artist, :title, :value, :rated_at)", rows)
    return len(rows)
