"""Score the pool — SPEC.md §7. Fast enough to re-run after every rating.

Each component is computed raw, then normalised to 0–1 across the current pool
(min–max; a component that is the same for every track normalises to 0). The score is
the weighted sum of normalised components, with penalties carrying negative weights.

dislike_match is normalised per disliked track before weighting: similarity to each
1–2-rated track is rescaled to 0–1 across the pool, 2-rated tracks count half as much
as 1-rated, and the highest value is taken. It is not min–max normalised again, so a
pool close only to 2-rated tracks tops out at 0.5.
"""

import json
import math
import sqlite3

import numpy as np

from . import db
from .config import CONFIG
from .ratings import NEGATIVE_WEIGHTS, learning_ratings, negative_set, positive_set, seed_averages, source_averages

CFG = CONFIG["scoring"]
COMPONENTS = ("audio_match", "dislike_match", "source_score", "seed_score",
              "agreement", "personal_model", "diversity_penalty")


def current_weights(conn: sqlite3.Connection) -> tuple[int, dict[str, float]]:
    """Latest weight set, creating the initial one from config.toml on first use."""
    row = conn.execute("SELECT id, weights FROM weight_sets ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        return row["id"], json.loads(row["weights"])
    weights = dict(CFG["weights"])
    with conn:
        wid = conn.execute("INSERT INTO weight_sets (reason, weights) VALUES ('initial', ?)",
                           (json.dumps(weights),)).lastrowid
    return wid, weights


def personal_model(conn: sqlite3.Connection):
    """Ridge regression from CLAP embedding to rating, trained on learning ratings.

    The ridge strength is picked from [personal_model] alphas by exact leave-one-out
    error. Returns a function mapping embeddings (n, dim) to predicted ratings.
    """
    rows = conn.execute(
        "SELECT p.rating, e.embedding FROM plays p JOIN track_embeddings e ON e.track_id = p.track_id"
        " WHERE p.is_control = 0 AND p.rating IS NOT NULL").fetchall()
    X = np.stack([db.from_blob(r["embedding"]) for r in rows]).astype(np.float64)
    y = np.array([r["rating"] for r in rows], dtype=np.float64)
    x_mean, y_mean = X.mean(0), y.mean()
    U, sv, Vt = np.linalg.svd(X - x_mean, full_matrices=False)
    uy = U.T @ (y - y_mean)
    best = None
    for alpha in CONFIG["personal_model"]["alphas"]:
        shrink = sv**2 / (sv**2 + alpha)
        fitted = U @ (shrink * uy)
        hat = (U**2 * shrink).sum(1) + 1 / len(y)   # leverage, including the intercept
        loo = np.mean(((y - y_mean - fitted) / (1 - hat)) ** 2)
        if best is None or loo < best[0]:
            best = (loo, Vt.T @ (sv / (sv**2 + alpha) * uy))
    coef = best[1]
    return lambda P: (P.astype(np.float64) - x_mean) @ coef + y_mean


def minmax(x: np.ndarray) -> np.ndarray:
    span = x.max() - x.min() if x.size else 0.0
    return (x - x.min()) / span if span > 1e-12 else np.zeros_like(x)


def pool_tracks(conn: sqlite3.Connection):
    """Unrated, unplayed pool tracks with embeddings."""
    return conn.execute(
        "SELECT t.id, t.artist, t.title, t.candidate_id, e.embedding FROM tracks t"
        " JOIN track_embeddings e ON e.track_id = t.id"
        " WHERE t.origin = 'pool' AND t.rating IS NULL AND t.id NOT IN (SELECT track_id FROM plays)"
        " ORDER BY t.id").fetchall()


def score_pool(conn: sqlite3.Connection) -> list[dict]:
    """Score every unplayed pool track; returns rows sorted best first, with rank (1 = best)."""
    rows = pool_tracks(conn)
    if not rows:
        return []
    wid, weights = current_weights(conn)
    P = np.stack([db.from_blob(r["embedding"]) for r in rows])
    n = len(rows)

    # Provenance: distinct (seed, source) pairs per candidate.
    prov: dict[int, list[tuple[int, str]]] = {}
    marks = ",".join("?" * n)
    for r in conn.execute(f"SELECT DISTINCT candidate_id, seed_track_id, source FROM suggestions"
                          f" WHERE candidate_id IN ({marks})", [r["candidate_id"] for r in rows]):
        prov.setdefault(r[0], []).append((r[1], r[2]))
    seed_avg = seed_averages(conn, {s for pairs in prov.values() for s, _ in pairs})
    src_avg = source_averages(conn)

    raw = {c: np.zeros(n) for c in COMPONENTS}

    _, R, wR = positive_set(conn)
    if len(R):
        sims = P @ R.T
        k = min(CFG["audio_match_top_k"] if len(R) >= CFG["audio_match_min_refs"] else 1, len(R))
        top = np.argsort(-sims, axis=1)[:, :k]
        s, w = np.take_along_axis(sims, top, 1), wR[top]
        raw["audio_match"] = (s * w).sum(1) / w.sum(1)

    _, N, wN = negative_set(conn)
    if len(N):
        per_track = np.stack([minmax(col) for col in (P @ N.T).T], axis=1)  # (n, negatives), 0–1 each
        raw["dislike_match"] = (per_track * (wN / max(NEGATIVE_WEIGHTS.values()))).max(1)

    for i, r in enumerate(rows):
        pairs = prov.get(r["candidate_id"], [])
        raw["source_score"][i] = max((src_avg[src] for _, src in pairs), default=0.0)
        raw["seed_score"][i] = max((seed_avg[s] for s, _ in pairs), default=0.0)
        raw["agreement"][i] = math.log(1 + len(pairs))

    if learning_ratings(conn) >= CFG["personal_model_min_ratings"]:
        raw["personal_model"] = personal_model(conn)(P)

    recent = conn.execute(
        "SELECT e.embedding FROM plays p JOIN track_embeddings e ON e.track_id = p.track_id"
        " ORDER BY p.id DESC LIMIT ?", (CFG["diversity_last_n"],)).fetchall()
    if recent:
        D = np.stack([db.from_blob(r["embedding"]) for r in recent])
        raw["diversity_penalty"] = (P @ D.T).max(1)

    norm = {c: raw[c] if c == "dislike_match" else minmax(raw[c]) for c in COMPONENTS}
    score = sum(weights.get(c, 0.0) * norm[c] for c in COMPONENTS)

    seed_label = {r["id"]: f"{r['artist']} – {r['title']}" for r in conn.execute("SELECT id, artist, title FROM tracks")}
    out = [{
        "track_id": r["id"], "artist": r["artist"], "title": r["title"], "candidate_id": r["candidate_id"],
        "raw": {c: float(raw[c][i]) for c in COMPONENTS},
        "norm": {c: float(norm[c][i]) for c in COMPONENTS},
        "score": float(score[i]), "weight_set_id": wid,
        "provenance": sorted([seed_label[s], src] for s, src in prov.get(r["candidate_id"], [])),
    } for i, r in enumerate(rows)]
    out.sort(key=lambda r: r["score"], reverse=True)
    for rank, r in enumerate(out, 1):
        r["rank"] = rank
    return out
