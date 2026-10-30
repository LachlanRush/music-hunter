"""v3 milestone 3 check: scoring within each seed's pool (SPEC.md §16.4) and the weight guardrails."""

import numpy as np

import music_hunter.scoring as scoring
from music_hunter.pull import seeds
from music_hunter.reweight import CAPS, FLOORS, clamp
from music_hunter.scoring import COMPONENTS, CFG, score_pool


def suggesters(conn):
    out = {}
    for cid, sid in conn.execute("SELECT DISTINCT candidate_id, seed_track_id FROM suggestions"):
        out.setdefault(cid, set()).add(sid)
    return out


def test_one_source_seed_can_top_its_pool(conn):
    """A seed whose tracks were each suggested by one source only still gets full agreement credit in its
    own pool, where tracks of better-known seeds need multi-source agreement to reach the top."""
    obscure = seeds(conn)[-1].track_id
    with conn:   # the obscure seed's pool: tracks only it suggested, through one source each
        conn.execute("DELETE FROM suggestions WHERE seed_track_id = ? AND candidate_id IN"
                     " (SELECT candidate_id FROM suggestions WHERE seed_track_id != ?)", (obscure, obscure))
        conn.execute("DELETE FROM suggestions WHERE seed_track_id = ? AND id NOT IN"
                     " (SELECT min(id) FROM suggestions WHERE seed_track_id = ? GROUP BY candidate_id)",
                     (obscure, obscure))
    rows = score_pool(conn)
    sug = suggesters(conn)
    mine = [r for r in rows if sug.get(r["candidate_id"]) == {obscure}]
    others = [r for r in rows if obscure not in sug.get(r["candidate_id"], set())]
    assert mine and all(r["norm"]["agreement"] == 1.0 for r in mine)
    assert all(r["scored_for_seed"] == obscure for r in mine)
    assert min(r["norm"]["agreement"] for r in others) < 1.0          # elsewhere agreement has to be earned


def test_track_from_several_seeds_keeps_its_best_pool(conn):
    rows = score_pool(conn)
    sug = suggesters(conn)
    multi = [r for r in rows if len(sug.get(r["candidate_id"], ())) > 1]
    assert multi
    assert all(r["scored_for_seed"] in sug[r["candidate_id"]] for r in rows if r["scored_for_seed"])


def test_seed_average_steers_scores(conn, monkeypatch):
    """Seed averages decide scores too (seed_score), not only pool shares: a seed that does well lifts its tracks."""
    target = seeds(conn)[0].track_id
    before = {r["track_id"]: r["score"] for r in score_pool(conn)}
    real = scoring.seed_averages
    monkeypatch.setattr(scoring, "seed_averages",
                        lambda c, ids: {s: (2.8 if s == target else a) for s, a in real(c, ids).items()})
    after = score_pool(conn)
    lifted = [r for r in after if r["scored_for_seed"] == target]
    assert lifted and all(r["norm"]["seed_score"] == 1.0 for r in lifted)
    assert np.mean([r["score"] - before[r["track_id"]] for r in lifted]) > 0


def test_guardrails():
    caps = dict(zip(COMPONENTS, CAPS))
    floors = dict(zip(COMPONENTS, FLOORS))
    assert max(caps.values()) <= 0.35 and floors["seed_score"] >= 0.10 and floors["agreement"] >= 0.15
    w = clamp(np.array([np.sign(CFG["weights"][c]) * 5.0 for c in COMPONENTS]))   # far too big
    assert np.all(np.abs(w) <= 0.35 + 1e-12)
    w = clamp(np.zeros(len(COMPONENTS)))                                           # far too small
    assert np.all(np.abs(w) >= FLOORS - 1e-12)
    for c, v in CFG["weights"].items():                                            # initial weights obey them
        assert floors[c] - 1e-12 <= abs(v) <= caps[c] + 1e-12, c
