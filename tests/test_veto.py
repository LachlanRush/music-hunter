"""v4 veto and rotating pool (SPEC.md §17.2–17.4), on a temporary copy of a real pool."""

import json

import numpy as np

import music_hunter.config as config
from music_hunter import pool, ratings, veto


def test_robust_z_ignores_a_long_tail():
    x = np.array([0.80, 0.81, 0.82, 0.83, 0.84, 0.85, 0.30, 0.20])     # two tracks far out of the area
    z = veto.robust_z(x)
    assert z[-1] < -10 and abs(np.median(z)) < 1e-9                  # the tail doesn't stretch the scale


def test_veto_rotates_the_pool(conn):
    counts = veto.update(conn)
    assert counts["active"] and 0 < counts["vetoed"] < counts["analysed"] * 0.3
    rows = conn.execute("SELECT id, file_path, veto_state, veto FROM tracks WHERE origin = 'pool'").fetchall()
    vetoed = [r for r in rows if r["veto_state"] == "vetoed"]
    assert all(r["file_path"] is None for r in vetoed)                  # audio dropped…
    assert all(conn.execute("SELECT count(*) FROM track_embeddings WHERE track_id = ?", (r["id"],)).fetchone()[0]
               for r in vetoed)                                        # …fingerprint kept
    for r in vetoed:
        v = json.loads(r["veto"])
        assert v["z"] < -config.CONFIG["veto"]["cut_sd"] or v["rules"]["backstop"] or v["rules"]["tempo_band"]
    assert set(ratings.playable_ids(conn)).isdisjoint({r["id"] for r in vetoed})
    assert counts["playable"] == pool.unrated_pool_size(conn)


def test_two_lines_and_return(conn, monkeypatch):
    veto.update(conn)
    tid = next(r[0] for r in conn.execute("SELECT id, veto FROM tracks WHERE veto_state = 'vetoed'")
               if not json.loads(r[1])["rules"]["tempo_band"])                # not held by the tempo band
    z = json.loads(conn.execute("SELECT veto FROM tracks WHERE id = ?", (tid,)).fetchone()[0])["z"]
    # Between the lines a vetoed track stays vetoed (no flip-flopping)…
    monkeypatch.setitem(config.CONFIG["veto"], "return_sd", -z - 0.01)       # return line just above the track
    monkeypatch.setitem(config.CONFIG["veto"], "backstop_sd", 99)
    veto.update(conn)
    assert conn.execute("SELECT veto_state FROM tracks WHERE id = ?", (tid,)).fetchone()[0] == "vetoed"
    # …above the return line it becomes playable again, and needs its audio back.
    monkeypatch.setitem(config.CONFIG["veto"], "return_sd", -z + 0.01)       # return line just below it
    counts = veto.update(conn)
    assert counts["returned"] >= 1
    assert conn.execute("SELECT veto_state FROM tracks WHERE id = ?", (tid,)).fetchone()[0] == "playable"
    assert tid in pool.missing_audio(conn) and tid not in ratings.playable_ids(conn)


def test_statistics_are_anchored_to_everything_analysed(conn):
    """Playing the best tracks must not lower the line: played tracks stay in the statistics."""
    veto.update(conn)
    before = {r[0]: json.loads(r[1])["z"] for r in conn.execute("SELECT id, veto FROM tracks WHERE veto IS NOT NULL")}
    best = sorted(before, key=before.get, reverse=True)[:30]
    for tid in best:                                                   # play out the 30 best-scored tracks
        p = ratings.show(conn, track_id=tid)
        ratings.skip(conn, p)
    veto.update(conn)
    after = {r[0]: json.loads(r[1])["z"] for r in conn.execute("SELECT id, veto FROM tracks WHERE veto IS NOT NULL")}
    rest = [t for t in before if t not in best]
    assert max(abs(after[t] - before[t]) for t in rest) < 1e-9          # nothing moved


def test_settings_version_is_stable(conn, monkeypatch):
    a = veto.settings_version(conn)
    assert veto.settings_version(conn) == a
    monkeypatch.setitem(config.CONFIG["veto"], "cut_sd", 1.5)
    assert veto.settings_version(conn) != a


def test_tempo_band(conn):
    """A track far below or above the seeds' tempo is vetoed whatever its score; a half/double/2:3 reading of the
    right tempo isn't; closeness to the tempo is part of the score."""
    veto.update(conn)
    rows = {r["id"]: json.loads(r["veto"]) for r in conn.execute("SELECT id, veto FROM tracks WHERE veto IS NOT NULL")}
    target = next(iter(rows.values()))["tempo"]["target"]
    assert 120 < target < 160 and abs(veto.nearest_reading(target * 2 / 3, target) - target) < 1e-9
    tid = max(rows, key=lambda t: rows[t]["z"])                                   # the best-scored track…
    with conn:
        conn.execute("UPDATE tracks SET bpm = ? WHERE id = ?", (target * 0.85, tid))   # …made 15% slower
    veto.update(conn)
    v = json.loads(conn.execute("SELECT veto, veto_state FROM tracks WHERE id = ?", (tid,)).fetchone()[0])
    assert v["rules"]["tempo_band"] and v["tempo"]["off_pct"] == -15.0
    played = tid in {r[0] for r in conn.execute("SELECT track_id FROM plays")}
    assert played or conn.execute("SELECT veto_state FROM tracks WHERE id = ?", (tid,)).fetchone()[0] == "vetoed"
    with conn:
        conn.execute("UPDATE tracks SET bpm = ? WHERE id = ?", (target / 2, tid))       # a half-tempo misread: fine
    assert not veto.compute(conn)[tid]["rules"]["tempo_band"]
