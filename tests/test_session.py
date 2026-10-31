"""Simulated listening session through the web API, with every log populated (v4: veto, random picks,
injected vetoed tracks, rotating pool).

A synthetic listener rates by how close a track sounds to the references, plus noise.
The refill's network pull is replaced by re-using generation 1's unselected candidates
as a new generation, and downloads/analysis by copies of existing embeddings, so the
test runs offline on a temporary copy of the database.
"""

import json
import random

import numpy as np

import music_hunter.config as config
from music_hunter import db, pool, web
from music_hunter.reweight import CFG as RW
from music_hunter.scoring import COMPONENTS

PLAYS = 112


class FakeFiller:
    """Stands in for download + analysis: a perturbed copy of an existing pool embedding."""

    def __init__(self, conn, generation):
        self.conn, self.generation, self.failed, self.ready = conn, generation, [], 0
        self.rng = np.random.default_rng(generation)
        self.base = [db.from_blob(r[0]) for r in conn.execute(
            "SELECT e.embedding FROM track_embeddings e JOIN tracks t ON t.id = e.track_id WHERE t.origin = 'pool'")]

    def process(self, cid, seed=None):
        from music_hunter.embedding import TrackEmbedding

        c = self.conn.execute("SELECT * FROM candidates WHERE id = ?", (cid,)).fetchone()
        v = self.base[self.rng.integers(len(self.base))] + self.rng.normal(0, 0.003, len(self.base[0])).astype(np.float32)
        v /= np.linalg.norm(v)
        rel = f"cache/{c['video_id']}.webm"
        (config.ROOT / rel).write_bytes(b"placeholder audio")
        with self.conn:
            tid = self.conn.execute(
                "INSERT INTO tracks (artist, title, file_path, duration_s, origin, candidate_id, generation,"
                " video_id, loudness_lufs, true_peak_db, gain_db) VALUES (?, ?, ?, 200, 'pool', ?, ?, ?, -9, 0, -5)",
                (c["artist"], c["title"], rel, cid, self.generation, c["video_id"])).lastrowid
            db.store_embedding(self.conn, tid, "laion/larger_clap_music", TrackEmbedding(v, v[None], [(0.0, 10.0)]))
            self.conn.execute("UPDATE candidates SET status = 'pool', track_id = ?, pool_seed = ? WHERE id = ?",
                              (tid, seed, cid))
        self.ready += 1
        return True


def fake_job(job):
    """Worker job without network: a 'pull' re-offers earlier generations' unused candidates; 'restore' writes
    placeholder audio."""
    conn = db.connect()
    if job == "pull":
        with conn:
            gen = conn.execute("INSERT INTO generations DEFAULT VALUES").lastrowid
            # One per video, and never a video already downloaded (as a real pull's filters ensure).
            for c in conn.execute(
                    "SELECT * FROM candidates WHERE status IN ('not_selected', 'candidate') AND generation < ?"
                    " AND video_id NOT IN (SELECT video_id FROM tracks WHERE video_id IS NOT NULL)"
                    " GROUP BY video_id", (gen,)).fetchall():
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
        from music_hunter.pull import mark_pulled, seeds
        mark_pulled(conn, [s.track_id for s in seeds(conn)])          # a real pull queries every seed
    elif job == "restore":
        for tid in pool.missing_audio(conn):
            pool.restore_audio(conn, tid)
        return
    else:
        gen = conn.execute("SELECT max(id) FROM generations").fetchone()[0]
    pool.fill(conn, gen)


def fake_restore(conn, track_id):
    """Stands in for re-downloading a track's audio: a placeholder file."""
    t = conn.execute("SELECT video_id, file_path FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if not t["file_path"]:
        rel = f"cache/{t['video_id']}.webm"
        (config.ROOT / rel).write_bytes(b"placeholder audio")
        with conn:
            conn.execute("UPDATE tracks SET file_path = ? WHERE id = ?", (rel, track_id))
    return True


class InlineThread:
    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def test_session(conn, monkeypatch):
    from fastapi.testclient import TestClient

    c = conn
    random.seed(5)                                   # control draws are random (SPEC.md §15.4); keep the test repeatable
    monkeypatch.setattr(pool, "Filler", FakeFiller)
    monkeypatch.setattr(pool, "restore_audio", fake_restore)
    # The real pool size changes as listening goes on; set thresholds so a refill happens mid-session.
    from music_hunter.config import CONFIG
    from music_hunter.pool import unrated_pool_size
    start = unrated_pool_size(c)
    monkeypatch.setitem(CONFIG["pool"], "target_size", start)
    monkeypatch.setitem(CONFIG["pool"], "refill_below", start - 40)
    monkeypatch.setattr(web.threading, "Thread", InlineThread)
    jobs = []

    def run_job(self, conn, job):
        jobs.append(job)
        fake_job(job)
        self.state, self.message = "idle", ""
    monkeypatch.setattr(web.Worker, "_job", run_job)
    monkeypatch.setitem(CONFIG["tempo"], "locked", False)        # exercises the player's tempo controls
    monkeypatch.setitem(CONFIG["controls"], "target", 10**6)     # the run doesn't end during the session…

    refs = np.stack([db.from_blob(r[0]) for r in c.execute(
        "SELECT e.embedding FROM track_embeddings e JOIN tracks t ON t.id = e.track_id"
        " WHERE t.origin = 'original_reference'")])
    rng = np.random.default_rng(7)

    def closeness(e):
        return np.sort(refs @ e)[-3:].mean()

    # Ratings follow closeness to the seeds relative to the pool (any audio model's similarity scale): roughly the
    # closest fifth Add, the next two fifths Close, the rest Off, with noise.
    pool_sims = [closeness(db.from_blob(r[0])) for r in c.execute(
        "SELECT e.embedding FROM track_embeddings e JOIN tracks t ON t.id = e.track_id WHERE t.origin = 'pool'")]
    cut_close, cut_add = np.percentile(pool_sims, [40, 80])
    spread = np.std(pool_sims) or 1.0

    def listener(track_id):
        e = db.from_blob(c.execute("SELECT embedding FROM track_embeddings WHERE track_id = ?",
                                   (track_id,)).fetchone()[0])
        sim = closeness(e) + rng.normal(0, 0.5 * spread)
        return 3 if sim >= cut_add else 2 if sim >= cut_close else 1           # Off … Add

    with TestClient(web.app) as client:
        settings = client.post("/api/settings", json={"target_bpm": 128.0, "match_tempo": True}).json()
        assert settings["effective_target_bpm"] == 128.0
        for i in range(PLAYS):
            t = client.post("/api/next").json()
            assert not {"score", "rank", "control", "components", "provenance"} & set(t)   # blind
            assert t["title"] is None and t["art"] is None                               # names hidden
            if i % 23 == 11:
                client.post("/api/skip", json={"play_id": t["play_id"], "played_seconds": 8})
                continue
            stretched = {"played_bpm": 128.0, "playback_rate": 1.04} if i % 3 == 0 else {}
            rev = client.post("/api/rate", json={"play_id": t["play_id"], "rating": listener(t["track_id"]),
                                                 "played_seconds": 60 + i, **stretched}).json()
            assert not {"score", "rank", "control", "components", "provenance"} & set(rev)  # still blind
            assert rev["title"] is not None                                             # names revealed
            if i == 40:                                            # mis-tap correction
                client.post("/api/change", json={"rating": max(1, rev["rating"] - 1)})
            if i == 3:                                             # player-side events
                client.post("/api/event", json={"kind": "playing", "play_id": t["play_id"], "data": {"latency_ms": 180}})
                client.post("/api/event", json={"kind": "stall", "play_id": t["play_id"], "data": {"ms": 900, "at_s": 30}})
                assert client.post("/api/event", json={"kind": "bogus"}).status_code == 400
        sealed = client.get("/api/results").json()                  # …so results stay sealed: nothing about controls
        assert sealed == {"sealed": True, "rated": sealed["rated"], "target_controls": 10**6}
        done = c.execute("SELECT count(*) FROM plays WHERE is_control = 1 AND rating IS NOT NULL").fetchone()[0]
        monkeypatch.setitem(CONFIG["controls"], "target", done)      # …until the target is reached: the run ends
        assert client.get("/api/state").json()["run_complete"] and client.post("/api/next").status_code == 409
        results = client.get("/api/results").json()
        assert not results["sealed"]
        assert client.get("/results").status_code == 200

    plays = c.execute("SELECT * FROM plays ORDER BY play_number").fetchall()
    assert len(plays) == PLAYS
    skipped = [i for i in range(PLAYS) if i % 23 == 11]          # skipped without a rating: counted as Close, marked
    auto = [p for p in plays if p["auto_rated"]]
    assert len(auto) == len(skipped) and all(p["rating"] == config.CONFIG["ratings"]["unrated_counts_as"] for p in auto)

    # Every play logged with all components, provenance, rank, generation, timing.
    for p in plays:
        comps = json.loads(p["components"])
        assert set(comps) == set(COMPONENTS)
        assert json.loads(p["provenance"]) and p["rank"] >= 1 and p["generation"] >= 1
        assert p["played_seconds"] is not None and p["shown_at"]

    # v4: controls are injected vetoed tracks (at the run's secret rate, once anything is vetoed); normal picks are
    # random playable tracks, not the top score. Every play logs its veto details and settings version.
    controls = [p for p in plays if p["is_control"]]
    assert 0.03 < len(controls) / PLAYS < 0.3
    assert all(json.loads(p["veto"])["state"] == "vetoed" for p in controls)
    assert all(json.loads(p["veto"])["state"] == "playable" for p in plays if not p["is_control"] and p["veto"])
    assert len({p["rank"] for p in plays if not p["is_control"]}) > 5          # random picks: all over the ranking
    assert all(p["settings_version"] for p in plays)
    v = json.loads(plays[-1]["veto"])
    assert {"fixed_10pct", "fixed_20pct", "sd_1.0", "sd_1.25", "sd_1.5", "seed_closeness_below_0.75", "backstop"} \
        == set(v["rules"]) and v["analysed"] >= config.CONFIG["veto"]["min_analysed"]
    vetoed = c.execute("SELECT count(*), count(file_path) FROM tracks WHERE veto_state = 'vetoed'"
                       " AND id NOT IN (SELECT track_id FROM plays)").fetchone()
    assert vetoed[0] > 0 and vetoed[1] == 0                                  # vetoed tracks keep no audio

    if not RW.get("enabled", True):                                          # v4: re-weighting is off
        assert not c.execute("SELECT count(*) FROM reweightings").fetchone()[0]
        RW_ON = False
    else:
        RW_ON = True

    # Re-weighting on schedule, each logged with a new weight set and later evaluated.
    learning = c.execute("SELECT count(*) FROM plays WHERE rating IS NOT NULL").fetchone()[0]   # v3: controls learn too
    expected = list(range(RW["first_at"], learning + 1, RW["every"])) if RW_ON else []
    rws = c.execute("SELECT * FROM reweightings ORDER BY at_ratings").fetchall()
    assert [r["at_ratings"] for r in rws] == expected
    assert c.execute("SELECT count(*) FROM weight_sets").fetchone()[0] == 1 + len(rws)
    for r in rws:
        assert r["new_concordance"] >= r["old_concordance"] - 1e-9
        if r["at_ratings"] + RW["eval_window"] <= learning:
            assert r["eval_n"] == RW["eval_window"]
            assert (r["eval_new_concordance"] is not None) == (r["eval_pairs"] > 0)  # all-equal ratings: no pairs

    # Personal model: zero until the minimum ratings (with enough Offs and likes), then active;
    # its value is the predicted rating on the rating scale.
    from music_hunter.scoring import CFG as SC
    from music_hunter.ratings import NEGATIVE_WEIGHTS, POSITIVE_WEIGHTS
    seen = likes = offs = 0
    for p in plays:
        comp = json.loads(p["components"])["personal_model"]
        ready = seen >= SC["personal_model_min_ratings"] and min(likes, offs) >= SC["personal_model_min_each"]
        if not ready:
            assert comp["raw"] == 0.0
        elif not p["is_control"]:
            assert comp["raw"] != 0.0 and 0.0 <= comp["norm"] <= 1.0
        if p["rating"] is not None:                       # v3: control ratings count too
            seen += 1
            likes += p["rating"] in POSITIVE_WEIGHTS
            offs += p["rating"] in NEGATIVE_WEIGHTS

    # A refill ran in the background once fewer than refill_below tracks were left, as a new generation.
    assert "pull" in jobs, (jobs, unrated_pool_size(c), web.Worker.job_needed(c),
                            [tuple(r) for r in c.execute("SELECT generation, status, count(*) FROM candidates GROUP BY 1, 2")])
    gens = c.execute("SELECT id, stats FROM generations ORDER BY id").fetchall()
    assert len(gens) >= 2
    for g in gens:
        stats = json.loads(g["stats"])
        assert {"seed_averages", "source_averages", "quotas", "weights", "pool_size"} <= set(stats), \
            (g["id"], sorted(stats), [__import__("json").loads(r[0])["error"] for r in c.execute("SELECT data FROM events WHERE kind = 'job_error' LIMIT 1")])
    assert {"raw_suggestions", "filter_reasons"} <= set(json.loads(gens[0]["stats"]))
    assert len({p["generation"] for p in plays}) >= 2              # listening carried on into a refilled generation

    # Operational log.
    kinds = {r[0]: r[1] for r in c.execute("SELECT kind, count(*) FROM events GROUP BY kind")}
    assert kinds["shown"] == PLAYS and kinds.get("reweighted", 0) == len(rws) and kinds["rating_changed"] == 1
    assert kinds["job_start"] == kinds["job_done"] == len(jobs) and kinds["server_start"] == 1
    fills = [j for j in jobs if j in ("pull", "fill")]
    assert kinds["fill_done"] >= len(fills)
    churn = [json.loads(r[0]) for r in c.execute("SELECT data FROM events WHERE kind = 'veto_update'")]
    print(f"\nveto churn over {PLAYS} plays: {sum(d['newly_vetoed'] for d in churn)} vetoed, "
          f"{sum(d['returned'] for d in churn)} returned, in {len(churn)} updates; jobs {dict(__import__('collections').Counter(jobs))}")
    assert kinds["player_playing"] == 1 and kinds["player_stall"] == 1
    from music_hunter.report import report
    report(c)

    # Add saves to collection/ (learning or control); nothing else is kept, apart from the latest
    # rating's held audio (the back button can still change it).
    from music_hunter.ratings import ADD
    added = [p for p in plays if p["rating"] == ADD]
    assert len(list((config.ROOT / "collection").glob("*"))) == len(added) > 0
    held = c.execute("SELECT count(*) FROM plays WHERE undo_path IS NOT NULL").fetchone()[0]
    assert held <= 1
    assert all(p["target_bpm"] == 128.0 for p in plays)
    assert sum(1 for p in plays if p["playback_rate"] == 1.04) > 20

    # Results view has everything to show (v2: no pass mark).
    assert results["reading"] in ("clearly_higher", "clearly_lower", "unclear")
    cr = results["controls_by_rank"]
    assert cr["n"] == sum(1 for p in plays if p["is_control"] and p["rating"] is not None)
    assert sum(b["n"] for b in cr["bins"]) == cr["n"] and len({b["n"] > 0 for b in cr["bins"]}) >= 1
    assert all(len(v) == len(rws) + 1 for v in results["weight_history"].values())
    assert results["tempo"]["stretched_plays"] > 20 and results["tempo"]["setting_changes"] == 2
    assert results["add_difference"]["diff"] is not None
    assert results["ranked"]["n"] and results["control"]["n"]
    groups = results["by_sources"]                       # ratings by number of sources: every rated play counted once
    assert [g["name"] for g in groups] == ["1 source", "2 sources", "3+ sources"]
    assert sum(g["all"]["n"] for g in groups) == results["totals"]["rated"]
    assert all(g["all"]["n"] == g["ranked"]["n"] + g["control"]["n"] for g in groups)
    by_seed = results["by_seed"]                          # ratings by the seed pool each track was scored in
    assert by_seed and sum(g["all"]["n"] for g in by_seed) == results["totals"]["rated"]
    assert 0.10 <= results["control_rate"] <= 0.25
    assert results["rolling"]["ranked"] and results["rolling"]["control"]
    assert all(s["points"] for s in results["source_averages"])
    assert len(results["reweightings"]) == len(rws)
    assert results["totals"]["skipped"] == sum(1 for p in plays if p["skipped_at"])
    print(f"\nranked {results['ranked']['mean']:.2f} vs control {results['control']['mean']:.2f} "
          f"(diff {results['difference']['diff']:+.2f}, {results['reading']}), Add rate {results['ranked']['keep']:.0%} vs "
          f"{results['control']['keep']:.0%}; updates at {expected}; generations {len(gens)}")
    print("controls by rank:", [(b["name"], b["n"], b["mean"] and round(b["mean"], 2)) for b in cr["bins"]],
          "spearman", cr["spearman"] and round(cr["spearman"], 2))
    for r in rws[::4]:
        print(f"  at {r['at_ratings']}: towards {r['target']:8s} fit {r['old_concordance']:.3f} -> {r['new_concordance']:.3f}")
