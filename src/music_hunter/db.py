"""SQLite storage. Embeddings are stored as float32 BLOBs alongside the model that produced them."""

import json
import logging
import re
import sqlite3
from contextvars import ContextVar
from pathlib import Path

import numpy as np

from .config import path

log = logging.getLogger("music_hunter")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id          INTEGER PRIMARY KEY,
    artist      TEXT NOT NULL,
    title       TEXT NOT NULL,
    file_path   TEXT,               -- NULL once audio is deleted (ratings 1-2)
    duration_s  REAL,
    origin      TEXT NOT NULL,      -- 'original_reference' | 'pool' (downloaded candidate)
    rating      INTEGER,            -- 1-5; originals are treated as 5
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    -- pool tracks only:
    candidate_id  INTEGER REFERENCES candidates(id),
    generation    INTEGER REFERENCES generations(id),
    video_id      TEXT,
    loudness_lufs REAL,             -- integrated loudness (EBU R128)
    true_peak_db  REAL,             -- dBTP
    gain_db       REAL,             -- playback gain to reach [loudness] target_lufs
    -- seeds from Spotify links (v2):
    spotify_id    TEXT,
    release_date  TEXT,
    art_path      TEXT,             -- cover art (pool tracks), shown only after rating
    bpm           REAL              -- measured from the audio (v2), folded into [bpm] min–max
);
CREATE UNIQUE INDEX IF NOT EXISTS tracks_file_path ON tracks(file_path);

CREATE TABLE IF NOT EXISTS track_embeddings (
    track_id    INTEGER PRIMARY KEY REFERENCES tracks(id),
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    n_chunks    INTEGER NOT NULL,
    embedding   BLOB NOT NULL       -- mean of chunk embeddings, L2-normalised
);

CREATE TABLE IF NOT EXISTS chunk_embeddings (
    track_id    INTEGER NOT NULL REFERENCES tracks(id),
    chunk_index INTEGER NOT NULL,
    start_s     REAL NOT NULL,
    end_s       REAL NOT NULL,      -- end of real audio (before padding)
    embedding   BLOB NOT NULL,      -- L2-normalised
    PRIMARY KEY (track_id, chunk_index)
);

-- Seed -> YouTube Music match (SPEC.md §4.1 step 1). video_id NULL if matching failed.
CREATE TABLE IF NOT EXISTS seed_matches (
    track_id      INTEGER PRIMARY KEY REFERENCES tracks(id),
    video_id      TEXT,
    yt_artist     TEXT,
    yt_title      TEXT,
    yt_duration_s REAL,
    reason        TEXT,             -- why matching failed
    matched_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS generations (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    stats       TEXT                -- JSON: raw counts per seed/source, removals per reason, …
);

-- One row per distinct song that survived the filters.
CREATE TABLE IF NOT EXISTS candidates (
    id            INTEGER PRIMARY KEY,
    generation    INTEGER NOT NULL REFERENCES generations(id),
    video_id      TEXT,             -- chosen version (Extended preferred); NULL until matched (v3)
    artist        TEXT NOT NULL,
    title         TEXT NOT NULL,
    duration_s    REAL,
    song_title    TEXT NOT NULL,    -- normalised, version labels removed
    song_artists  TEXT NOT NULL,    -- JSON list, normalised
    is_extended   INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'candidate',  -- candidate | pool | download_failed | match_failed
                                                      -- | merged | not_selected
    fail_reason   TEXT,
    track_id      INTEGER REFERENCES tracks(id),      -- set once downloaded and analysed
    isrc          TEXT,                               -- from Deezer or TIDAL, when known
    release_date  TEXT,                               -- from Deezer, when known
    matched       INTEGER NOT NULL DEFAULT 1,         -- v3: 0 = still to be found on YouTube Music
    deezer_id     INTEGER,                            -- v3: for fetching ISRC and release date when selected
    pool_seed     INTEGER REFERENCES tracks(id)       -- v3: the seed whose share of the pool it fills
);

-- Every raw suggestion from every (seed, source), with what happened to it.
-- Provenance of a candidate = its rows here with candidate_id set.
CREATE TABLE IF NOT EXISTS suggestions (
    id            INTEGER PRIMARY KEY,
    generation    INTEGER NOT NULL REFERENCES generations(id),
    seed_track_id INTEGER NOT NULL REFERENCES tracks(id),
    source        TEXT NOT NULL,    -- 'lastfm' | 'ytmusic' | 'ytrelated' | 'deezer' | 'tidal'
    rank          INTEGER NOT NULL, -- position in the source's list (1-based)
    artist        TEXT NOT NULL,
    title         TEXT NOT NULL,
    via           TEXT,             -- Last.fm: similar artist it came through
    duration_s    REAL,             -- as given by the source (Last.fm: from track.getInfo)
    video_id      TEXT,             -- chosen YouTube Music ID after matching
    outcome       TEXT NOT NULL,    -- 'new' | 'merged' | 'dropped'
    reason        TEXT,             -- filter reason for merged/dropped; 'name_only_match' notes on new
    candidate_id  INTEGER REFERENCES candidates(id)
);
CREATE INDEX IF NOT EXISTS suggestions_candidate ON suggestions(candidate_id);

-- Every track shown (SPEC.md §10), with its scores as they were at that moment.
CREATE TABLE IF NOT EXISTS plays (
    id             INTEGER PRIMARY KEY,
    track_id       INTEGER NOT NULL REFERENCES tracks(id),
    generation     INTEGER,         -- latest generation when shown (drives decay)
    play_number    INTEGER NOT NULL,
    is_control     INTEGER NOT NULL,
    shown_at       TEXT NOT NULL DEFAULT (datetime('now')),
    weight_set_id  INTEGER REFERENCES weight_sets(id),
    components     TEXT NOT NULL,   -- JSON {component: {"raw": x, "norm": y}}
    score          REAL NOT NULL,
    rank           INTEGER NOT NULL,
    pool_size      INTEGER NOT NULL,
    provenance     TEXT NOT NULL,   -- JSON [[seed, source], …]
    rating         INTEGER,
    rated_at       TEXT,
    played_seconds REAL,            -- how long it played before rating (or skipping)
    skipped_at     TEXT,            -- skipped without a rating (shown, never re-shown; no learning effect)
    undo_path      TEXT             -- audio of a 1–2 rating, held until the next rating (mis-tap correction)
);

-- Every re-weighting (SPEC.md §9–10). eval_* filled once eval_window more learning ratings exist.
CREATE TABLE IF NOT EXISTS reweightings (
    id                   INTEGER PRIMARY KEY,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    at_ratings           INTEGER NOT NULL UNIQUE,  -- learning ratings used
    old_weight_set_id    INTEGER NOT NULL REFERENCES weight_sets(id),
    new_weight_set_id    INTEGER NOT NULL REFERENCES weight_sets(id),
    pairs                INTEGER NOT NULL,
    old_concordance      REAL,                     -- on the data used (decay-weighted)
    new_concordance      REAL,
    eval_n               INTEGER,
    eval_pairs           INTEGER,
    eval_old_concordance REAL,                     -- on the next eval_window learning ratings
    eval_new_concordance REAL,
    evaluated_at         TEXT,
    target               TEXT,                     -- v2: what the update moved towards ('current', 'search', 'set N')
    candidates           TEXT                      -- v2: JSON {target: {full, step}} objective scores
);

-- Operational log: timings, background jobs, errors, rating changes, player-side events.
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY,
    at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    kind     TEXT NOT NULL,
    play_id  INTEGER,
    data     TEXT                       -- JSON
);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, at);

-- Listener settings that affect ranking or playback (v2: target BPM), with history in events.
-- v5: playlists (SPEC.md §18.1). A track can be in several playlists; stars (1–5) make it a seed of that playlist.
CREATE TABLE IF NOT EXISTS playlists (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    spotify_id  TEXT,                   -- imported from this Spotify playlist, if any
    description TEXT,
    image_path  TEXT,                   -- cover image (from Spotify), relative to the project
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id    INTEGER NOT NULL REFERENCES tracks(id),
    stars       INTEGER,                -- 1–5; NULL = not a seed
    status      TEXT NOT NULL DEFAULT 'member',   -- 'member' | 'added' (Added in Find music, waiting for stars)
    added_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    PRIMARY KEY (playlist_id, track_id)
);

-- v5: background jobs (imports now; Find music, refills and top-ups later), run here or by the PC worker (§18.4).
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,          -- 'import', …
    playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
    payload     TEXT NOT NULL DEFAULT '{}',
    state       TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed
    progress    TEXT NOT NULL DEFAULT '{}',       -- {done, total, message, failures: […]}
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v5 (SPEC.md §18.4): tracks waiting to be downloaded and analysed by the PC worker ([worker] mode = remote).
CREATE TABLE IF NOT EXISTS work (
    id             INTEGER PRIMARY KEY,
    video_id       TEXT NOT NULL,
    bpm_prior      REAL,
    state          TEXT NOT NULL DEFAULT 'queued',  -- queued | claimed | done | failed | collected | cancelled
    claimed_at     TEXT,
    file_path      TEXT,                            -- uploaded audio, art and fingerprint (npz)
    art_path       TEXT,
    embedding_path TEXT,
    result         TEXT,                            -- JSON measurements (or failure stage and timings)
    error          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v4: each distinct set of run settings (veto, controls, pool), so plays can be split by what they ran under.
CREATE TABLE IF NOT EXISTS settings_versions (
    id          INTEGER PRIMARY KEY,
    hash        TEXT NOT NULL UNIQUE,
    settings    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,                -- JSON
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS weight_sets (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    reason      TEXT NOT NULL,      -- 'initial' | 're-weighting'
    weights     TEXT NOT NULL       -- JSON {component: weight}
);
"""


# Columns added after a table was first created: (table, column, definition).
MIGRATIONS = [
    ("tracks", "candidate_id", "INTEGER REFERENCES candidates(id)"),
    ("tracks", "generation", "INTEGER REFERENCES generations(id)"),
    ("tracks", "video_id", "TEXT"),
    ("tracks", "loudness_lufs", "REAL"),
    ("tracks", "true_peak_db", "REAL"),
    ("tracks", "gain_db", "REAL"),
    ("candidates", "fail_reason", "TEXT"),
    ("candidates", "track_id", "INTEGER REFERENCES tracks(id)"),
    ("plays", "skipped_at", "TEXT"),
    ("tracks", "spotify_id", "TEXT"),
    ("tracks", "release_date", "TEXT"),
    ("candidates", "isrc", "TEXT"),
    ("candidates", "release_date", "TEXT"),
    ("tracks", "art_path", "TEXT"),
    ("tracks", "bpm", "REAL"),
    ("plays", "target_bpm", "REAL"),
    ("plays", "played_bpm", "REAL"),
    ("plays", "playback_rate", "REAL"),
    ("reweightings", "target", "TEXT"),
    ("reweightings", "candidates", "TEXT"),
    ("candidates", "matched", "INTEGER NOT NULL DEFAULT 1"),
    ("candidates", "deezer_id", "INTEGER"),
    ("candidates", "pool_seed", "INTEGER REFERENCES tracks(id)"),
    ("plays", "scored_for_seed", "INTEGER"),          # v3: the seed pool whose view of the track scored best
    ("plays", "auto_rated", "INTEGER NOT NULL DEFAULT 0"),   # 1: skipped / ended unrated, counted as [ratings] unrated_counts_as
    ("tracks", "stars", "INTEGER"),                    # v5 session: a seed's stars (synced from its playlist)
    ("tracks", "library_id", "INTEGER"),               # v5 session: the library track this is a copy of / was saved as
    ("tracks", "veto_state", "TEXT"),                  # v4: NULL/'playable' or 'vetoed' (audio dropped, fingerprint kept)
    ("tracks", "veto", "TEXT"),                        # v4: latest veto score, z-scores and every rule's verdict (JSON)
    ("plays", "veto", "TEXT"),                         # v4: the track's veto details at the moment it was shown
    ("plays", "settings_version", "INTEGER"),          # v4: which settings the play ran under (settings_versions)
    ("plays", "tags", "TEXT"),                         # v4: optional reason tags (JSON list)
]


def _nullable_video_id(conn: sqlite3.Connection) -> None:
    """v3: candidates.video_id may be NULL (matched on YouTube Music only when selected for the pool).

    SQLite can't drop a NOT NULL constraint, so the table is rebuilt from its current definition.
    """
    col = next(r for r in conn.execute("PRAGMA table_info(candidates)") if r["name"] == "video_id")
    if not col["notnull"]:
        return
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'candidates'").fetchone()[0]
    new_sql = re.sub(r"video_id(\s+)TEXT NOT NULL", r"video_id\1TEXT", sql, count=1)
    cols = ", ".join(r["name"] for r in conn.execute("PRAGMA table_info(candidates)"))
    conn.execute("PRAGMA foreign_keys = OFF")
    with conn:
        conn.execute(re.sub(r'CREATE TABLE\s+(IF NOT EXISTS\s+)?"?candidates"?', "CREATE TABLE candidates_new",
                            new_sql, count=1))
        conn.execute(f"INSERT INTO candidates_new ({cols}) SELECT {cols} FROM candidates")
        conn.execute("DROP TABLE candidates")
        conn.execute("ALTER TABLE candidates_new RENAME TO candidates")
    conn.execute("PRAGMA foreign_keys = ON")


# v5 (SPEC.md §18.2): each playlist's Find music session has its own database, so the v4 machinery (pulls, pool,
# veto, plays) works unchanged inside it. SESSION names the playlist whose session a request or background job
# works on; None = the library database (playlists, library tracks, jobs).
SESSION: ContextVar[int | None] = ContextVar("session", default=None)
_CURRENT = object()


def session_path(playlist_id: int) -> Path:
    return path("data") / "sessions" / f"playlist-{playlist_id}.db"


def connect(session=_CURRENT) -> sqlite3.Connection:
    """The current session's database (see SESSION), a given playlist's session, or (None) the library database."""
    pid = SESSION.get() if session is _CURRENT else session
    target = path("db") if pid is None else session_path(pid)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # downloads write while other commands read
    conn.executescript(SCHEMA)
    for table, column, definition in MIGRATIONS:
        if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    _nullable_video_id(conn)
    return conn


def connect_library() -> sqlite3.Connection:
    return connect(None)


def log_event(conn: sqlite3.Connection, kind: str, play_id: int | None = None, **data) -> None:
    """Record an operational event in the DB and the log file."""
    with conn:
        conn.execute("INSERT INTO events (kind, play_id, data) VALUES (?, ?, ?)",
                     (kind, play_id, json.dumps(data, default=str)))
    log.info("%s%s %s", kind, f" play={play_id}" if play_id else "", json.dumps(data, default=str))


def setup_logging() -> None:
    """Log to data/music-hunter.log (rotated) as well as the terminal."""
    from logging.handlers import RotatingFileHandler

    path("data").mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("music_hunter")
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file = RotatingFileHandler(path("data") / "music-hunter.log", maxBytes=5_000_000, backupCount=5)
    file.setFormatter(fmt)
    root.addHandler(file)


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_setting(conn: sqlite3.Connection, key: str, value, log: bool = True) -> None:
    with conn:
        conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now'))"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                     (key, json.dumps(value)))
    if log:
        log_event(conn, "setting_changed", key=key, value=value)


def store_embedding(conn: sqlite3.Connection, track_id: int, model: str, emb) -> None:
    """Replace a track's embedding and per-chunk embeddings (emb: embedding.TrackEmbedding)."""
    conn.execute("DELETE FROM chunk_embeddings WHERE track_id=?", (track_id,))
    conn.execute(
        "INSERT OR REPLACE INTO track_embeddings (track_id, model, dim, n_chunks, embedding)"
        " VALUES (?, ?, ?, ?, ?)", (track_id, model, emb.track.size, len(emb.chunks), to_blob(emb.track)))
    conn.executemany(
        "INSERT INTO chunk_embeddings (track_id, chunk_index, start_s, end_s, embedding) VALUES (?, ?, ?, ?, ?)",
        [(track_id, i, s, e, to_blob(c)) for i, (c, (s, e)) in enumerate(zip(emb.chunks, emb.spans))])


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)
