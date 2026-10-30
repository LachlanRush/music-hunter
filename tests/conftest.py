"""Shared fixture: a temporary copy of the real project, reset to a pool with no plays.

Works whether or not listening has started: plays, events and re-weightings are
cleared in the copy, and placeholder audio files stand in for the pool. The real
database and folders are never touched.
"""

import sqlite3

import pytest

import music_hunter.config as config
from music_hunter import db

REAL_DB = config.path("db")
ARCHIVES = [config.ROOT / "runs" / v / "data" / "music_hunter.db" for v in ("v2", "v1")]


def source_db():
    """The live database if it has a finished pool, else the latest archived run's (its pool is all that's used).

    A pool still downloading (a generation without a recorded pool size) isn't used: its tracks
    would be dropped as unfinished, leaving nothing to test against.
    """
    for candidate in (REAL_DB, *ARCHIVES):
        if not candidate.exists():
            continue
        with sqlite3.connect(candidate) as c:
            if not c.execute("SELECT count(*) FROM sqlite_master WHERE name = 'tracks'").fetchone()[0]:
                continue
            pool = c.execute("SELECT count(*) FROM tracks WHERE origin = 'pool'").fetchone()[0]
            unfinished = c.execute("SELECT count(*) FROM generations g WHERE EXISTS (SELECT 1 FROM tracks t"
                                   " WHERE t.generation = g.id) AND json_extract(coalesce(g.stats, '{}'),"
                                   " '$.pool_size') IS NULL").fetchone()[0]
            if pool and not unfinished:
                return candidate
    return None


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db_file = source_db()
    if db_file is None:
        pytest.skip("needs a filled pool: run `music-hunter pull` first")
    (tmp_path / "data").mkdir()
    src = sqlite3.connect(db_file)
    with sqlite3.connect(tmp_path / "data" / "music_hunter.db") as dst:
        src.backup(dst)
    src.close()
    monkeypatch.setattr(config, "ROOT", tmp_path)
    c = db.connect()
    if not c.execute("SELECT count(*) FROM tracks WHERE origin = 'pool'").fetchone()[0]:
        pytest.skip("needs a filled pool: run `music-hunter pull` first")
    with c:
        for table in ("plays", "events", "reweightings"):
            c.execute(f"DELETE FROM {table}")
        # A pull or fill still running in the real server leaves an unfinished generation (no pool size yet).
        for (gen,) in c.execute("SELECT id FROM generations"
                                " WHERE json_extract(coalesce(stats, '{}'), '$.pool_size') IS NULL").fetchall():
            for (tid,) in c.execute("SELECT id FROM tracks WHERE generation = ?", (gen,)).fetchall():
                c.execute("DELETE FROM chunk_embeddings WHERE track_id = ?", (tid,))
                c.execute("DELETE FROM track_embeddings WHERE track_id = ?", (tid,))
            c.execute("UPDATE candidates SET track_id = NULL WHERE generation = ?", (gen,))
            c.execute("DELETE FROM tracks WHERE generation = ?", (gen,))
            c.execute("DELETE FROM suggestions WHERE generation = ?", (gen,))
            c.execute("DELETE FROM candidates WHERE generation = ?", (gen,))
            c.execute("DELETE FROM generations WHERE id = ?", (gen,))
        c.execute("DELETE FROM weight_sets WHERE id > (SELECT min(id) FROM weight_sets)")
        c.execute("UPDATE tracks SET rating = NULL, file_path = 'cache/' || video_id || '.webm' WHERE origin = 'pool'")
    for (rel,) in c.execute("SELECT file_path FROM tracks WHERE origin = 'pool'"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"placeholder audio")
    yield c
    c.close()
