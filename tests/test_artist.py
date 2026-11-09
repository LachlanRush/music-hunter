"""Artist history in the veto score (SPEC.md §18.29)."""

import json

import numpy as np

import music_hunter.config as config
from music_hunter import artist, veto


def _hist(conn, rows):
    artist.store(conn, [{"source": "test", "artist": a, "title": t, "value": v, "rated_at": None} for a, t, v in rows])


def test_artist_value_pools_artists_and_skips_the_song_itself(conn, monkeypatch):
    monkeypatch.setitem(config.CONFIG["veto"], "artist_min_ratings", 5)
    _hist(conn, [("Alpha", "One", -1), ("Alpha", "Two", -1), ("Alpha", "Three", 1),
                 ("Beta", "Four", 0), ("Alpha & Beta", "Five", 1),        # one rating, counted once for the pair
                 ("Alpha", "Same Song (Extended Mix)", -1)])
    vals, n = artist.values(conn, [("Alpha", "Six"), ("Alpha, Beta", "Seven"), ("Beta", "Eight"),
                                   ("Alpha", "Same Song")])
    assert n.tolist() == [5, 6, 2, 4]                  # its own song (any version) never counts
    assert vals[0] == np.mean([-1, -1, 1, 1, -1])
    assert vals[1] == np.mean([-1, -1, 1, 0, 1, -1])   # Alpha's and Beta's ratings pooled
    assert np.isnan(vals[2]) and np.isnan(vals[3])     # fewer than 5: the average artist


def test_artist_share_moves_the_veto_score(conn, monkeypatch):
    t = conn.execute("SELECT artist, title, id FROM tracks WHERE origin = 'pool' LIMIT 1").fetchone()
    other = conn.execute("SELECT artist FROM tracks WHERE origin = 'pool' AND artist != ? LIMIT 1",
                         (t["artist"],)).fetchone()[0]
    _hist(conn, [(t["artist"], f"Off {i}", -1) for i in range(6)] + [(other, f"Add {i}", 1) for i in range(6)])
    monkeypatch.setitem(config.CONFIG["veto"], "artist_share", 0.0)
    before = veto.compute(conn)[t["id"]]
    monkeypatch.setitem(config.CONFIG["veto"], "artist_share", 0.4)
    after = veto.compute(conn)[t["id"]]
    assert after["artist"] == {"value": -1.0, "ratings": 6, "z": after["artist"]["z"]} and after["artist"]["z"] < 0
    assert after["z"] < before["z"]                    # disliked artist: needs a higher sound score to pass
    veto.update(conn)
    logged = json.loads(conn.execute("SELECT veto FROM tracks WHERE id = ?", (t["id"],)).fetchone()[0])
    assert logged["artist"]["ratings"] == 6


def test_archived_runs_map_to_add_close_off(tmp_path):
    import sqlite3
    run = tmp_path / "v1" / "data"
    run.mkdir(parents=True)
    c = sqlite3.connect(run / "music_hunter.db")
    c.executescript("CREATE TABLE tracks (id INTEGER PRIMARY KEY, artist TEXT, title TEXT);"
                    "CREATE TABLE plays (id INTEGER PRIMARY KEY, track_id INTEGER, rating INTEGER, rated_at TEXT);")
    c.executemany("INSERT INTO tracks VALUES (?, 'A', ?)", [(i, f"T{i}") for i in range(1, 6)])
    c.executemany("INSERT INTO plays VALUES (?, ?, ?, NULL)", [(i, i, i) for i in range(1, 6)])
    c.commit()
    assert [r["value"] for r in artist.archived(tmp_path / "v1", "v1")] == [-1, -1, 0, 1, 1]   # 1–5 stars


def test_playlist_tracks_count_as_adds_once(conn, monkeypatch):
    monkeypatch.setitem(config.CONFIG["veto"], "artist_min_ratings", 5)
    _hist(conn, [("Alpha", "One", -1), ("Alpha", "Two", -1), ("Alpha", "Three", -1),
                 ("Alpha", "Kept (Extended Mix)", 1)])               # an Add that's now in the playlist
    with conn:
        pid = conn.execute("INSERT INTO playlists (name) VALUES ('test')").lastrowid
        for t in ("Kept", "Kept (Radio Edit)", "Starred"):            # one song in two versions, and a seed
            tid = conn.execute("INSERT INTO tracks (origin, artist, title, video_id) VALUES ('library', 'Alpha', ?, ?)",
                               (t, f"vid-{t}")).lastrowid
            conn.execute("INSERT INTO playlist_tracks (playlist_id, track_id, stars) VALUES (?, ?, ?)",
                         (pid, tid, 5 if t == "Starred" else None))
    monkeypatch.setattr(artist, "playlist_id", lambda c: pid)
    vals, n = artist.values(conn, [("Alpha", "New")])
    assert n[0] == 5                                   # 3 Offs + Kept once (not its Add as well) + Starred
    assert vals[0] == np.mean([-1, -1, -1, 1, 1])
    monkeypatch.setattr(artist, "playlist_id", lambda c: None)
    assert artist.values(conn, [("Alpha", "New")])[1][0] == 4   # outside a playlist's session: ratings only


def test_artist_push_is_read_against_zero_not_other_artists(conn, monkeypatch):
    worst, mild, unknown = [r[0] for r in conn.execute("SELECT id FROM tracks WHERE origin = 'pool' LIMIT 3")]
    with conn:      # made-up names: real credits can share an artist, which would pool their ratings
        for tid, name in ((worst, "Worst Artist"), (mild, "Mild Artist"), (unknown, "Unknown Artist")):
            conn.execute("UPDATE tracks SET artist = ? WHERE id = ?", (name, tid))
    # Every artist known here leans disliked: the less disliked one must still be pushed down, not up.
    _hist(conn, [("Worst Artist", f"W{i}", -1) for i in range(6)]
          + [("Mild Artist", f"M{i}", -1 if i < 2 else 0) for i in range(6)])
    d = veto.compute(conn)
    scale = config.CONFIG["veto"]["artist_scale"]
    assert d[worst]["artist"]["z"] == round(-1 / scale, 3)
    assert d[mild]["artist"]["z"] == round(-(2 / 6) / scale, 3)
    assert d[unknown]["artist"]["z"] == 0              # too few ratings: no push
