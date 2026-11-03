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
    assert counts["active"] and 0 < counts["vetoed"] < counts["analysed"] * 0.75     # gate at the median
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
    """A track more than tempo_margin_bpm outside the seeds' tempo range is vetoed whatever its score; a half / double /
    2:3 reading of a tempo inside it isn't; distance from the range is part of the score."""
    veto.update(conn)
    rows = {r["id"]: json.loads(r["veto"]) for r in conn.execute("SELECT id, veto FROM tracks WHERE veto IS NOT NULL")}
    t = next(iter(rows.values()))["tempo"]
    assert t["lo"] <= t["center"] <= t["hi"] and 120 < t["center"] < 160
    assert abs(veto.nearest_reading(t["center"] * 2 / 3, t["center"]) - t["center"]) < 1e-9
    margin = config.CONFIG["veto"]["tempo_margin_bpm"]
    tid = max(rows, key=lambda k: rows[k]["z"])                                      # the best-scored track…
    with conn:
        conn.execute("UPDATE tracks SET bpm = ? WHERE id = ?", (t["lo"] - margin - 2, tid))   # …made too slow
    veto.update(conn)
    v = json.loads(conn.execute("SELECT veto FROM tracks WHERE id = ?", (tid,)).fetchone()[0])
    assert v["rules"]["tempo_band"] and v["tempo"]["outside_bpm"] == margin + 2
    played = tid in {r[0] for r in conn.execute("SELECT track_id FROM plays")}
    assert played or conn.execute("SELECT veto_state FROM tracks WHERE id = ?", (tid,)).fetchone()[0] == "vetoed"
    with conn:
        conn.execute("UPDATE tracks SET bpm = ? WHERE id = ?", (t["lo"] - margin + 2, tid))   # inside: scored down
    v = veto.compute(conn)[tid]
    assert not v["rules"]["tempo_band"] and v["raw"]["tempo"] == -(margin - 2)
    with conn:
        conn.execute("UPDATE tracks SET bpm = ? WHERE id = ?", (t["center"] / 2, tid))   # a half-tempo misread: fine
    assert veto.compute(conn)[tid]["raw"]["tempo"] == 0.0


def test_playlist_tempo_from_four_and_five_stars(tmp_path, monkeypatch):
    from music_hunter import db, library, tempo

    monkeypatch.setattr(config, "ROOT", tmp_path)
    lib = db.connect()
    pid = library.create_playlist(lib, "p")

    def add(bpm, stars):
        with lib:
            tid = lib.execute("INSERT INTO tracks (artist, title, duration_s, origin, bpm) VALUES ('a', 't', 200,"
                              " 'library', ?)", (bpm,)).lastrowid
        library.add_to_playlist(lib, pid, tid)
        library.set_stars(lib, pid, tid, stars)
        return tid
    for b in (138, 141, 142, 145):
        add(b, 5)
    add(120, 2)                                      # a 2-star track doesn't widen the range
    add(70.5, 4)                                     # a half-tempo reading of 141 counts as 141
    assert tempo.playlist_tempo(lib, pid) == {"center": 141.0, "lo": 138.0, "hi": 145.0}
    add(146.5, 4)                                    # the top end moves 1.5 BPM: kept steady
    assert tempo.playlist_tempo(lib, pid)["hi"] == 145.0
    add(148, 4)                                      # 3 BPM: updated
    assert tempo.playlist_tempo(lib, pid)["hi"] == 148.0


def test_grace_and_redownload_limit(conn, monkeypatch):
    """A playable track that falls below the gate keeps its audio for grace_ratings ratings, then loses it; a track
    whose audio was downloaded again max_redownloads times isn't brought back (unless it reaches the top band)."""
    veto.update(conn)
    tid = conn.execute("SELECT id FROM tracks WHERE veto_state = 'playable' AND file_path IS NOT NULL"
                       " AND id NOT IN (SELECT track_id FROM plays) LIMIT 1").fetchone()[0]
    z = json.loads(conn.execute("SELECT veto FROM tracks WHERE id = ?", (tid,)).fetchone()[0])["z"]
    monkeypatch.setitem(config.CONFIG["veto"], "cut_sd", -z - 0.01)            # gate just above it: it falls below
    monkeypatch.setitem(config.CONFIG["veto"], "return_sd", -z - 0.5)
    veto.update(conn)
    row = lambda: conn.execute("SELECT veto_state, file_path, gate_since FROM tracks WHERE id = ?", (tid,)).fetchone()
    assert row()[0] == "vetoed" and row()[1] is not None                         # in grace: audio kept
    monkeypatch.setitem(config.CONFIG["veto"], "grace_ratings", 0)
    veto.update(conn)
    assert row()[1] is None                                                      # grace over: audio gone
    with conn:
        conn.execute("UPDATE tracks SET redownloads = 2 WHERE id = ?", (tid,))
    monkeypatch.setitem(config.CONFIG["veto"], "cut_sd", -z + 0.5)             # it's well above the gate again…
    monkeypatch.setitem(config.CONFIG["veto"], "return_sd", -z + 0.4)
    monkeypatch.setitem(config.CONFIG["veto"], "redownload_exempt_sd", 99)
    veto.update(conn)
    assert row()[0] == "vetoed"                                                  # …but downloaded twice already
    monkeypatch.setitem(config.CONFIG["veto"], "redownload_exempt_sd", -99)    # top band: comes back regardless
    veto.update(conn)
    assert row()[0] == "playable" and tid in pool.missing_audio(conn)


def test_no_more_slipped_in_tracks(conn):
    from music_hunter import db

    db.set_setting(conn, "control_rate", 0.5, log=False)          # a rate drawn by the run earlier…
    assert not any(ratings.next_is_control(conn) for _ in range(50))  # …no longer used: controls are off
