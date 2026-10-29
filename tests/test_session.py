"""Milestone 6 check: a simulated listening session through the web API, with every log populated.

A synthetic listener rates by how close a track sounds to the references, plus noise.
The refill's network pull is replaced by re-using generation 1's unselected candidates
as a new generation, and downloads/analysis by copies of existing embeddings, so the
test runs offline on a temporary copy of the database.
"""

import json
import sqlite3

import numpy as np
import pytest

import music_hunter.config as config
from music_hunter import db, pool, web
from music_hunter.reweight import CFG as RW

REAL_DB = config.path("db")
PLAYS = 112


@pytest.fixture
def project(tmp_path, monkeypatch):
    if not REAL_DB.exists():
        pytest.skip("needs a filled pool: run `music-hunter pull` first")
    (tmp_path / "data").mkdir()
    src = sqlite3.connect(REAL_DB)
    with sqlite3.connect(tmp_path / "data" / "music_hunter.db") as dst:
        src.backup(dst)
    src.close()
    monkeypatch.setattr(config, "ROOT", tmp_path)
    c = db.connect()
    if c.execute("SELECT count(*) FROM plays").fetchone()[0]:
        pytest.skip("the real database already has plays; this test expects a fresh pool")
    for (rel,) in c.execute("SELECT file_path FROM tracks WHERE origin = 'pool'"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"placeholder audio")
    return c


class FakeFiller:
    """Stands in for download + analysis: a perturbed copy of an existing pool embedding."""

    def __init__(self, conn, generation):
        self.conn, self.generation, self.failed, self.ready = conn, generation, [], 0
        self.rng = np.random.default_rng(generation)
        self.base = [db.from_blob(r[0]) for r in conn.execute(
            "SELECT e.embedding FROM track_embeddings e JOIN tracks t ON t.id = e.track_id WHERE t.origin = 'pool'")]

    def process(self, cid):
        from music_hunter.clap import TrackEmbedding

        c = self.conn.execute("SELECT * FROM candidates WHERE id = ?", (cid,)).fetchone()
        v = self.base[self.rng.integers(len(self.base))] + self.rng.normal(0, 0.003, 512).astype(np.float32)
        v /= np.linalg.norm(v)
        rel = f"cache/{c['video_id']}.webm"
        (config.ROOT / rel).write_bytes(b"placeholder audio")
        with self.conn:
            tid = self.conn.execute(
                "INSERT INTO tracks (artist, title, file_path, duration_s, origin, candidate_id, generation,"
                " video_id, loudness_lufs, true_peak_db, gain_db) VALUES (?, ?, ?, 200, 'pool', ?, ?, ?, -9, 0, -5)",
                (c["artist"], c["title"], rel, cid, self.generation, c["video_id"])).lastrowid
            db.store_embedding(self.conn, tid, "laion/larger_clap_music", TrackEmbedding(v, v[None], [(0.0, 10.0)]))
            self.conn.execute("UPDATE candidates SET status = 'pool', track_id = ? WHERE id = ?", (tid, cid))
        self.ready += 1
        return True


def fake_job(job):
    """Worker job without network: a 'pull' re-offers generation 1's unselected candidates."""
    conn = db.connect()
    if job == "pull":
        with conn:
            gen = conn.execute("INSERT INTO generations DEFAULT VALUES").lastrowid
            for c in conn.execute("SELECT * FROM candidates WHERE status = 'not_selected'").fetchall():
                new = conn.execute(
                    "INSERT INTO candidates (generation, video_id, artist, title, duration_s, song_title,"
                    " song_artists, is_extended) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (gen, c["video_id"], c["artist"], c["title"], c["duration_s"], c["song_title"],
                     c["song_artists"], c["is_extended"])).lastrowid
                conn.execute(
                    "INSERT INTO suggestions (generation, seed_track_id, source, rank, artist, title, outcome,"
                    " candidate_id) SELECT ?, seed_track_id, source, rank, artist, title, 'new', ?"
                    " FROM suggestions WHERE candidate_id = ?", (gen, new, c["id"]))
                conn.execute("UPDATE candidates SET status = 'reoffered' WHERE id = ?", (c["id"],))
            conn.execute("UPDATE generations SET stats = '{}' WHERE id = ?", (gen,))
    else:
        gen = conn.execute("SELECT max(id) FROM generations").fetchone()[0]
    pool.fill(conn, gen)


class InlineThread:
    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def test_session(project, monkeypatch):
    from fastapi.testclient import TestClient

    c = project
    monkeypatch.setattr(pool, "Filler", FakeFiller)
    monkeypatch.setattr(web.threading, "Thread", InlineThread)
    jobs = []

    def run_job(self, conn, job):
        jobs.append(job)
        fake_job(job)
        self.state, self.message = "idle", ""
    monkeypatch.setattr(web.Worker, "_job", run_job)

    refs = np.stack([db.from_blob(r[0]) for r in c.execute(
        "SELECT e.embedding FROM track_embeddings e JOIN tracks t ON t.id = e.track_id"
        " WHERE t.origin = 'original_reference'")])
    rng = np.random.default_rng(7)

    def listener(track_id):
        e = db.from_blob(c.execute("SELECT embedding FROM track_embeddings WHERE track_id = ?",
                                   (track_id,)).fetchone()[0])
        sim = np.sort(refs @ e)[-3:].mean()
        return int(np.clip(round(3 + (sim - 0.975) * 80 + rng.normal(0, 0.7)), 1, 5))

    with TestClient(web.app) as client:
        for i in range(PLAYS):
            t = client.post("/api/next").json()
            assert "score" not in t and "control" not in t        # blind until rated
            if i % 23 == 11:
                client.post("/api/skip", json={"play_id": t["play_id"], "played_seconds": 8})
                continue
            rev = client.post("/api/rate", json={"play_id": t["play_id"], "rating": listener(t["track_id"]),
                                                 "played_seconds": 60 + i}).json()
            assert {"score", "rank", "control", "components", "provenance"} <= set(rev)
            if i == 40:                                            # mis-tap correction
                client.post("/api/change", json={"rating": max(1, rev["rating"] - 1)})
            if i == 3:                                             # player-side events
                client.post("/api/event", json={"kind": "playing", "play_id": t["play_id"], "data": {"latency_ms": 180}})
                client.post("/api/event", json={"kind": "stall", "play_id": t["play_id"], "data": {"ms": 900, "at_s": 30}})
                assert client.post("/api/event", json={"kind": "bogus"}).status_code == 400
        results = client.get("/api/results").json()
        assert client.get("/results").status_code == 200

    plays = c.execute("SELECT * FROM plays ORDER BY play_number").fetchall()
    assert len(plays) == PLAYS

    # Every play logged with all components, provenance, rank, generation, timing.
    for p in plays:
        comps = json.loads(p["components"])
        assert set(comps) == {"audio_match", "dislike_match", "source_score", "seed_score", "agreement",
                              "personal_model", "diversity_penalty"}
        assert json.loads(p["provenance"]) and p["rank"] >= 1 and p["generation"] >= 1
        assert p["played_seconds"] is not None and p["shown_at"]

    # Controls: exactly one per complete block of 5, always the median-scored track.
    for b in range(PLAYS // 5):
        assert sum(p["is_control"] for p in plays[b * 5:b * 5 + 5]) == 1
    for p in plays:
        if p["is_control"]:
            assert p["rank"] == (p["pool_size"] - 1) // 2 + 1
    positions = {next(i for i in range(5) if plays[b * 5 + i]["is_control"]) for b in range(PLAYS // 5)}
    assert len(positions) > 1                                      # not a countable pattern

    # Re-weighting on schedule, each logged with a new weight set and later evaluated.
    learning = c.execute("SELECT count(*) FROM plays WHERE is_control = 0 AND rating IS NOT NULL").fetchone()[0]
    expected = list(range(RW["first_at"], learning + 1, RW["every"]))
    rws = c.execute("SELECT * FROM reweightings ORDER BY at_ratings").fetchall()
    assert [r["at_ratings"] for r in rws] == expected
    assert c.execute("SELECT count(*) FROM weight_sets").fetchone()[0] == 1 + len(rws)
    for r in rws:
        assert r["new_concordance"] >= r["old_concordance"] - 1e-9
        if r["at_ratings"] + RW["eval_window"] <= learning:
            assert r["eval_n"] == RW["eval_window"]
            assert (r["eval_new_concordance"] is not None) == (r["eval_pairs"] > 0)  # all-equal ratings: no pairs

    # Personal model: zero until 50 learning ratings, then active.
    seen = 0
    for p in plays:
        model = json.loads(p["components"])["personal_model"]["raw"]
        if seen < 50:
            assert model == 0.0
        elif not p["is_control"]:
            assert model != 0.0
        if not p["is_control"] and p["rating"] is not None:
            seen += 1

    # A refill ran in the background once fewer than 25 tracks were left, as a new generation.
    assert "pull" in jobs
    gens = c.execute("SELECT id, stats FROM generations ORDER BY id").fetchall()
    assert len(gens) >= 2
    for g in gens:
        stats = json.loads(g["stats"])
        assert {"seed_averages", "source_averages", "quotas", "weights", "pool_size"} <= set(stats)
    assert {"raw_suggestions", "filter_reasons"} <= set(json.loads(gens[0]["stats"]))
    assert any(p["generation"] == gens[-1]["id"] for p in plays)

    # Operational log.
    kinds = {r[0]: r[1] for r in c.execute("SELECT kind, count(*) FROM events GROUP BY kind")}
    assert kinds["shown"] == PLAYS and kinds["reweighted"] == len(rws) and kinds["rating_changed"] == 1
    assert kinds["job_start"] == kinds["job_done"] == len(jobs) and kinds["server_start"] == 1
    assert kinds["fill_start"] >= len(jobs) and kinds["fill_done"] >= len(jobs)
    assert kinds["player_playing"] == 1 and kinds["player_stall"] == 1
    from music_hunter.report import report
    report(c)

    # Results view has everything to show.
    assert results["verdict"] in ("meets", "below")
    assert results["ranked"]["n"] and results["control"]["n"]
    assert results["rolling"]["ranked"] and results["rolling"]["control"]
    assert all(s["points"] for s in results["source_averages"])
    assert len(results["reweightings"]) == len(rws)
    assert results["totals"]["skipped"] == sum(1 for p in plays if p["skipped_at"])
    print(f"\nranked {results['ranked']['mean']:.2f} vs control {results['control']['mean']:.2f} "
          f"(diff {results['difference']['diff']:+.2f}), keep {results['ranked']['keep']:.0%} vs "
          f"{results['control']['keep']:.0%}; re-weightings at {expected}; generations {len(gens)}")
    for r in rws:
        print(f"  at {r['at_ratings']}: concordance {r['old_concordance']:.3f} -> {r['new_concordance']:.3f};"
              f" next 15: old {r['eval_old_concordance']}, new {r['eval_new_concordance']}")
