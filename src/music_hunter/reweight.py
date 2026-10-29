"""Re-weighting — SPEC.md §9.

Runs at [reweighting] first_at learning ratings, then every `every`. It uses each
learning-rated play's normalised components as logged when the track was shown, and
maximises weighted concordance: over pairs of plays with different ratings, the share
where the higher-rated one has the higher score (score ties count half). A pair's
weight is the product of its two plays' generation decay weights, so recent ratings
count more.

Search: sign-constrained random weight vectors, then rounds of multiplicative
perturbation around the best. Weights keep the scale (sum of absolute values) of the
current set. Components that were the same for every logged play (e.g. the personal
model before it switches on) carry no information and keep their current weight.
The best set found is always adopted; the current weights are among the candidates.

Evaluation (logged only): once eval_window more learning ratings exist, old and new
weights are compared by plain concordance on those ratings.
"""

import json
import sqlite3
import time

import numpy as np

from . import db
from .config import CONFIG
from .ratings import current_generation, learning_ratings
from .scoring import COMPONENTS, current_weights

CFG = CONFIG["reweighting"]
SIGNS = np.array([np.sign(CONFIG["scoring"]["weights"][c]) for c in COMPONENTS])


def learning_data(conn: sqlite3.Connection, plays: list | None = None):
    """(play ids, logged components X, ratings, generations) for rated learning plays, in rating order."""
    rows = plays if plays is not None else conn.execute(
        "SELECT id, components, rating, generation FROM plays WHERE is_control = 0 AND rating IS NOT NULL"
        " ORDER BY rated_at, id").fetchall()
    X = np.array([[json.loads(r["components"])[c]["norm"] for c in COMPONENTS] for r in rows]).reshape(-1, len(COMPONENTS))
    return ([r["id"] for r in rows], X, np.array([r["rating"] for r in rows], dtype=float),
            np.array([r["generation"] for r in rows], dtype=float))


def pairs(ratings: np.ndarray, weights: np.ndarray):
    """Index arrays (higher-rated, lower-rated) and pair weights over pairs with different ratings."""
    i, j = np.triu_indices(len(ratings), 1)
    keep = ratings[i] != ratings[j]
    i, j = i[keep], j[keep]
    hi = np.where(ratings[i] > ratings[j], i, j)
    lo = np.where(ratings[i] > ratings[j], j, i)
    return hi, lo, weights[i] * weights[j]


def concordance(W: np.ndarray, X: np.ndarray, hi, lo, pw) -> np.ndarray:
    """Weighted concordance for each row of W (m, components)."""
    out = np.empty(len(W))
    for start in range(0, len(W), 256):
        S = W[start:start + 256] @ X.T
        d = S[:, hi] - S[:, lo]
        out[start:start + 256] = ((d > 1e-12) + 0.5 * (np.abs(d) <= 1e-12)) @ pw / pw.sum()
    return out


def search(X, hi, lo, pw, old: np.ndarray, rng) -> np.ndarray:
    active = X.std(0) > 1e-9
    scale = np.abs(old[active]).sum() or 1.0
    k = active.sum()

    def to_weights(mags: np.ndarray) -> np.ndarray:
        W = np.tile(old, (len(mags), 1))
        W[:, active] = mags / mags.sum(1, keepdims=True) * scale * SIGNS[active]
        return W

    cands = np.vstack([old[None], to_weights(rng.dirichlet(np.ones(k), CFG["random_samples"]))])
    scores = concordance(cands, X, hi, lo, pw)
    best, best_score = cands[scores.argmax()], scores.max()
    for _ in range(CFG["refine_rounds"]):
        mags = np.abs(best[active]) + 1e-6
        trial = to_weights(mags * np.exp(rng.normal(0, 0.35, (CFG["refine_batch"], k))))
        s = concordance(trial, X, hi, lo, pw)
        if s.max() > best_score:
            best, best_score = trial[s.argmax()], s.max()
    return best


def due(conn: sqlite3.Connection) -> bool:
    n = learning_ratings(conn)
    on_schedule = n >= CFG["first_at"] and (n - CFG["first_at"]) % CFG["every"] == 0
    done = conn.execute("SELECT 1 FROM reweightings WHERE at_ratings = ?", (n,)).fetchone()
    return on_schedule and not done


def reweight(conn: sqlite3.Connection) -> dict:
    """Fit and adopt new weights from all learning ratings so far; logs the event."""
    t0 = time.monotonic()
    _, X, r, g = learning_data(conn)
    decay = CONFIG["averages"]["decay"] ** (current_generation(conn) - g)
    hi, lo, pw = pairs(r, decay)
    old_id, old_w = current_weights(conn)
    old = np.array([old_w[c] for c in COMPONENTS])
    n = len(r)
    if len(hi) == 0:
        new = old
    else:
        new = search(X, hi, lo, pw, old, np.random.default_rng(n))
    old_c, new_c = ([float(v) for v in concordance(np.vstack([old, new]), X, hi, lo, pw)] if len(hi) else [None, None])
    new_w = {c: round(float(v), 6) for c, v in zip(COMPONENTS, new)}
    with conn:
        new_id = conn.execute("INSERT INTO weight_sets (reason, weights) VALUES ('re-weighting', ?)",
                              (json.dumps(new_w),)).lastrowid
        conn.execute(
            "INSERT INTO reweightings (at_ratings, old_weight_set_id, new_weight_set_id, pairs,"
            " old_concordance, new_concordance) VALUES (?, ?, ?, ?, ?, ?)",
            (n, old_id, new_id, int(len(hi)), None if old_c is None else float(old_c),
             None if new_c is None else float(new_c)))
    db.log_event(conn, "reweighted", at_ratings=n, pairs=int(len(hi)), seconds=round(time.monotonic() - t0, 3),
                 old_concordance=old_c, new_concordance=new_c)
    return {"at_ratings": n, "old": old_w, "new": new_w, "old_concordance": old_c, "new_concordance": new_c}


def evaluate_pending(conn: sqlite3.Connection) -> None:
    """Old vs new weights on the eval_window learning ratings that followed each re-weighting."""
    window = CFG["eval_window"]
    rated = conn.execute("SELECT id, components, rating, generation FROM plays"
                         " WHERE is_control = 0 AND rating IS NOT NULL ORDER BY rated_at, id").fetchall()
    for rw in conn.execute("SELECT * FROM reweightings WHERE eval_n IS NULL").fetchall():
        nxt = rated[rw["at_ratings"]:rw["at_ratings"] + window]
        if len(nxt) < window:
            continue
        _, X, r, _ = learning_data(conn, nxt)
        hi, lo, pw = pairs(r, np.ones(len(r)))
        W = np.array([[json.loads(conn.execute("SELECT weights FROM weight_sets WHERE id = ?", (wid,))
                                  .fetchone()[0])[c] for c in COMPONENTS]
                      for wid in (rw["old_weight_set_id"], rw["new_weight_set_id"])])
        old_c, new_c = concordance(W, X, hi, lo, pw) if len(hi) else (None, None)
        with conn:
            conn.execute("UPDATE reweightings SET eval_n = ?, eval_pairs = ?, eval_old_concordance = ?,"
                         " eval_new_concordance = ?, evaluated_at = datetime('now') WHERE id = ?",
                         (len(nxt), int(len(hi)), None if old_c is None else float(old_c),
                          None if new_c is None else float(new_c), rw["id"]))


def after_rating(conn: sqlite3.Connection) -> dict | None:
    """Run after every new rating: evaluate earlier re-weightings, then re-weight if due."""
    evaluate_pending(conn)
    return reweight(conn) if due(conn) else None
