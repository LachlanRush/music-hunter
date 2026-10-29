"""BPM measurement and the BPM / release-year score components (SPEC.md §15.6)."""

import numpy as np
import pytest

from music_hunter import db
from music_hunter.bpm import estimate, fold
from music_hunter.scoring import closeness, score_pool, target_bpm, target_year


def kicks(bpm: float, sr: int = 22050, secs: int = 60) -> np.ndarray:
    y = np.zeros(sr * secs, np.float32)
    n = int(0.08 * sr)
    kick = (np.sin(2 * np.pi * 60 * np.arange(n) / sr) * np.exp(-np.arange(n) / (0.02 * sr))).astype(np.float32)
    t = 0.0
    while int(t) + n < len(y):
        y[int(t):int(t) + n] += kick
        t += sr * 60 / bpm
    return y


@pytest.mark.parametrize("bpm", [122.0, 124.5, 128.0, 132.0, 140.0, 174.0])
def test_bpm_estimate_is_precise(bpm):
    assert estimate(kicks(bpm), 22050) == pytest.approx(bpm, abs=0.3)


@pytest.mark.parametrize("bpm", [95.0, 124.0, 140.0, 174.0])
def test_seed_prior_does_not_override_a_clear_tempo(bpm):
    """The 3:2 tie-break leans towards the seeds' tempo (here 138.4) but a clear beat keeps its own."""
    assert estimate(kicks(bpm), 22050, around=138.4) == pytest.approx(bpm, abs=0.3)


def test_candidates_cover_3_2_readings():
    from music_hunter.bpm import candidates
    assert candidates(94.6) == [94.6, 126.13, 141.9]     # the reading, its 3/2 and its 2/3 (folded)


def test_fold_undoes_half_and_double_tempo():
    assert fold(70) == 140 and fold(256) == 128 and fold(128) == 128


def test_closeness_and_unknowns():
    w = closeness([138, 146, 154.56, 170], 138, 4, window=16.56, edge=0.5)   # 138 ±12%
    assert w[0] == 1.0 and 0.5 < w[1] < 1.0 and w[2] == pytest.approx(0.5) and w[3] < 0.5
    out = closeness([138, 142, None, 160], 138, 4)
    assert out[0] == pytest.approx(1.0) and 0.5 < out[1] < 0.7 and out[3] < 0.01
    assert out[2] == pytest.approx(np.mean([out[0], out[1], out[3]]))   # unknown → neutral
    assert closeness([120, None], None, 4).tolist() == [0.0, 0.0]          # no target → no effect


def test_bpm_and_year_components(conn):
    c = conn
    pool = c.execute("SELECT id FROM tracks WHERE origin = 'pool' ORDER BY id").fetchall()
    with c:
        for i, (tid,) in enumerate(pool):
            c.execute("UPDATE tracks SET bpm = ?, release_date = ? WHERE id = ?",
                      (120 + (i % 30), f"{2000 + i % 27}-01-01", tid))
        c.execute("UPDATE tracks SET bpm = 130, release_date = '2025-06-01' WHERE origin = 'original_reference'")
    assert target_bpm(c) == (130.0, "from liked tracks") and target_year(c) == 2025.0
    rows = {r["track_id"]: r for r in score_pool(c)}
    best = max(rows.values(), key=lambda r: r["raw"]["bpm_match"])
    assert c.execute("SELECT bpm FROM tracks WHERE id = ?", (best["track_id"],)).fetchone()[0] == 130
    best_year = max(rows.values(), key=lambda r: r["raw"]["year_match"])
    assert c.execute("SELECT release_date FROM tracks WHERE id = ?", (best_year["track_id"],)).fetchone()[0][:4] == "2025"

    db.set_setting(c, "target_bpm", 160.0)                       # a target I set wins, and is logged
    assert target_bpm(c) == (160.0, "set")
    bpm_of = dict(c.execute("SELECT id, bpm FROM tracks WHERE origin = 'pool'").fetchall())
    scored = score_pool(c)                                       # window ±12%: 140.8–179.2
    assert {abs(bpm_of[r["track_id"]] - 160) <= 19.2 for r in scored} == {True, False}
    for r in scored:
        d = abs(bpm_of[r["track_id"]] - 160)
        if d <= 19.2:                                            # inside: closer is better, at least half credit
            assert r["raw"]["bpm_match"] == pytest.approx(1 - 0.5 * d / 19.2)
        else:                                                    # outside: below half credit
            assert r["raw"]["bpm_match"] < 0.5
    best = max(scored, key=lambda r: r["raw"]["bpm_match"])
    assert bpm_of[best["track_id"]] == max(bpm_of.values())     # the pool's closest tempo to 160 (149) ranks first
    assert c.execute("SELECT count(*) FROM events WHERE kind = 'setting_changed'").fetchone()[0] == 1


def test_settings_api(conn, monkeypatch):
    from fastapi.testclient import TestClient
    from music_hunter import web

    monkeypatch.setattr(web.worker, "start_if_needed", lambda: None)
    with TestClient(web.app) as client:
        s = client.post("/api/settings", json={"target_bpm": 126, "match_tempo": True}).json()
        assert s["effective_target_bpm"] == 126 and s["match_tempo"] and s["target_source"] == "set"
        assert client.post("/api/settings", json={"target_bpm": 500}).status_code == 400
        s = client.post("/api/settings", json={"target_bpm": None, "match_tempo": False}).json()
        assert s["target_bpm"] is None and s["target_source"] != "set"
        t = client.post("/api/next").json()
        assert "bpm" in t and "score" not in t
        r = client.post("/api/rate", json={"play_id": t["play_id"], "rating": 3, "played_bpm": 126.0,
                                           "playback_rate": 1.05}).json()
        assert r["rating"] == 3
    with db.connect() as c2:
        row = c2.execute("SELECT played_bpm, playback_rate, target_bpm FROM plays WHERE id = ?", (t["play_id"],)).fetchone()
        assert tuple(row)[:2] == (126.0, 1.05)
