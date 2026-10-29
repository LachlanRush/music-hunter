"""Re-weighting v2 — SPEC.md §15.8.

When: after [reweighting] first_at learning ratings, then every `every`.

Objective (top-weighted, whole history): over pairs of rated plays with different ratings,
the weighted share where the higher-rated play has the higher score (ties count half).
A pair's weight is the product of the two plays' recency weights (half-life in ratings)
times the gap in "gain" (2^rating), so getting Add vs Off right matters far more than
Close vs Off: mistakes near the top count most. Every rated play counts, controls
included — they're the only tracks rated without being chosen by the weights.

Each update (continuous re-evaluation):
1. search for the best weights on the history (sign-constrained, within the guardrails);
2. re-score the current set, that optimum, and every earlier weight set on the same history;
3. for each, take the step towards it allowed by the ±step limit, and adopt the step that
   scores best (staying put is one option, so an update never makes the history fit worse).
No set stays a target because of a few lucky picks: every set is re-scored as ratings
accumulate. Live results of each set's own picks are in the plays log, not used here.

Evaluation (logged only): once eval_window more learning ratings exist, the weights before
and after each update are compared on those ratings.
"""

import json
import sqlite3
import time

import numpy as np

from . import db
from .config import CONFIG
from .ratings import learning_ratings
from .scoring import CFG as SCORING
from .scoring import COMPONENTS, current_weights

CFG = CONFIG["reweighting"]
SIGNS = np.array([np.sign(SCORING["weights"][c]) for c in COMPONENTS])
FLOORS = np.array([CFG["min_weight"].get(c, 0.0) for c in COMPONENTS])           # |w| at least
CAPS = np.array([CFG["max_weight"].get(c, np.inf) for c in COMPONENTS])          # |w| at most


def to_vec(weights: dict) -> np.ndarray:
    return np.array([{**SCORING["weights"], **weights}[c] for c in COMPONENTS], dtype=float)


def to_dict(vec: np.ndarray) -> dict:
    return {c: round(float(v), 6) for c, v in zip(COMPONENTS, vec)}


def clamp(W: np.ndarray) -> np.ndarray:
    """Guardrails: signs fixed, magnitudes within [floor, cap] per component."""
    return SIGNS * np.clip(np.abs(W) * (np.sign(W) == SIGNS), FLOORS, CAPS)


def history(conn: sqlite3.Connection, plays: list | None = None):
    """(play ids, logged normalised components X, ratings) for rated plays (learning and control), in rating order."""
    rows = plays if plays is not None else conn.execute(
        "SELECT id, components, rating FROM plays WHERE rating IS NOT NULL ORDER BY rated_at, id").fetchall()
    X = np.array([[json.loads(r["components"]).get(c, {"norm": 0.0})["norm"] for c in COMPONENTS]
                  for r in rows]).reshape(-1, len(COMPONENTS))
    return [r["id"] for r in rows], X, np.array([r["rating"] for r in rows], dtype=float)


def recency(n: int) -> np.ndarray:
    return 0.5 ** ((n - 1 - np.arange(n)) / CFG["recency_half_life"])


def pairs(ratings: np.ndarray, weights: np.ndarray, top_weighted: bool = True):
    """(higher-rated, lower-rated) index arrays and pair weights over pairs with different ratings."""
    i, j = np.triu_indices(len(ratings), 1)
    keep = ratings[i] != ratings[j]
    i, j = i[keep], j[keep]
    hi = np.where(ratings[i] > ratings[j], i, j)
    lo = np.where(ratings[i] > ratings[j], j, i)
    pw = weights[i] * weights[j]
    if top_weighted:
        pw = pw * (2.0 ** ratings[hi] - 2.0 ** ratings[lo])
    return hi, lo, pw


def objective(W: np.ndarray, X: np.ndarray, hi, lo, pw) -> np.ndarray:
    """Weighted share of correctly ordered pairs, for each row of W."""
    W = np.atleast_2d(W)
    out = np.empty(len(W))
    for start in range(0, len(W), 256):
        S = W[start:start + 256] @ X.T
        d = S[:, hi] - S[:, lo]
        out[start:start + 256] = ((d > 1e-12) + 0.5 * (np.abs(d) <= 1e-12)) @ pw / pw.sum()
    return out


def search(X, hi, lo, pw, start: np.ndarray, rng) -> np.ndarray:
    """Best weights found on the history: random sign-constrained sets, then local refinement."""
    active = X.std(0) > 1e-9                      # constant components carry no information
    scale = np.abs(start[active]).sum() or 1.0
    k = int(active.sum())
    if k == 0:
        return start

    def to_weights(mags: np.ndarray) -> np.ndarray:
        W = np.tile(start, (len(mags), 1))
        W[:, active] = mags / mags.sum(1, keepdims=True) * scale * SIGNS[active]
        return clamp(W)

    cands = np.vstack([start[None], to_weights(rng.dirichlet(np.ones(k), CFG["random_samples"]))])
    scores = objective(cands, X, hi, lo, pw)
    best, best_score = cands[scores.argmax()], scores.max()
    for _ in range(CFG["refine_rounds"]):
        mags = np.abs(best[active]) + 1e-6
        trial = to_weights(mags * np.exp(rng.normal(0, 0.35, (CFG["refine_batch"], k))))
        s = objective(trial, X, hi, lo, pw)
        if s.max() > best_score:
            best, best_score = trial[s.argmax()], s.max()
    return best


def step_towards(current: np.ndarray, target: np.ndarray) -> np.ndarray:
    return clamp(current + np.clip(target - current, -CFG["step"], CFG["step"]))


def due(conn: sqlite3.Connection) -> bool:
    n = learning_ratings(conn)
    on_schedule = n >= CFG["first_at"] and (n - CFG["first_at"]) % CFG["every"] == 0
    done = conn.execute("SELECT 1 FROM reweightings WHERE at_ratings = ?", (n,)).fetchone()
    return on_schedule and not done


def score_sets(conn: sqlite3.Connection) -> dict[int, float]:
    """Every weight set ever used, scored on the whole history as it stands now."""
    _, X, r = history(conn)
    hi, lo, pw = pairs(r, recency(len(r)))
    if len(hi) == 0:
        return {}
    sets = [(row["id"], to_vec(json.loads(row["weights"])))
            for row in conn.execute("SELECT id, weights FROM weight_sets")]
    scores = objective(np.stack([v for _, v in sets]), X, hi, lo, pw)
    return {sid: float(s) for (sid, _), s in zip(sets, scores)}


def reweight(conn: sqlite3.Connection) -> dict:
    """One update: the best move within the step limit towards {search optimum, earlier sets}, or stay."""
    t0 = time.monotonic()
    _, X, r = history(conn)
    hi, lo, pw = pairs(r, recency(len(r)))
    cur_id, cur_w = current_weights(conn)
    current = clamp(to_vec(cur_w))
    n = learning_ratings(conn)

    targets: dict[str, np.ndarray] = {"current": current}
    if len(hi):
        targets["search"] = search(X, hi, lo, pw, current, np.random.default_rng(n))
        for row in conn.execute("SELECT id, weights FROM weight_sets WHERE id != ?", (cur_id,)):
            targets[f"set {row['id']}"] = clamp(to_vec(json.loads(row["weights"])))
    names = list(targets)
    moves = {k: step_towards(current, v) for k, v in targets.items()}
    if len(hi):
        full = dict(zip(names, objective(np.stack([targets[k] for k in names]), X, hi, lo, pw)))
        stepped = dict(zip(names, objective(np.stack([moves[k] for k in names]), X, hi, lo, pw)))
        # Best allowed move; on ties prefer staying put, then the search, then earlier sets.
        chosen = max(names, key=lambda k: (round(stepped[k], 12), k == "current", k == "search"))
        best_target = max(full, key=full.get)
    else:
        full, stepped, chosen, best_target = {}, {}, "current", "current"
    new = moves[chosen]

    with conn:
        new_id = conn.execute("INSERT INTO weight_sets (reason, weights) VALUES ('re-weighting', ?)",
                              (json.dumps(to_dict(new)),)).lastrowid
        conn.execute(
            "INSERT INTO reweightings (at_ratings, old_weight_set_id, new_weight_set_id, pairs,"
            " old_concordance, new_concordance, target, candidates) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (n, cur_id, new_id, int(len(hi)), stepped.get("current"), stepped.get(chosen), chosen,
             json.dumps({k: {"full": round(float(full[k]), 4), "step": round(float(stepped[k]), 4)}
                         for k in full})))
    db.log_event(conn, "reweighted", at_ratings=n, pairs=int(len(hi)), seconds=round(time.monotonic() - t0, 3),
                 moved_towards=chosen, best_full_target=best_target,
                 old_concordance=stepped.get("current"), new_concordance=stepped.get(chosen),
                 largest_change=round(float(np.abs(new - current).max()), 4))
    return {"at_ratings": n, "old": to_dict(current), "new": to_dict(new), "moved_towards": chosen,
            "best_full_target": best_target, "objective": stepped}


def evaluate_pending(conn: sqlite3.Connection) -> None:
    """Weights before vs after each update, on the eval_window learning ratings that followed (logged only)."""
    window = CFG["eval_window"]
    rated = conn.execute("SELECT id, components, rating FROM plays"
                         " WHERE is_control = 0 AND rating IS NOT NULL ORDER BY rated_at, id").fetchall()
    for rw in conn.execute("SELECT * FROM reweightings WHERE eval_n IS NULL").fetchall():
        nxt = rated[rw["at_ratings"]:rw["at_ratings"] + window]
        if len(nxt) < window:
            continue
        _, X, r = history(conn, nxt)
        hi, lo, pw = pairs(r, np.ones(len(r)))
        W = np.stack([to_vec(json.loads(conn.execute("SELECT weights FROM weight_sets WHERE id = ?",
                                                     (wid,)).fetchone()[0]))
                      for wid in (rw["old_weight_set_id"], rw["new_weight_set_id"])])
        old_c, new_c = objective(W, X, hi, lo, pw) if len(hi) else (None, None)
        with conn:
            conn.execute("UPDATE reweightings SET eval_n = ?, eval_pairs = ?, eval_old_concordance = ?,"
                         " eval_new_concordance = ?, evaluated_at = datetime('now') WHERE id = ?",
                         (len(nxt), int(len(hi)), None if old_c is None else float(old_c),
                          None if new_c is None else float(new_c), rw["id"]))


def after_rating(conn: sqlite3.Connection) -> dict | None:
    """Run after every new rating: evaluate earlier updates, then update if due."""
    evaluate_pending(conn)
    return reweight(conn) if due(conn) else None
