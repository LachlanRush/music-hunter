"""Milestone 4 check: scripted ratings produce the expected updates (SPEC.md §3, §5, §7, §8).

Runs against a copy of the real database (needs a filled pool) in a temporary project
folder, with placeholder audio files, so the real pool and folders are untouched.
"""

import pytest

import music_hunter.config as config
from music_hunter.pool import quotas
from music_hunter.pull import Pull, seeds
from music_hunter.ratings import (change_last_rating, negative_set, positive_set, rate, seed_averages, show,
                                  source_averages)
from music_hunter.scoring import score_pool
from music_hunter.sources import Item

def provenance(c, track_id):
    """(seeds, sources) that suggested a track — queried independently of ratings.py."""
    rows = c.execute("SELECT DISTINCT s.seed_track_id, s.source FROM suggestions s"
                     " JOIN tracks t ON t.candidate_id = s.candidate_id WHERE t.id = ?", (track_id,)).fetchall()
    return {r[0] for r in rows}, {r[1] for r in rows}


def expected_averages(c, history, generation_weights=None):
    """§5 formula from the test's own record of learning ratings: [(track_id, rating, generation)]."""
    gen_now = c.execute("SELECT max(id) FROM generations").fetchone()[0]
    sums = {}
    for track_id, rating, gen in history:
        w = 0.5 ** (gen_now - gen)
        seeds_, sources_ = provenance(c, track_id)
        for key in seeds_ | sources_:
            t, n = sums.get(key, (0.0, 0.0))
            sums[key] = (t + w * rating, n + w)
    return {k: (3 * 3 + t) / (3 + n) for k, (t, n) in sums.items()}


def check_averages(c, history):
    expected = expected_averages(c, history)
    seed_ids = [s.track_id for s in seeds(c)]
    actual = {**seed_averages(c, seed_ids), **source_averages(c)}
    for key, value in actual.items():
        assert value == pytest.approx(expected.get(key, 3.0)), key


def track_of(c, play_id):
    return c.execute("SELECT t.* FROM plays p JOIN tracks t ON t.id = p.track_id WHERE p.id = ?",
                     (play_id,)).fetchone()


def test_scripted_ratings(conn):
    c = conn
    root = config.ROOT
    gen = c.execute("SELECT max(id) FROM generations").fetchone()[0]
    originals = [r[0] for r in c.execute("SELECT id FROM tracks WHERE origin = 'original_reference'")]
    history = []

    # Fresh pool: everything at the prior, positive set = the 5 originals at weight 2.
    check_averages(c, history)
    ids, _, w = positive_set(c)
    assert ids == originals and list(w) == [2.0] * 5
    assert negative_set(c)[0] == []
    before = {r["track_id"]: r for r in score_pool(c)}
    pool_size = c.execute("SELECT count(*) FROM tracks WHERE origin = 'pool'").fetchone()[0]
    assert len(before) == pool_size

    # 1. Learning play rated 5: references/, positive weight 2, becomes a seed, averages 3.5.
    p1 = show(c)
    t1 = track_of(c, p1)
    logged = c.execute("SELECT * FROM plays WHERE id = ?", (p1,)).fetchone()
    assert logged["rank"] == 1 and logged["pool_size"] == pool_size and logged["is_control"] == 0
    old_file = root / t1["file_path"]
    rate(c, p1, 5, played_seconds=200)
    history.append((t1["id"], 5, gen))
    t1 = track_of(c, p1)
    assert t1["file_path"].startswith("references/") and (root / t1["file_path"]).exists()
    assert not old_file.exists()
    ids, _, w = positive_set(c)
    assert t1["id"] in ids and w[ids.index(t1["id"])] == 2.0
    assert any(s.track_id == t1["id"] and s.video_id == t1["video_id"] for s in seeds(c))
    check_averages(c, history)
    for key in set.union(*provenance(c, t1["id"])):
        assert expected_averages(c, history)[key] == pytest.approx(3.5)

    # Pool re-scores: rated track gone, audio match can only rise (new 5-rated reference),
    # diversity penalty now active (last play).
    after = {r["track_id"]: r for r in score_pool(c)}
    assert len(after) == pool_size - 1 and t1["id"] not in after
    rises = [after[t]["raw"]["audio_match"] - before[t]["raw"]["audio_match"] for t in after]
    assert min(rises) >= -1e-6 and max(rises) > 0
    assert max(r["raw"]["diversity_penalty"] for r in after.values()) > 0

    # 2. Rated 1: audio held in data/undo/, file_path cleared, negative set weight 2.
    p2 = show(c)
    t2 = track_of(c, p2)
    rate(c, p2, 1)
    history.append((t2["id"], 1, gen))
    undo = c.execute("SELECT undo_path FROM plays WHERE id = ?", (p2,)).fetchone()[0]
    assert track_of(c, p2)["file_path"] is None and (root / undo).exists()
    ids, _, w = negative_set(c)
    assert ids == [t2["id"]] and list(w) == [2.0]
    dislike = [r["raw"]["dislike_match"] for r in score_pool(c)]
    assert max(dislike) == pytest.approx(1.0) and min(dislike) == pytest.approx(0.0)
    check_averages(c, history)

    # 3. Mis-tap correction 1 → 3: file restored to keepers/, out of the negative set.
    assert change_last_rating(c, 3) == p2
    history[-1] = (t2["id"], 3, gen)
    t2 = track_of(c, p2)
    assert t2["file_path"].startswith("keepers/") and (root / t2["file_path"]).exists()
    assert not (root / undo).exists()
    assert negative_set(c)[0] == []
    check_averages(c, history)

    # 4. Rated 2 (2-rated dislikes top out at 0.5), then 4: the 2's audio is deleted for good.
    p3 = show(c)
    t3 = track_of(c, p3)
    rate(c, p3, 2)
    history.append((t3["id"], 2, gen))
    undo3 = root / c.execute("SELECT undo_path FROM plays WHERE id = ?", (p3,)).fetchone()[0]
    assert max(r["raw"]["dislike_match"] for r in score_pool(c)) == pytest.approx(0.5)
    p4 = show(c)
    t4 = track_of(c, p4)
    rate(c, p4, 4)
    history.append((t4["id"], 4, gen))
    assert not undo3.exists()
    assert c.execute("SELECT count(*) FROM track_embeddings WHERE track_id = ?", (t3["id"],)).fetchone()[0] == 1
    t4 = track_of(c, p4)
    assert t4["file_path"].startswith("references/")
    ids, _, w = positive_set(c)
    assert w[ids.index(t4["id"])] == 1.0
    check_averages(c, history)

    # 5. Control play rated 5: keepers/, no credit, not a reference, not a seed.
    averages_before = ({**seed_averages(c, [s.track_id for s in seeds(c)]), **source_averages(c)})
    p5 = show(c, control=True)
    t5 = track_of(c, p5)
    rate(c, p5, 5)
    t5 = track_of(c, p5)
    assert t5["file_path"].startswith("keepers/")
    assert t5["id"] not in positive_set(c)[0]
    assert all(s.track_id != t5["id"] for s in seeds(c))
    assert {**seed_averages(c, [s.track_id for s in seeds(c)]), **source_averages(c)} == averages_before

    # 6. Quotas follow seed averages: the 5-rated track's seeds get more of the next pull.
    avgs = seed_averages(c, [s.track_id for s in seeds(c)])
    q = quotas(75, avgs, source_averages(c), {})
    best_seed = max(avgs, key=avgs.get)
    worst_seed = min(avgs, key=avgs.get)
    if avgs[best_seed] > avgs[worst_seed]:
        assert sum(n for (s, _), n in q.items() if s == best_seed) > sum(n for (s, _), n in q.items() if s == worst_seed)

    # 7. Next generation: earlier ratings decay to weight 0.5.
    with c:
        c.execute("INSERT INTO generations DEFAULT VALUES")
    check_averages(c, history)
    key = next(iter(provenance(c, t1["id"])[1]))
    assert source_averages(c)[key] == pytest.approx(expected_averages(c, history)[key])
    assert source_averages(c)[key] != pytest.approx(
        (9 + sum(r for t, r, _ in history if key in provenance(c, t)[1]))
        / (3 + sum(1 for t, _, _ in history if key in provenance(c, t)[1])))

    # 8. A later pull drops suggestions of anything already shown.
    pull = Pull(c, yt=None, lastfm=None)
    seed = seeds(c)[0]
    pull.process(seed, "ytmusic", 1, Item(t3["artist"], t3["title"], t3["duration_s"], t3["video_id"]))
    outcome = c.execute("SELECT outcome, reason FROM suggestions ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(outcome) == ("dropped", "already_rated_or_shown")
