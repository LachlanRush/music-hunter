"""Results across every playlist's Find music (SPEC.md §18.25): what the score says about ratings, sources, seeds,
tags, tempo and each playlist's sound. Only totals, rates and shapes — nothing that names a track I've rated, so it
can't tell me afterwards which play was which (seeds are my own starred tracks, so they're named)."""

import json
import sqlite3
from collections import Counter, defaultdict

import numpy as np

from . import db, find, library
from .config import CONFIG

RATINGS = {1: "off", 2: "close", 3: "add"}
SOURCE_NAMES = {"lastfm": "Last.fm", "ytmusic": "YouTube radio", "ytrelated": "YouTube related",
                "deezer": "Deezer", "tidal": "TIDAL"}
BANDS = [(-99, -1.25, "below −1.25 (blocked)"), (-1.25, 0, "−1.25 to 0"), (0, 0.5, "0 to 0.5"),
         (0.5, 1, "0.5 to 1"), (1, 1.5, "1 to 1.5"), (1.5, 99, "1.5 and up")]


def _split(ratings) -> dict:
    c = Counter(ratings)
    return {"n": len(ratings), **{name: c.get(r, 0) for r, name in RATINGS.items()}}


def _plays(sess) -> list[dict]:
    rows = sess.execute("SELECT p.id, p.track_id, p.rating, p.is_control, p.veto, p.tags, p.rated_at, t.candidate_id"
                        " FROM plays p JOIN tracks t ON t.id = p.track_id WHERE p.rating IS NOT NULL"
                        " ORDER BY p.rated_at").fetchall()
    sources = defaultdict(set)
    seeds = defaultdict(set)
    for cid, src, seed in sess.execute("SELECT candidate_id, source, seed_track_id FROM suggestions"
                                       " WHERE candidate_id IS NOT NULL"):
        sources[cid].add(src)
        seeds[cid].add(seed)
    out = []
    for r in rows:
        v = json.loads(r["veto"]) if r["veto"] else {}
        out.append({"rating": r["rating"], "control": bool(r["is_control"]), "z": v.get("z"),
                    "tempo_out": (v.get("tempo") or {}).get("outside_bpm"), "tags": json.loads(r["tags"] or "[]"),
                    "at": r["rated_at"], "sources": sorted(sources.get(r["candidate_id"], ())),
                    "seeds": sorted(seeds.get(r["candidate_id"], ()))})
    return out


def _sound_map(sess, max_points: int = 600) -> dict:
    """Each playlist's pool and seeds on a 2-D sound map (the two main directions of their MAEST fingerprints)."""
    rated = {r[0]: r[1] for r in sess.execute("SELECT track_id, rating FROM plays WHERE rating IS NOT NULL")}
    rows = sess.execute("SELECT t.id, t.origin, t.stars, t.artist, t.title, coalesce(t.veto_state, 'playable') st,"
                        " e.embedding FROM tracks t JOIN track_embeddings e ON e.track_id = t.id"
                        " WHERE t.origin = 'pool' OR t.stars IS NOT NULL").fetchall()
    if len(rows) < 3:
        return {"points": [], "seeds": []}
    X = np.stack([db.from_blob(r["embedding"]) for r in rows]).astype(float)
    Xc = X - X.mean(0)
    _, s, vt = np.linalg.svd(Xc, full_matrices=False)
    xy = Xc @ vt[:2].T
    xy = (xy - xy.min(0)) / np.where(np.ptp(xy, 0) > 0, np.ptp(xy, 0), 1)
    points, seeds = [], []
    rng = np.random.default_rng(0)
    pool_idx = [i for i, r in enumerate(rows) if r["origin"] == "pool"]
    unrated = [i for i in pool_idx if rows[i]["id"] not in rated]
    keep = set(rng.choice(unrated, size=min(len(unrated), max_points), replace=False).tolist()) if unrated else set()
    for i, r in enumerate(rows):
        x, y = round(float(xy[i, 0]), 4), round(float(xy[i, 1]), 4)
        if r["stars"] is not None:                       # my starred tracks (named: they're mine)
            seeds.append({"x": x, "y": y, "stars": r["stars"], "name": f"{r['artist']} – {r['title']}"})
        elif r["id"] in rated:
            points.append({"x": x, "y": y, "k": RATINGS.get(rated[r["id"]], "close")})
        elif i in keep:
            points.append({"x": x, "y": y, "k": "playable" if r["st"] == "playable" else "blocked"})
    share = float((s[:2] ** 2).sum() / (s ** 2).sum()) if s.size else 0.0
    return {"points": points, "seeds": seeds, "explained": round(share, 3)}


def overview() -> dict:
    lib = db.connect_library()
    playlists, allplays = [], []
    for p in library.list_playlists(lib):
        if not find.started(p["id"]):
            continue
        sess = db.connect(p["id"])
        plays = _plays(sess)
        for x in plays:
            x["playlist"] = p["name"]
        allplays += plays
        names = {r[0]: f"{r[1]} – {r[2]}" for r in sess.execute("SELECT id, artist, title FROM tracks")}
        stars = {r[0]: r[1] for r in sess.execute("SELECT id, stars FROM tracks WHERE stars IS NOT NULL")}
        by_seed = defaultdict(list)
        for x in plays:
            if not x["control"]:
                for s in x["seeds"]:
                    by_seed[s].append(x["rating"])
        seeds = [{"name": names.get(s, "?"), "stars": stars.get(s), **_split(rs)} for s, rs in by_seed.items()]
        seeds = sorted([s for s in seeds if s["n"] >= 3], key=lambda s: (-(s["add"] / s["n"]), s["off"] / s["n"]))
        playlists.append({"name": p["name"], **_split([x["rating"] for x in plays]),
                          "best_seeds": seeds[:5], "map": _sound_map(sess)})
    picks = [x for x in allplays if not x["control"]]

    curve = []
    for lo, hi, label in BANDS:
        rs = [x["rating"] for x in allplays if x["z"] is not None and lo <= x["z"] < hi]
        curve.append({"band": label, **_split(rs)})

    window = 25
    seq = sorted(picks, key=lambda x: x["at"] or "")
    trend = []
    for i in range(window, len(seq) + 1):
        w = [x["rating"] for x in seq[i - window:i]]
        trend.append({"n": i, "at": seq[i - 1]["at"], "off": round(100 * w.count(1) / window, 1),
                      "add": round(100 * w.count(3) / window, 1)})

    src = []
    for s, label in SOURCE_NAMES.items():
        rs = [x["rating"] for x in picks if s in x["sources"]]
        if rs:
            src.append({"source": label, **_split(rs)})
    n_src = []
    for k, label in ((1, "1 source"), (2, "2 sources"), (3, "3 or more")):
        rs = [x["rating"] for x in picks if (len(x["sources"]) >= 3 if k == 3 else len(x["sources"]) == k)]
        n_src.append({"band": label, **_split(rs)})

    tags = {name: Counter() for name in RATINGS.values()}
    for x in allplays:
        for t in x["tags"]:
            tags[RATINGS[x["rating"]]][t] += 1
    tempo = []
    for lo, hi, label in ((0, 0.001, "inside the range"), (0.001, 3, "up to 3 BPM outside"),
                          (3, 6, "3–6 BPM outside"), (6, 999, "over 6 BPM (blocked)")):
        rs = [x["rating"] for x in allplays if x["tempo_out"] is not None and lo <= x["tempo_out"] < hi]
        tempo.append({"band": label, **_split(rs)})

    return {"totals": {**_split([x["rating"] for x in picks]), "playlists": len(playlists)},
            "curve": curve, "trend": trend, "window": window, "sources": src, "n_sources": n_src,
            "tags": {k: v.most_common() for k, v in tags.items()}, "tempo": tempo, "playlists": playlists}
