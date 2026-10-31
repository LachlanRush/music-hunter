"""Scripted ratings produce the expected updates — v3 rules (SPEC.md §16.4, §15.4, §5, §7).

Buttons: 1 Off (negative set), 2 Close (mild positive, weight 0.5), 3 Add (positive, weight 2, seed,
saved to collection/). Audio of tracks not Added is held in data/undo/ until the next rating, then
deleted. Runs on a temporary copy of a pool.
"""

import random

import pytest

import music_hunter.config as config
from music_hunter.pool import quotas
from music_hunter.pull import Pull, seeds
from music_hunter.ratings import (ADD, SOURCES, change_last_rating, negative_set, next_is_control, positive_set, rate,
                                  seed_averages, show, skip, source_averages)
from music_hunter.scoring import score_pool
from music_hunter.sources import Item

PRIOR, K = config.CONFIG["averages"]["prior"], config.CONFIG["averages"]["prior_strength"]
OFF, CLOSE = 1, 2
CLOSE_WEIGHT = config.CONFIG["ratings"]["positive_weights"][str(CLOSE)]


def provenance(c, track_id):
    """(seeds, sources) that suggested a track — queried independently of ratings.py."""
    rows = c.execute("SELECT DISTINCT s.seed_track_id, s.source FROM suggestions s"
                     " JOIN tracks t ON t.candidate_id = s.candidate_id WHERE t.id = ?", (track_id,)).fetchall()
    return {r[0] for r in rows}, {r[1] for r in rows}


def expected_averages(c, history):
    """§5 formula from the test's own record of ratings, in rating order: [(track_id, rating, generation)].

    v3: a rating decay_half_life_ratings back counts half (or × decay per generation when that is 0)."""
    gen_now = c.execute("SELECT max(id) FROM generations").fetchone()[0]
    half_life = config.CONFIG["averages"]["decay_half_life_ratings"]
    sums = {}
    for i, (track_id, rating, gen) in enumerate(history):
        w = (0.5 ** ((len(history) - 1 - i) / half_life) if half_life
             else config.CONFIG["averages"]["decay"] ** (gen_now - gen))
        seeds_, sources_ = provenance(c, track_id)
        for key in seeds_ | sources_:
            t, n = sums.get(key, (0.0, 0.0))
            sums[key] = (t + w * rating, n + w)
    return {k: (PRIOR * K + t) / (K + n) for k, (t, n) in sums.items()}


def all_averages(c):
    return {**seed_averages(c, [s.track_id for s in seeds(c)]), **source_averages(c)}


def check_averages(c, history):
    expected = expected_averages(c, history)
    for key, value in all_averages(c).items():
        assert value == pytest.approx(expected.get(key, PRIOR)), key


def track_of(c, play_id):
    return c.execute("SELECT t.* FROM plays p JOIN tracks t ON t.id = p.track_id WHERE p.id = ?",
                     (play_id,)).fetchone()


def undo_of(c, play_id):
    return c.execute("SELECT undo_path FROM plays WHERE id = ?", (play_id,)).fetchone()[0]


def test_scripted_ratings(conn):
    c = conn
    root = config.ROOT
    gen = c.execute("SELECT max(id) FROM generations").fetchone()[0]
    originals = [r[0] for r in c.execute("SELECT id FROM tracks WHERE origin = 'original_reference' ORDER BY id")]
    history = []

    # Fresh pool: everything at the prior; positive set = the seeds at Add weight.
    check_averages(c, history)
    ids, _, w = positive_set(c)
    assert ids == originals and set(w) == {2.0}
    assert negative_set(c)[0] == []
    before = {r["track_id"]: r for r in score_pool(c)}
    pool_size = c.execute("SELECT count(*) FROM tracks WHERE origin = 'pool'").fetchone()[0]
    assert len(before) == pool_size

    # 1. Learning play Added: collection/, positive weight 2, becomes a seed.
    p1 = show(c)
    t1 = track_of(c, p1)
    old_file = root / t1["file_path"]
    rate(c, p1, ADD, played_seconds=120)
    history.append((t1["id"], ADD, gen))
    t1 = track_of(c, p1)
    assert t1["file_path"].startswith("collection/") and (root / t1["file_path"]).exists()
    assert not old_file.exists()
    ids, _, w = positive_set(c)
    assert w[ids.index(t1["id"])] == 2.0
    assert any(s.track_id == t1["id"] for s in seeds(c))
    check_averages(c, history)
    for key in set.union(*provenance(c, t1["id"])):
        assert expected_averages(c, history)[key] == pytest.approx((PRIOR * K + ADD) / (K + 1))

    # Pool re-scores: rated track gone, diversity penalty active; audio match can only rise, unless this
    # reference took the set to audio_match_min_refs (then the top 3 are averaged instead of the closest one).
    after = {r["track_id"]: r for r in score_pool(c)}
    assert len(after) == pool_size - 1 and t1["id"] not in after
    rises = [after[t]["raw"]["audio_match"] - before[t]["raw"]["audio_match"] for t in after]
    if len(originals) + 1 != config.CONFIG["scoring"]["audio_match_min_refs"]:
        assert min(rises) >= -1e-6
    assert max(r["raw"]["diversity_penalty"] for r in after.values()) > 0

    # 2. Off: audio held in data/undo/, negative set (weight 1), dislike spans 0–1.
    p2 = show(c)
    t2 = track_of(c, p2)
    rate(c, p2, OFF)
    history.append((t2["id"], OFF, gen))
    assert track_of(c, p2)["file_path"] is None and (root / undo_of(c, p2)).exists()
    ids, _, w = negative_set(c)
    assert ids == [t2["id"]] and list(w) == [1.0]
    scored = score_pool(c)
    dislike = [r["norm"]["dislike_match"] for r in scored]
    assert max(dislike) == pytest.approx(1.0) and min(dislike) == pytest.approx(0.0)
    raw = [r["raw"]["dislike_match"] for r in scored]          # one Off track: plain similarity to it
    assert -1.0 <= min(raw) < max(raw) <= 1.0
    check_averages(c, history)

    # 3. Mis-tap correction Off → Close: out of the negative set, into the positive set at Close's
    #    mild weight; still not saved (only Add saves), so the audio stays held.
    assert change_last_rating(c, CLOSE) == p2
    history[-1] = (t2["id"], CLOSE, gen)
    assert negative_set(c)[0] == []
    ids, _, w = positive_set(c)
    assert w[ids.index(t2["id"])] == CLOSE_WEIGHT
    assert track_of(c, p2)["file_path"] is None and (root / undo_of(c, p2)).exists()
    check_averages(c, history)
    held = root / undo_of(c, p2)

    # 4. Another Off. Rating it makes the previous hold final: audio deleted.
    p3 = show(c)
    t3 = track_of(c, p3)
    rate(c, p3, OFF)
    history.append((t3["id"], OFF, gen))
    assert not held.exists() and undo_of(c, p2) is None
    assert t3["id"] not in positive_set(c)[0] and t3["id"] in negative_set(c)[0]
    assert c.execute("SELECT count(*) FROM track_embeddings WHERE track_id = ?", (t2["id"],)).fetchone()[0] == 1
    check_averages(c, history)

    # 5. Skip, then rate the next track: the skipped track's audio is deleted.
    p4 = show(c)
    t4 = track_of(c, p4)
    skipped_file = root / t4["file_path"]
    skip(c, p4, played_seconds=5)
    assert skipped_file.exists()                     # still reachable with the back button
    p5 = show(c)
    t5 = track_of(c, p5)
    rate(c, p5, CLOSE)
    history.append((t5["id"], CLOSE, gen))
    assert not skipped_file.exists() and track_of(c, p4)["file_path"] is None

    # 6. Control Added (v3, SPEC.md §16.6): saved to collection/, counts for learning (positive set, seed and
    #    source credit) — but never becomes a seed, so random picks don't steer the sources.
    p6 = show(c, control=True, rng=random.Random(3))
    t6 = track_of(c, p6)
    rate(c, p6, ADD)
    history.append((t6["id"], ADD, gen))
    assert track_of(c, p6)["file_path"].startswith("collection/")
    ids, _, w = positive_set(c)
    assert w[ids.index(t6["id"])] == 2.0
    assert all(s.track_id != t6["id"] for s in seeds(c))
    check_averages(c, history)

    # 7. Quotas follow seed averages across every source.
    avgs = seed_averages(c, [s.track_id for s in seeds(c)])
    q = quotas(150, avgs, source_averages(c), {})
    assert sum(q.values()) == 150 and {src for _, src in q} == set(SOURCES)
    best, worst = max(avgs, key=avgs.get), min(avgs, key=avgs.get)
    if avgs[best] > avgs[worst]:
        assert sum(n for (s, _), n in q.items() if s == best) > sum(n for (s, _), n in q.items() if s == worst)

    # 8. Next generation: averages follow the decay rule (v3: by rating count, so a refill doesn't age ratings).
    with c:
        c.execute("INSERT INTO generations DEFAULT VALUES")
    check_averages(c, history)

    # 9. A later pull drops suggestions of anything already shown.
    pull = Pull(c, yt=None, lastfm=None)
    pull.process(seeds(c)[0], "ytmusic", 1, Item(t3["artist"], t3["title"], t3["duration_s"], t3["video_id"]))
    outcome = c.execute("SELECT outcome, reason FROM suggestions ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(outcome) == ("dropped", "already_rated_or_shown")


def test_controls_are_random(conn):
    """The run draws its own secret control rate once (1 in 4 to 1 in 10); each play is a control independently
    at that rate; a control is a random pool track."""
    from music_hunter import db
    from music_hunter.ratings import control_rate

    rng = random.Random(42)
    draws = [next_is_control(conn, rng) for _ in range(5000)]
    rate_ = db.get_setting(conn, "control_rate")
    assert 0.10 <= rate_ <= 0.25 and control_rate(conn, rng) == rate_      # drawn once, kept for the run
    assert abs(sum(draws) / len(draws) - rate_) < 0.025
    assert conn.execute("SELECT count(*) FROM events WHERE kind = 'setting_changed'").fetchone()[0] == 0  # never logged
    longest = run = 0                                  # no block structure: runs of non-controls vary freely
    for d in draws:
        run = 0 if d else run + 1
        longest = max(longest, run)
    assert longest > 10
    ranks = {c_rank for c_rank in (conn.execute("SELECT rank FROM plays WHERE id = ?",
             (show(conn, control=True, rng=rng),)).fetchone()[0] for _ in range(6))}
    assert len(ranks) > 1                              # not always the same (e.g. median) position


def test_upcoming_matches_next_play(conn):
    """v4: the preloaded track is the one that plays next when nothing changes in between, controls included;
    a control is a random *vetoed* track, a normal pick a random playable one."""
    from music_hunter import veto
    from music_hunter.ratings import show_next, upcoming

    counts = veto.update(conn)
    assert counts["active"] and counts["vetoed"] > 0
    rng = random.Random(5)
    controls = 0
    for _ in range(40):
        predicted = upcoming(conn, rng)
        assert upcoming(conn, rng) == predicted            # asking again doesn't redraw
        play = show_next(conn, rng)
        row = conn.execute("SELECT p.track_id, p.is_control, t.veto_state FROM plays p JOIN tracks t ON t.id = p.track_id"
                           " WHERE p.id = ?", (play,)).fetchone()
        assert row["track_id"] == predicted
        assert (row["veto_state"] == "vetoed") == bool(row["is_control"])
        controls += row["is_control"]
        skip(conn, play)
    assert 0 < controls < 40


def test_run_ends_and_results_unseal(conn, monkeypatch):
    """SPEC.md §16.6: results stay sealed (nothing hinting at controls) until the target number of controls is rated;
    then the run ends by itself."""
    from fastapi.testclient import TestClient
    from music_hunter import web
    from music_hunter.config import CONFIG

    monkeypatch.setattr(web.worker, "start_if_needed", lambda: None)
    monkeypatch.setitem(CONFIG["controls"], "target", 2)
    from music_hunter.ratings import control_rate
    control_rate(conn)                                     # drawn at the run's first play
    with TestClient(web.app) as client:
        for _ in range(3):
            assert client.get("/api/results").json()["sealed"]
            rate(conn, show(conn, control=True, rng=random.Random(1)), CLOSE)
            if client.get("/api/state").json()["run_complete"]:
                break
        state = client.get("/api/state").json()
        assert state["run_complete"] and not state["ready"]
        assert client.post("/api/next").status_code == 409
        res = client.get("/api/results").json()
        assert not res["sealed"] and res["control"]["n"] == 2 and 0.10 <= res["control_rate"] <= 0.25


def test_refill_for_new_seeds(conn, monkeypatch):
    """v4 placeholder rule (SPEC.md §17.8): pull once a new seed (an Add) is waiting and enough plays have passed;
    a seed counts as pulled once a pull has queried it, even if its sources returned nothing."""
    from music_hunter import web
    from music_hunter.config import CONFIG
    from music_hunter.pool import unpulled_seeds
    from music_hunter.pull import mark_pulled, seeds

    monkeypatch.setitem(CONFIG["pool"], "refill_below", 0)
    monkeypatch.setitem(CONFIG["pool"], "refill_min_plays", 2)
    monkeypatch.setitem(CONFIG["pool"], "target_size", 1)                 # no top-up due
    gen = conn.execute("SELECT max(id) FROM generations").fetchone()[0]

    def play(rating):
        p = show(conn)
        with conn:
            conn.execute("UPDATE plays SET generation = ? WHERE id = ?", (gen, p))
        rate(conn, p, rating)

    play(OFF)
    assert not unpulled_seeds(conn) and web.Worker.job_needed(conn) is None
    play(ADD)                                                             # a new seed, never pulled for…
    assert unpulled_seeds(conn) and web.Worker.job_needed(conn) == "pull"  # …and 2 plays since the pull
    mark_pulled(conn, [s.track_id for s in seeds(conn)])                  # the pull queried it (even with no results)
    assert not unpulled_seeds(conn) and web.Worker.job_needed(conn) is None
