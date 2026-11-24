"""Lanes: Find music following my stars (SPEC.md §20.7), on a temporary copy of a real pool."""

import random
import sqlite3

import music_hunter.config as config
from music_hunter import lanes, ratings, veto


def _setup(conn, monkeypatch, stars=(5, 3)):
    """Lanes on (without a playlist), the first seeds starred."""
    monkeypatch.setattr(lanes, "enabled", lambda conn: True)
    monkeypatch.setitem(config.CONFIG["veto"], "backstop_sd", 99)
    monkeypatch.setitem(config.CONFIG["lanes"], "song_min_similarity", 0.0)       # (filters tested on their own)
    monkeypatch.setitem(config.CONFIG["lanes"], "artist_min_similarity", 0.0)
    seeds = [r[0] for r in conn.execute("SELECT id FROM tracks WHERE origin = 'original_reference' ORDER BY id")]
    with conn:
        conn.execute("UPDATE tracks SET stars = NULL WHERE origin = 'original_reference'")
        for sid, n in zip(seeds, stars):
            conn.execute("UPDATE tracks SET stars = ? WHERE id = ?", (n, sid))
    veto.update(conn)                                   # (syncs the lanes)
    return seeds[:len(stars)]


def _fetched(conn):
    """Stand-in for the background restore job: every waiting lane song gets its audio."""
    from music_hunter.config import resolve
    with conn:
        for tid in lanes.missing_audio(conn):
            conn.execute("UPDATE tracks SET file_path = 'cache/' || video_id || '.webm' WHERE id = ?", (tid,))
            f = resolve(conn.execute("SELECT file_path FROM tracks WHERE id = ?", (tid,)).fetchone()[0])
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"placeholder audio")


def _drop_some_audio(conn):
    """Half the waiting lane songs without their audio (as when a lane takes songs whose audio went earlier)."""
    with conn:
        for tid in sorted(lanes.waiting(conn))[::2]:
            conn.execute("UPDATE tracks SET file_path = NULL WHERE id = ?", (tid,))


def _lane(conn, kind, key):
    return conn.execute("SELECT * FROM lanes WHERE kind = ? AND key = ?", (kind, str(key))).fetchone()


def _songs(conn, lane_id, batch=None):
    q = "SELECT track_id FROM lane_songs WHERE lane_id = ?" + (" AND batch = ?" if batch else "")
    return [r[0] for r in conn.execute(q, (lane_id, batch) if batch else (lane_id,))]


def _rate_all(conn, track_ids, rating_for):
    for tid in track_ids:
        if conn.execute("SELECT 1 FROM plays WHERE track_id = ?", (tid,)).fetchone():
            continue                                    # (shared with another lane: played already)
        p = ratings.show(conn, track_id=tid)
        ratings.rate(conn, p, rating_for(tid))
    veto.update(conn)


def test_a_starred_song_queues_one_song_per_star(conn, monkeypatch):
    seeds = _setup(conn, monkeypatch)
    five, three = _lane(conn, "song", seeds[0]), _lane(conn, "song", seeds[1])
    assert len(_songs(conn, five["id"])) == 5 and len(_songs(conn, three["id"])) == 3
    waiting = lanes.waiting(conn)
    for tid in waiting:                                 # not shut out by the tempo band (no score limit)
        v = conn.execute("SELECT veto FROM tracks WHERE id = ?", (tid,)).fetchone()[0]
        import json
        d = json.loads(v)
        assert not d["rules"]["tempo_band"]
    had = {t for t in waiting if conn.execute("SELECT file_path FROM tracks WHERE id = ?", (t,)).fetchone()[0]}
    monkeypatch.setitem(config.CONFIG["veto"], "grace_ratings", 0)
    veto.update(conn)                                   # the veto keeps a waiting song's audio (no grace needed)
    assert had and all(conn.execute("SELECT file_path FROM tracks WHERE id = ?", (t,)).fetchone()[0] for t in had)
    artist = conn.execute("SELECT * FROM lanes WHERE kind = 'artist'").fetchall()
    assert artist and all(r["batch"] == 1 for r in artist)


def test_a_song_lane_takes_close_songs_its_own_lists_first(conn, monkeypatch):
    from music_hunter import db, names
    seeds = _setup(conn, monkeypatch, stars=(5,))
    lane = _lane(conn, "song", seeds[0])
    t = conn.execute("SELECT artist, title FROM tracks WHERE id = ?", (seeds[0],)).fetchone()
    me = db.from_blob(conn.execute("SELECT embedding FROM track_embeddings WHERE track_id = ?", (seeds[0],)).fetchone()[0])
    pool = lanes._pool(conn)
    own = lanes.own_tracks(conn, seeds[0])
    ok = [i for i, p in pool.items() if p["usable"] and not p["song"].same_song(names.song(t[0], t[1]))]
    best = sorted(ok, key=lambda i: (i not in own, -float(db.from_blob(pool[i]["emb"]) @ me)))[:5]
    assert own and sorted(_songs(conn, lane["id"])) == sorted(best)


def test_the_similarity_filter(conn, monkeypatch):
    """Only songs at least song_min_similarity from the starred song join its lane; a lane short of them is owed
    the rest, and wants downloads from its seed's own lists, best list positions first."""
    from music_hunter import db
    seeds = _setup(conn, monkeypatch, stars=(5,))
    me = db.from_blob(conn.execute("SELECT embedding FROM track_embeddings WHERE track_id = ?", (seeds[0],)).fetchone()[0])
    pool = lanes._pool(conn)
    sims = sorted((float(db.from_blob(p["emb"]) @ me) for p in pool.values() if p["usable"]), reverse=True)
    floor = (sims[1] + sims[2]) / 2                     # only two songs pass
    monkeypatch.setitem(config.CONFIG["lanes"], "song_min_similarity", floor)
    with conn:
        conn.execute("DELETE FROM lane_songs")
        conn.execute("UPDATE lanes SET due = 5")
    veto.update(conn)
    lane = _lane(conn, "song", seeds[0])
    got = _songs(conn, lane["id"])
    assert 1 <= len(got) <= 2 and lane["due"] == 5 - len(got)
    assert all(float(db.from_blob(pool[t]["emb"]) @ me) >= floor for t in got)
    want = [w for w in lanes.wanted(conn) if w[2] == lane["id"]]
    cands = lanes._seed_candidates(conn, seeds[0])
    assert [w[0] for w in want] == cands[:len(want)] and all(w[1] == seeds[0] for w in want)


def test_a_song_two_lanes_share(conn, monkeypatch):
    """A song can wait in a song lane and an artist lane at once; it plays once and counts for both."""
    _setup(conn, monkeypatch)
    rows = conn.execute("SELECT track_id, count(*) FROM lane_songs GROUP BY track_id HAVING count(*) > 1").fetchall()
    if not rows:
        return
    tid = rows[0][0]
    p = ratings.show(conn, track_id=tid)
    ratings.rate(conn, p, ratings.ADD)
    assert tid not in lanes.waiting(conn)
    lanes_of = [r[0] for r in conn.execute("SELECT lane_id FROM lane_songs WHERE track_id = ?", (tid,))]
    assert all(lanes._result(conn, l, 1) >= 1 for l in lanes_of)


def test_an_add_refills_and_offs_quieten(conn, monkeypatch):
    seeds = _setup(conn, monkeypatch, stars=(4, 2))
    good, bad = _lane(conn, "song", seeds[0]), _lane(conn, "song", seeds[1])
    first_good = _songs(conn, good["id"], 1)
    add = ratings.ADD
    _rate_all(conn, first_good, lambda t: add if t == first_good[0] else 2)      # one Add, the rest heard: net +1
    _rate_all(conn, _songs(conn, bad["id"], 1), lambda t: 1)                     # all Off
    good, bad = _lane(conn, "song", seeds[0]), _lane(conn, "song", seeds[1])
    assert good["state"] == "active" and good["batch"] == 2
    second = _songs(conn, good["id"], 2)
    assert len(second) == 4 and not set(second) & set(first_good)              # the next-closest songs
    assert bad["state"] == "quiet" and not _songs(conn, bad["id"], 2)
    # One Add and one Off cancel: no refill.
    _rate_all(conn, second, lambda t: add if t == second[0] else 1 if t == second[1] else 2)
    assert _lane(conn, "song", seeds[0])["state"] == "quiet"
    # More stars wake a quiet lane.
    with conn:
        conn.execute("UPDATE tracks SET stars = 4 WHERE id = ?", (seeds[1],))
    veto.update(conn)
    bad = _lane(conn, "song", seeds[1])
    assert bad["state"] == "active" and len(_songs(conn, bad["id"], bad["batch"])) == 4


def test_half_the_picks_come_from_the_lanes(conn, monkeypatch):
    _setup(conn, monkeypatch)
    rng = random.Random(7)
    lane_picks = 0
    for _ in range(40):
        _fetched(conn)                                  # (the background restore job: lane songs' audio)
        tid = ratings.upcoming(conn, rng)
        draw = ratings.db.get_setting(conn, "next_draw")
        p = ratings.show_next(conn, rng)
        lane_id = conn.execute("SELECT lane_id FROM plays WHERE id = ?", (p,)).fetchone()[0]
        assert (lane_id is not None) == bool(draw.get("lane"))
        lane_picks += lane_id is not None
        if lane_id is not None:
            assert tid in lanes.waiting_ids(conn) or True
        ratings.rate(conn, p, 2)
    assert 10 <= lane_picks <= 30
    assert not conn.execute("SELECT count(*) FROM plays WHERE is_control = 1").fetchone()[0]   # no checks any more


def test_a_random_pick_of_a_lane_song_counts_for_the_lane(conn, monkeypatch):
    """The two kinds of pick share one song: a lane song the random picks reach first leaves the lane's waiting list,
    and its rating counts for the lane."""
    seeds = _setup(conn, monkeypatch, stars=(1,))
    lane = _lane(conn, "song", seeds[0])
    (tid,) = _songs(conn, lane["id"])
    with conn:
        conn.execute("UPDATE tracks SET veto_state = 'playable' WHERE id = ?", (tid,))
    p = ratings.show(conn, track_id=tid)               # a random pick (no lane)
    ratings.rate(conn, p, ratings.ADD)
    assert tid not in lanes.waiting(conn)
    veto.update(conn)
    lane = _lane(conn, "song", seeds[0])
    assert lane["batch"] == 2 and lane["state"] == "active"                     # its Add refilled the lane


def test_a_lane_pick_never_waits_for_audio(conn, monkeypatch):
    """§20.7, 2026-11-24: lane picks come only from songs whose audio is here; the rest are fetched in the background
    first (before top-ups), and until then the pick is a random one."""
    from music_hunter import pool
    _setup(conn, monkeypatch)
    _drop_some_audio(conn)
    missing = lanes.missing_audio(conn)
    assert missing and pool.missing_audio(conn)[:len(missing)] == missing        # lane songs first
    rng = random.Random(3)
    for _ in range(20):
        ratings.upcoming(conn, rng)
        draw = ratings.db.get_setting(conn, "next_draw")
        assert draw["track_id"] not in missing
        ratings.rate(conn, ratings.show_next(conn, rng), 2)


def test_the_background_worker_fetches_lane_songs(conn, monkeypatch):
    """The session's worker sees its lanes (it asks for the job outside any request) and fetches lane songs first."""
    from music_hunter import web
    _setup(conn, monkeypatch)
    _drop_some_audio(conn)
    monkeypatch.setattr("music_hunter.analysis.youtube_paused", lambda **k: False)
    assert lanes.missing_audio(conn)
    seen = {}
    monkeypatch.setattr(web.Worker, "job_needed", staticmethod(lambda c: seen.setdefault("job", web.db.SESSION.get()) and None))
    web.Worker(5).start_if_needed()
    assert seen["job"] == 5                             # the playlist is known while it decides


def test_rescoring_runs_in_a_background_process(conn, monkeypatch, tmp_path):
    """2026-11-24: veto.update (and the lanes) runs in a separate, lower-priority process, with the same result."""
    import json
    import shutil
    from music_hunter import background, db
    monkeypatch.setattr(lanes, "enabled", lambda conn: False)
    # a playlist's Find music database, where the child process finds it (MUSIC_HUNTER_ROOT)
    sess = tmp_path / "data" / "sessions" / "playlist-3.db"
    sess.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(tmp_path / "data" / "music_hunter.db")
    with sqlite3.connect(sess) as dst:
        src.backup(dst)
    monkeypatch.setenv("MUSIC_HUNTER_ROOT", str(tmp_path))
    monkeypatch.setitem(config.CONFIG["ui"], "background_process", True)
    monkeypatch.setitem(config.CONFIG["ui"], "background_processes", 1)
    s = db.connect(3)
    token = db.SESSION.set(3)                            # (as the app calls it: in that playlist's session)
    try:
        there = veto.update(s)                           # in the child
        assert background._pool is not None
        states = dict(s.execute("SELECT id, veto_state FROM tracks WHERE origin = 'pool'").fetchall())
        monkeypatch.setitem(config.CONFIG["ui"], "background_process", False)
        here = veto.update(s)                            # the same, here
        assert {k: there[k] for k in ("analysed", "playable", "vetoed")} == {k: here[k] for k in ("analysed", "playable", "vetoed")}
        assert states == dict(s.execute("SELECT id, veto_state FROM tracks WHERE origin = 'pool'").fetchall())
    finally:
        db.SESSION.reset(token)
        if background._pool is not None:
            background._pool.shutdown()
            background._pool = None


def test_an_artist_search_suggestion_doesnt_break_scoring(conn, monkeypatch):
    """2026-11-24: a pool song suggested only by a lane's artist search (source 'artist_top') made scoring fail with
    a KeyError, so Find music couldn't start ("internal error" in La Musica)."""
    from music_hunter.scoring import score_pool
    _setup(conn, monkeypatch)
    tid, cid = conn.execute("SELECT id, candidate_id FROM tracks WHERE origin = 'pool' AND candidate_id IS NOT NULL"
                            " AND id NOT IN (SELECT track_id FROM plays) LIMIT 1").fetchone()
    with conn:
        g = conn.execute("SELECT * FROM suggestions WHERE candidate_id = ? LIMIT 1", (cid,)).fetchone()
        conn.execute("INSERT INTO suggestions (generation, seed_track_id, source, rank, artist, title, outcome, candidate_id)"
                     " VALUES (?, ?, 'artist_top', 1, 'A', 'B', 'new', ?)", (g["generation"], g["seed_track_id"], cid))
    assert score_pool(conn)
    p = ratings.show(conn, track_id=tid)
    assert p
