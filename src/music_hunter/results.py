"""Results view data — SPEC.md §11 (results view) and §12 (success test)."""

import json
import math
import sqlite3

from .config import CONFIG
from .pull import seeds
from .ratings import SOURCES, _averages
from .scoring import CFG as SCORING, current_weights

CFG = CONFIG["results"]
Z = 1.96  # 95% intervals


def summary(ratings: list[int]) -> dict:
    """Average with 95% CI, and keep rate (3–5) with a 95% Wilson interval."""
    n = len(ratings)
    out = {"n": n, "mean": None, "mean_lo": None, "mean_hi": None, "keep": None, "keep_lo": None, "keep_hi": None}
    if n == 0:
        return out
    mean = sum(ratings) / n
    out["mean"] = mean
    if n > 1:
        sd = math.sqrt(sum((r - mean) ** 2 for r in ratings) / (n - 1))
        out["mean_lo"], out["mean_hi"] = mean - Z * sd / math.sqrt(n), mean + Z * sd / math.sqrt(n)
    p = sum(r >= 3 for r in ratings) / n
    centre = (p + Z**2 / (2 * n)) / (1 + Z**2 / n)
    half = Z * math.sqrt(p * (1 - p) / n + Z**2 / (4 * n * n)) / (1 + Z**2 / n)
    out.update(keep=p, keep_lo=max(0.0, centre - half), keep_hi=min(1.0, centre + half))
    return out


def difference(a: list[int], b: list[int]) -> dict:
    """Mean of a minus mean of b, with a 95% interval (Welch standard error)."""
    if len(a) < 2 or len(b) < 2:
        return {"diff": None, "lo": None, "hi": None}
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    return {"diff": ma - mb, "lo": ma - mb - Z * se, "hi": ma - mb + Z * se}


def rolling(points: list[tuple[int, int]]) -> list[dict]:
    """Rolling average over the last `rolling_window` ratings of one group: [{play, value, n}]."""
    w, out = CFG["rolling_window"], []
    for k in range(len(points)):
        window = [r for _, r in points[max(0, k - w + 1):k + 1]]
        out.append({"play": points[k][0], "value": sum(window) / len(window), "n": len(window)})
    return out


def results(conn: sqlite3.Connection) -> dict:
    plays = conn.execute("SELECT * FROM plays ORDER BY play_number").fetchall()
    rated = [p for p in plays if p["rating"] is not None]
    ranked = [(p["play_number"], p["rating"]) for p in rated if not p["is_control"]]
    control = [(p["play_number"], p["rating"]) for p in rated if p["is_control"]]
    r_only, c_only = [r for _, r in ranked], [r for _, r in control]

    diff = difference(r_only, c_only)
    margin = CFG["success_margin"]
    if diff["diff"] is None:
        verdict = "not_enough_data"
    elif diff["diff"] >= margin:
        verdict = "meets"
    else:
        verdict = "below"

    # Seed and source averages as they stood after each learning rating.
    seed_list = seeds(conn)
    labels = {s.track_id: s.label for s in seed_list}
    prior = CONFIG["averages"]["prior"]
    seed_series = {sid: [] for sid in labels}
    source_series = {s: [] for s in SOURCES}
    for p in rated:
        if p["is_control"]:
            continue
        s_avg = _averages(conn, "seed_track_id", upto_play=p["id"], generation=p["generation"])
        src_avg = _averages(conn, "source", upto_play=p["id"], generation=p["generation"])
        for sid in labels:
            seed_series[sid].append({"play": p["play_number"], "value": s_avg.get(sid, prior)})
        for s in SOURCES:
            source_series[s].append({"play": p["play_number"], "value": src_avg.get(s, prior)})

    weight_sets = {r["id"]: json.loads(r["weights"]) for r in conn.execute("SELECT id, weights FROM weight_sets")}
    reweightings = [{
        **{k: r[k] for k in r.keys()},
        "old_weights": weight_sets[r["old_weight_set_id"]], "new_weights": weight_sets[r["new_weight_set_id"]],
    } for r in conn.execute("SELECT * FROM reweightings ORDER BY at_ratings")]

    return {
        "totals": {
            "plays": len(plays), "rated": len(rated), "learning": len(ranked), "control": len(control),
            "skipped": sum(1 for p in plays if p["skipped_at"]),
            "generations": conn.execute("SELECT count(*) FROM generations").fetchone()[0],
            "personal_model_active": len(ranked) >= SCORING["personal_model_min_ratings"],
            "personal_model_at": SCORING["personal_model_min_ratings"],
        },
        "ranked": summary(r_only), "control": summary(c_only),
        "difference": diff, "margin": margin, "verdict": verdict,
        "usual_keep_rate": CFG["usual_keep_rate"], "target_plays": 100,
        "rolling_window": CFG["rolling_window"],
        "rolling": {"ranked": rolling(ranked), "control": rolling(control)},
        "seed_averages": [{"label": labels[sid], "points": pts} for sid, pts in seed_series.items()],
        "source_averages": [{"label": s, "points": pts} for s, pts in source_series.items()],
        "reweightings": reweightings,
        "weights": current_weights(conn)[1],
    }
