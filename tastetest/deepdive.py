"""Deep dive across v1, v2 and v3: which signals predict my ratings? (2026-10-30)

    uv run python deepdive.py        # writes runs/taste-test/deepdive.json and prints a summary

One record per rated play, with everything the runs logged: provenance (seed, source, position in the source's
list), timing, artist history, metadata, MAEST similarity to the seeds / earlier likes / earlier dislikes.
"Liked" is v1 4–5, v2 On target/Add, v3 Add; "rank" is the rating's percentile within its run.
"""

import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime

import numpy as np
from scipy.stats import chi2_contingency, mannwhitneyu, spearmanr

from common import OUT, ROOT, load_dataset, load_features
from music_hunter.names import split_artists

RUNS = {"v1": (ROOT / "runs/v1/data/music_hunter.db", 4, (1, 2)),   # db, liked from, disliked ratings
        "v2": (ROOT / "runs/v2/data/music_hunter.db", 3, (1,)),
        "v3": (ROOT / "data/music_hunter.db", 3, (1,))}


def ts(s):
    return datetime.fromisoformat(s)


def unit(v):
    return v / np.linalg.norm(v)


def build():
    recs = {r["key"]: r for r in load_dataset()}
    z = load_features("maest_gpu")
    maest = {k: unit(z["track"][i][8].astype(np.float64)) for i, k in enumerate(z["keys"])}
    zs = np.load(OUT / "features" / "seeds_maest.npz")
    seed_vec = {k: unit(v.astype(np.float64)) for k, v in zip(zs["keys"], zs["track"])}
    rows = []
    for run, (dbp, liked_from, disliked) in RUNS.items():
        c = sqlite3.connect(dbp)
        c.row_factory = sqlite3.Row
        cols = {x[1] for x in c.execute("PRAGMA table_info(tracks)")}
        tracks = {r["id"]: r for r in c.execute("SELECT * FROM tracks")}
        originals = {tid for tid, t in tracks.items() if t["origin"] == "original_reference"}
        seed_bpm = np.median([tracks[s]["bpm"] for s in originals if "bpm" in cols and tracks[s]["bpm"]] or [np.nan])
        plays = c.execute("SELECT * FROM plays WHERE rating IS NOT NULL ORDER BY rated_at, id").fetchall()
        top = max(p["rating"] for p in plays)
        sugg = defaultdict(list)
        for s in c.execute("SELECT candidate_id, seed_track_id, source, rank FROM suggestions WHERE candidate_id IS NOT NULL"):
            sugg[s["candidate_id"]].append((s["seed_track_id"], s["source"], s["rank"]))
        list_len = defaultdict(int)                     # length of each (seed, source) list, for relative position
        for s in c.execute("SELECT seed_track_id, source, generation, max(rank) FROM suggestions GROUP BY 1, 2, 3"):
            list_len[(s[0], s[1])] = max(list_len[(s[0], s[1])], s[3])
        added_seeds = {p["track_id"] for p in plays if p["rating"] == top and not p["is_control"]}
        prev_shown = None
        session, in_session = 0, 0
        history = []                                    # earlier plays in this run: (artists, liked, disliked, vec)
        for i, p in enumerate(plays):
            t = tracks[p["track_id"]]
            key = f"{run}:{p['id']}"
            shown = ts(p["shown_at"])
            if prev_shown is None or (shown - prev_shown).total_seconds() > 1800:
                session += 1
                in_session = 0
            in_session += 1
            prev_shown = shown
            prov = sugg.get(t["candidate_id"], [])
            arts = split_artists(t["artist"])
            seed_ids = {s for s, _, _ in prov}
            seed_arts = [split_artists(tracks[s]["artist"]) for s in seed_ids if s in tracks]
            all_seed_arts = set().union(*(split_artists(tracks[s]["artist"]) for s in originals))
            v = maest.get(key)
            run_seeds = [seed_vec[f"{run}:seed:{s}"] for s in originals if f"{run}:seed:{s}" in seed_vec]
            likes = [h[3] for h in history if h[1] and h[3] is not None]
            dislikes = [h[3] for h in history if h[2] and h[3] is not None]

            def topk(vs, k=3):
                if v is None or not vs:
                    return None
                s = np.sort(np.array(vs) @ v)[::-1]
                return float(s[:min(k, len(s))].mean())

            prev_art = [h for h in history if h[0] & arts]
            year = (t["release_date"] or "")[:4] if "release_date" in cols else ""
            comps = json.loads(p["components"] or "{}")
            rows.append({
                "key": key, "run": run, "order": i, "position": i / max(1, len(plays) - 1),
                "rating": p["rating"], "rank": recs[key]["rank"] if key in recs else None,
                "liked": int(p["rating"] >= liked_from), "disliked": int(p["rating"] in disliked),
                "control": int(p["is_control"]), "session": session, "in_session": in_session,
                "artist": t["artist"], "title": t["title"],
                "sources": sorted({s for _, s, _ in prov}), "n_sources": len({s for _, s, _ in prov}),
                "n_seeds": len(seed_ids), "seed_ids": sorted(seed_ids),
                "seed_labels": sorted(f"{tracks[x]['artist']} – {tracks[x]['title']}" for x in seed_ids if x in tracks), "n_pairs": len({(s, src) for s, src, _ in prov}),
                "best_rank": min((r for _, _, r in prov), default=None),
                "best_rel_rank": min((r / max(1, list_len[(s, src)]) for s, src, r in prov), default=None),
                "rank_by_source": {src: min(r for _, s2, r in prov if s2 == src) for src in {s for _, s, _ in prov}},
                "from_added_seed": int(any(s in added_seeds or (s not in originals) for s in seed_ids)),
                "own_artist": int(any(a & arts for a in seed_arts)),
                "seed_artist": int(bool(all_seed_arts & arts)),
                "artist_seen_before": len(prev_art),
                "artist_liked_before": sum(h[1] for h in prev_art),
                "artist_disliked_before": sum(h[2] for h in prev_art),
                "duration": t["duration_s"],
                "year": int(year) if year.isdigit() else None,
                "bpm": t["bpm"] if "bpm" in cols else None,
                "bpm_dist": abs(t["bpm"] - seed_bpm) if "bpm" in cols and t["bpm"] and not math.isnan(seed_bpm) else None,
                "seed_sim": topk(run_seeds, 1), "seed_sim3": topk(run_seeds, 3),
                "like_sim": topk(likes), "dislike_sim": topk(dislikes),
                "logged_score": p["score"], "logged_rank_pos": (p["rank"] - 1) / max(1, p["pool_size"] - 1)
                if p["rank"] else None,
                "logged": {k: v2["norm"] for k, v2 in comps.items()},
            })
            history.append((arts, p["rating"] >= liked_from, p["rating"] in disliked, v))
    return rows


def wilson(k, n):
    if n == 0:
        return (None, None, None)
    p, z = k / n, 1.96
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (p, max(0, centre - half), min(1, centre + half))


def rate_table(rows, keyf, label):
    groups = defaultdict(list)
    for r in rows:
        k = keyf(r)
        if k is not None:
            groups[k].append(r)
    out = []
    for k in sorted(groups, key=str):
        g = groups[k]
        p, lo, hi = wilson(sum(r["liked"] for r in g), len(g))
        out.append({"group": str(k), "n": len(g), "liked": p, "lo": lo, "hi": hi,
                    "rank": float(np.mean([r["rank"] for r in g]))})
    table = [[sum(r["liked"] for r in groups[k]), len(groups[k]) - sum(r["liked"] for r in groups[k])] for k in groups]
    pval = chi2_contingency(table)[1] if len(table) > 1 and all(sum(x) for x in zip(*table)) else None
    return {"label": label, "groups": out, "chi2_p": pval}


def corr(rows, f, label, target="rank"):
    pts = [(f(r), r[target]) for r in rows if f(r) is not None and r[target] is not None]
    if len(pts) < 8 or len({x for x, _ in pts}) < 2:
        return {"label": label, "n": len(pts), "rho": None, "p": None}
    rho, p = spearmanr([x for x, _ in pts], [y for _, y in pts])
    return {"label": label, "n": len(pts), "rho": float(rho), "p": float(p)}


def main():
    rows = build()
    (OUT / "deepdive_rows.json").write_text(json.dumps(rows, indent=1, default=str))
    by_run = {run: [r for r in rows if r["run"] == run] for run in RUNS}
    report = {}

    # 1. Timing: does liking fall over the run, and is that the pool or me? Random controls (v2, v3) can't be
    #    affected by the pool draining from the top, so a fall in them means my standards drifted.
    t = {}
    for run, rs in by_run.items():
        q = [rate_table([r for r in rs if not r["control"]], lambda r: min(3, int(r["position"] * 4)), "ranked by quarter"),
             corr([r for r in rs if not r["control"]], lambda r: r["position"], "ranked: position vs rank"),
             corr([r for r in rs if r["control"]], lambda r: r["position"], "controls: position vs rank"),
             corr(rs, lambda r: r["in_session"], "all: position within session vs rank")]
        t[run] = q
    rand_controls = [r for r in rows if r["control"] and r["run"] in ("v2", "v3")]
    t["random controls v2+v3"] = [corr(rand_controls, lambda r: r["position"], "position vs rank"),
                                  rate_table(rand_controls, lambda r: "first half" if r["position"] < 0.5 else "second half",
                                             "liked by half")]
    report["timing"] = t

    # 2. Sources: credit to every source that suggested it, and single-source tracks only (clean attribution)
    s = {}
    for run, rs in by_run.items():
        srcs = sorted({x for r in rs for x in r["sources"]})
        s[run] = {"any": [dict(source=x, **rate_table([r for r in rs if x in r["sources"]], lambda r: "all", x)["groups"][0])
                          for x in srcs],
                  "only": rate_table([r for r in rs if r["n_sources"] == 1], lambda r: r["sources"][0], "single-source")}
    report["sources"] = s

    # 3. Position in the source's own list (never used for ranking so far). Controls = unaffected by the ranking.
    p = {}
    for run, rs in by_run.items():
        p[run] = [corr(rs, lambda r: r["best_rank"], "all: best list position vs rank"),
                  corr(rs, lambda r: r["best_rel_rank"], "all: best relative position vs rank"),
                  corr([r for r in rs if r["control"]], lambda r: r["best_rank"], "controls: best position vs rank"),
                  rate_table(rs, lambda r: None if r["best_rank"] is None else
                             ("1-10" if r["best_rank"] <= 10 else "11-30" if r["best_rank"] <= 30 else "31+"), "liked by position")]
        for src in ("ytmusic", "lastfm", "deezer", "tidal", "ytrelated"):
            sub = [r for r in rs if src in r["rank_by_source"]]
            if len(sub) >= 10:
                p[run].append(corr(sub, lambda r, src=src: r["rank_by_source"][src], f"{src}: position vs rank"))
    report["position"] = p

    # 4. Agreement
    a = {}
    for run, rs in by_run.items():
        a[run] = [rate_table(rs, lambda r: min(r["n_sources"], 3), "liked by number of sources (3 = 3+)"),
                  corr(rs, lambda r: r["n_sources"], "n_sources vs rank"),
                  corr(rs, lambda r: r["n_seeds"], "n_seeds vs rank"),
                  corr([r for r in rs if r["control"]], lambda r: r["n_sources"], "controls: n_sources vs rank")]
    report["agreement"] = a

    # 5. Seeds: do some seeds give much better tracks? (credit to every suggesting seed; heterogeneity test)
    sd = {}
    for run, rs in by_run.items():
        exploded = [dict(r, seed=lab) for r in rs for lab in r["seed_labels"]]
        sd[run + " per seed"] = rate_table(exploded, lambda r: r["seed"], "liked by suggesting seed (credit to each)")
        sd[run] = rate_table(rs, lambda r: "added-track seed" if r["from_added_seed"] else "original seeds only",
                             "from a seed that was an Added track")
    report["seed_origin"] = sd

    # 6. Artist history and seed artists
    ar = {}
    for run, rs in by_run.items():
        ar[run] = [rate_table(rs, lambda r: ("liked before" if r["artist_liked_before"] else
                                             "disliked before" if r["artist_disliked_before"] else
                                             "seen, neutral" if r["artist_seen_before"] else "new artist"),
                              "artist history in this run"),
                   rate_table(rs, lambda r: "seed's own artist" if r["own_artist"] else "other artist", "own-artist suggestion"),
                   rate_table(rs, lambda r: "a seed artist" if r["seed_artist"] else "not a seed artist", "track by any seed artist")]
    report["artist"] = ar

    # 7. Sound (MAEST) and metadata
    snd = {}
    for run, rs in by_run.items():
        snd[run] = [corr(rs, lambda r: r["seed_sim"], "closest seed (MAEST)"),
                    corr(rs, lambda r: r["seed_sim3"], "3 closest seeds"),
                    corr(rs, lambda r: r["like_sim"], "earlier likes (top-3)"),
                    corr(rs, lambda r: r["dislike_sim"], "earlier dislikes (top-3)"),
                    corr(rs, lambda r: None if r["like_sim"] is None or r["dislike_sim"] is None
                         else r["like_sim"] - r["dislike_sim"], "likes minus dislikes"),
                    corr([r for r in rs if r["control"]], lambda r: r["seed_sim"], "controls: closest seed"),
                    corr(rs, lambda r: r["duration"], "duration"),
                    corr(rs, lambda r: r["year"], "release year"),
                    corr(rs, lambda r: r["bpm_dist"], "BPM distance from seeds' median")]
    report["sound_meta"] = snd

    # 8. What the player's own score did
    own = {}
    for run, rs in by_run.items():
        ranked = [r for r in rs if not r["control"]]
        ctl = [r for r in rs if r["control"]]
        own[run] = [corr(rs, lambda r: r["logged_score"], "logged score vs rank"),
                    corr(ctl, lambda r: -r["logged_rank_pos"] if r["logged_rank_pos"] is not None else None,
                         "controls: higher in ranking vs rank"),
                    {"ranked_liked": wilson(sum(r["liked"] for r in ranked), len(ranked)),
                     "control_liked": wilson(sum(r["liked"] for r in ctl), len(ctl)),
                     "ranked_rank": float(np.mean([r["rank"] for r in ranked])),
                     "control_rank": float(np.mean([r["rank"] for r in ctl])) if ctl else None}]
    report["own_score"] = own

    (OUT / "deepdive.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(report, indent=1, default=lambda o: round(o, 3) if isinstance(o, float) else str(o))[:100])
    print(f"{len(rows)} rows; report in runs/taste-test/deepdive.json")


if __name__ == "__main__":
    main()
