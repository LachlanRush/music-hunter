"""Results view data — SPEC.md §11 and §15.10 (v2: no pass criterion; judged from the data)."""

import json
import math
import sqlite3

import numpy as np

from .config import CONFIG
from .pull import seeds
from .ratings import ADD, LABELS, SOURCES, _averages
from . import db
from .scoring import CFG as SCORING, COMPONENTS, current_weights, personal_model_active, tempo_settings

CFG = CONFIG["results"]
Z = 1.96  # 95% intervals


def summary(ratings: list[int]) -> dict:
    """Average with 95% CI, and Add rate (v2; was keep rate 3–5 in v1) with a 95% Wilson interval."""
    n = len(ratings)
    out = {"n": n, "mean": None, "mean_lo": None, "mean_hi": None, "keep": None, "keep_lo": None, "keep_hi": None}
    if n == 0:
        return out
    mean = sum(ratings) / n
    out["mean"] = mean
    if n > 1:
        sd = math.sqrt(sum((r - mean) ** 2 for r in ratings) / (n - 1))
        out["mean_lo"], out["mean_hi"] = mean - Z * sd / math.sqrt(n), mean + Z * sd / math.sqrt(n)
    p = sum(r == ADD for r in ratings) / n
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


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3 or len(set(a)) < 2 or len(set(b)) < 2:
        return None
    rank = lambda v: np.argsort(np.argsort(v))
    return float(np.corrcoef(rank(np.array(a)), rank(np.array(b)))[0, 1])


def controls_by_rank(controls: list) -> dict:
    """Random controls land at every rank: does a higher score (better rank) mean a better rating?

    Rank is scaled to 0 (top of the pool) – 1 (bottom); bins are quarters of the pool.
    """
    pts = [((p["rank"] - 1) / max(1, p["pool_size"] - 1), p["rating"]) for p in controls]
    bins = []
    for i, name in enumerate(["Top quarter", "Second quarter", "Third quarter", "Bottom quarter"]):
        r = [rating for pos, rating in pts if i / 4 <= pos < (i + 1) / 4 or (i == 3 and pos == 1)]
        bins.append({"name": name, "n": len(r), "mean": sum(r) / len(r) if r else None})
    rho = spearman([pos for pos, _ in pts], [r for _, r in pts])
    return {"n": len(pts), "bins": bins, "spearman": rho,
            "points": [{"position": round(pos, 3), "rating": r} for pos, r in pts]}


def by_sources(rated: list) -> list[dict]:
    """Ratings by how many sources suggested the track (as logged when shown): 1, 2, 3+.

    Shows whether tracks only one source knew (often obscure ones) did worse or better; controls are the
    fairest comparison, since the ranking didn't choose them.
    """
    groups = [("1 source", 1, 1), ("2 sources", 2, 2), ("3+ sources", 3, 99)]
    out = []
    for name, lo, hi in groups:
        def pick(control=None):
            return [p["rating"] for p in rated
                    if lo <= len({src for _, src in json.loads(p["provenance"] or "[]")}) <= hi
                    and (control is None or bool(p["is_control"]) == control)]
        out.append({"name": name, "all": summary(pick()), "ranked": summary(pick(False)), "control": summary(pick(True))})
    return out


def reading(diff: dict) -> str:
    """Plain reading of the ranked − control gap from its 95% range (not a pass/fail criterion)."""
    if diff["diff"] is None:
        return "not_enough_data"
    if diff["lo"] > 0:
        return "clearly_higher"
    if diff["hi"] < 0:
        return "clearly_lower"
    return "unclear"


def results(conn: sqlite3.Connection) -> dict:
    plays = conn.execute("SELECT * FROM plays ORDER BY play_number").fetchall()
    rated = [p for p in plays if p["rating"] is not None]
    ranked = [(p["play_number"], p["rating"]) for p in rated if not p["is_control"]]
    control = [(p["play_number"], p["rating"]) for p in rated if p["is_control"]]
    r_only, c_only = [r for _, r in ranked], [r for _, r in control]

    diff = difference(r_only, c_only)
    add_ranked = [1 if r == ADD else 0 for r in r_only]
    add_control = [1 if r == ADD else 0 for r in c_only]

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

    defaults = SCORING["weights"]
    weight_sets = {r["id"]: {**defaults, **json.loads(r["weights"])}
                   for r in conn.execute("SELECT id, weights FROM weight_sets")}
    reweightings = [{
        **{k: r[k] for k in r.keys() if k != "candidates"},
        "candidates": json.loads(r["candidates"]) if r["candidates"] else None,
        "old_weights": weight_sets[r["old_weight_set_id"]], "new_weights": weight_sets[r["new_weight_set_id"]],
    } for r in conn.execute("SELECT * FROM reweightings ORDER BY at_ratings")]
    # Each component's weight over time: start, then after each update.
    first = weight_sets[reweightings[0]["old_weight_set_id"]] if reweightings else current_weights(conn)[1]
    weight_history = {c: [{"at": 0, "value": first[c]}] + [{"at": r["at_ratings"], "value": r["new_weights"][c]}
                                                          for r in reweightings] for c in COMPONENTS}

    controls = [p for p in rated if p["is_control"]]
    stretched = [p for p in rated if p["playback_rate"] and abs(p["playback_rate"] - 1) > 0.005]

    return {
        "totals": {
            "plays": len(plays), "rated": len(rated), "learning": len(ranked), "control": len(control),
            "skipped": sum(1 for p in plays if p["skipped_at"]),
            "generations": conn.execute("SELECT count(*) FROM generations").fetchone()[0],
            "personal_model_active": personal_model_active(conn),
            "personal_model_at": SCORING["personal_model_min_ratings"],
        },
        "ranked": summary(r_only), "control": summary(c_only),
        "difference": diff, "reading": reading(diff), "add_difference": difference(add_ranked, add_control),
        "controls_by_rank": controls_by_rank(controls),
        "by_sources": by_sources(rated),
        "weight_history": weight_history,
        "tempo": {**tempo_settings(conn),
                  "stretched_plays": len(stretched),
                  "setting_changes": conn.execute("SELECT count(*) FROM events WHERE kind = 'setting_changed'").fetchone()[0]},
        "usual_keep_rate": CFG["usual_keep_rate"], "target_plays": 100,
        "scale_max": len(LABELS), "labels": LABELS, "prior": CONFIG["averages"]["prior"],
        "rolling_window": CFG["rolling_window"],
        "rolling": {"ranked": rolling(ranked), "control": rolling(control)},
        "seed_averages": [{"label": labels[sid], "points": pts} for sid, pts in seed_series.items()],
        "source_averages": [{"label": s, "points": pts} for s, pts in source_series.items()],
        "reweightings": reweightings,
        "weights": current_weights(conn)[1],
    }
