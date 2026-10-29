"""Re-weighting v2 (SPEC.md §15.8): small steps, guardrails, never a worse fit, and a weight
set that only looked good on a few early (lucky) ratings losing its place as ratings accumulate."""

import json

import numpy as np

from music_hunter.reweight import CFG, CAPS, FLOORS, SIGNS, after_rating, score_sets, to_vec
from music_hunter.scoring import COMPONENTS, current_weights


def add_play(c, track_id, n, comps, rating):
    with c:
        c.execute("INSERT INTO plays (track_id, generation, play_number, is_control, components, score, rank,"
                  " pool_size, provenance, rating, rated_at) VALUES (?, 1, ?, 0, ?, 0, 1, 100, '[]', ?,"
                  " strftime('%Y-%m-%d %H:%M:%f', 'now', ?))",
                  (track_id, n, json.dumps({k: {"raw": v, "norm": v} for k, v in comps.items()}), rating,
                   f"+{n} seconds"))


def test_reweighting_v2(conn):
    c = conn
    rng = np.random.default_rng(0)
    track = c.execute("SELECT id FROM tracks WHERE origin = 'pool' LIMIT 1").fetchone()[0]
    current_weights(c)                                               # initial set from config
    lucky = {**dict(zip(COMPONENTS, to_vec({}))), "seed_score": 0.9, "audio_match": 0.2}
    with c:
        lucky_id = c.execute("INSERT INTO weight_sets (reason, weights) VALUES ('test: lucky', ?)",
                             (json.dumps(lucky),)).lastrowid
        # put the initial set back on top, so "lucky" is just an earlier set on record
        c.execute("INSERT INTO weight_sets (reason, weights) VALUES ('initial', ?)",
                  (json.dumps(dict(zip(COMPONENTS, to_vec({})))),))

    def play(i, driver):
        comps = {k: float(rng.uniform()) for k in COMPONENTS}
        rating = int(np.clip(1 + np.floor(comps[driver] * 4), 1, 4))   # the driver decides Off … Add
        add_play(c, track, i, comps, rating)

    for i in range(1, 21):                       # early ratings: explained by seed_score (lucky)
        play(i, "seed_score")
    early = score_sets(c)
    assert max(early, key=early.get) == lucky_id

    updates = []
    after_rating(c)                              # first update due at 20
    for i in range(21, 101):                     # later ratings: explained by audio_match
        play(i, "audio_match")
        result = after_rating(c)
        if result:
            updates.append(result)
    rws = c.execute("SELECT * FROM reweightings ORDER BY at_ratings").fetchall()
    assert [r["at_ratings"] for r in rws] == list(range(CFG["first_at"], 101, CFG["every"]))

    for r in rws:
        old = to_vec(json.loads(c.execute("SELECT weights FROM weight_sets WHERE id = ?", (r["old_weight_set_id"],)).fetchone()[0]))
        new = to_vec(json.loads(c.execute("SELECT weights FROM weight_sets WHERE id = ?", (r["new_weight_set_id"],)).fetchone()[0]))
        assert np.abs(new - old).max() <= CFG["step"] + 1e-9                     # small steps only
        assert np.all(np.sign(new) == SIGNS)                                       # no sign changes
        assert np.all(np.abs(new) >= FLOORS - 1e-9) and np.all(np.abs(new) <= CAPS + 1e-9)   # guardrails
        assert r["new_concordance"] >= r["old_concordance"] - 1e-12              # never a worse fit
        assert r["target"] in ("current", "search") or r["target"].startswith("set ")

    late = score_sets(c)
    assert max(late, key=late.get) != lucky_id                                     # lucky set lost its place
    assert late[lucky_id] < early[lucky_id]
    final = to_vec(current_weights(c)[1])
    start = to_vec({})
    assert final[COMPONENTS.index("audio_match")] > start[COMPONENTS.index("audio_match")]   # drifted the right way
    assert all(u["moved_towards"] != f"set {lucky_id}" for u in updates[-5:])
    a, sd = COMPONENTS.index("audio_match"), COMPONENTS.index("seed_score")
    print(f"\nlucky set score: {early[lucky_id]:.3f} (best at 20 ratings) -> {late[lucky_id]:.3f} at 100"
          f" (best now: set {max(late, key=late.get)}, {max(late.values()):.3f})")
    for r in rws[::3]:
        w = to_vec(json.loads(c.execute("SELECT weights FROM weight_sets WHERE id = ?", (r["new_weight_set_id"],)).fetchone()[0]))
        print(f"  at {r['at_ratings']:3d}: moved towards {r['target']:8s} audio {w[a]:.2f}  seed {w[sd]:.2f}"
              f"  fit {r['old_concordance']:.3f} -> {r['new_concordance']:.3f}")
