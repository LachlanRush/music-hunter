"""Score the pool — SPEC.md §7. Fast enough to re-run after every rating.

Each component is computed raw, then normalised to 0–1 across the current pool
(min–max; a component that is the same for every track normalises to 0). The score is
the weighted sum of normalised components, with penalties carrying negative weights.

dislike_match (v2, SPEC.md §15.7) is the mean similarity to the few closest tracks rated
Off, normalised like the other components — so one close match can't dominate, and the
max over a growing negative set no longer saturates (v1 problem).
"""

import json
import math
import sqlite3

import numpy as np

from . import db
from .config import CONFIG
from .names import split_artists
from .ratings import learning_ratings, negative_set, positive_set, seed_averages, source_averages

CFG = CONFIG["scoring"]
COMPONENTS = ("audio_match", "dislike_match", "source_score", "seed_score",
              "agreement", "personal_model", "diversity_penalty", "bpm_match", "year_match")


def current_weights(conn: sqlite3.Connection) -> tuple[int, dict[str, float]]:
    """Latest weight set, creating the initial one from config.toml on first use."""
    row = conn.execute("SELECT id, weights FROM weight_sets ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        # Components added after this set was made start at their configured weight.
        return row["id"], {**CFG["weights"], **json.loads(row["weights"])}
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


def _year(date: str | None) -> float | None:
    return float(date[:4]) if date and date[:4].isdigit() else None


def _liked(conn: sqlite3.Connection, column: str) -> list:
    ids = positive_set(conn)[0]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return [r[0] for r in conn.execute(f"SELECT {column} FROM tracks WHERE id IN ({marks})", ids) if r[0]]


def target_bpm(conn: sqlite3.Connection) -> tuple[float | None, str]:
    """The BPM to aim for: the one I set in the player, else the median of seeds and liked tracks."""
    chosen = db.get_setting(conn, "target_bpm")
    if chosen:
        return float(chosen), "set"
    bpms = _liked(conn, "bpm")
    return (float(np.median(bpms)), "from liked tracks") if bpms else (None, "none")


def target_year(conn: sqlite3.Connection) -> float | None:
    years = [y for y in (_year(d) for d in _liked(conn, "release_date")) if y]
    return float(np.median(years)) if years else None


def closeness(values: list[float | None], target: float | None, sigma: float, window: float = 0.0,
              edge: float = 1.0) -> np.ndarray:
    """How close each value is to the target, 0–1.

    No window: exp(-½(d/sigma)²) by distance d. With a window: 1 at the target, falling
    linearly to `edge` at ±window (closer is still better), then edge·exp(-½((d − window)/sigma)²)
    beyond it. Unknown values get the mean of the known ones (neutral). No target: all zero.
    """
    if target is None:
        return np.zeros(len(values))

    def one(v: float) -> float:
        d = abs(v - target)
        if window <= 0:
            return float(np.exp(-0.5 * (d / sigma) ** 2))
        if d <= window:
            return 1.0 - (1.0 - edge) * d / window
        return edge * float(np.exp(-0.5 * ((d - window) / sigma) ** 2))

    out = np.array([np.nan if v is None else one(v) for v in values])
    known = out[~np.isnan(out)]
    out[np.isnan(out)] = known.mean() if known.size else 0.0
    return out


def minmax(x: np.ndarray) -> np.ndarray:
    span = x.max() - x.min() if x.size else 0.0
    return (x - x.min()) / span if span > 1e-12 else np.zeros_like(x)


def pool_tracks(conn: sqlite3.Connection):
    """Unrated, unplayed pool tracks with embeddings."""
    return conn.execute(
        "SELECT t.id, t.artist, t.title, t.candidate_id, t.bpm, t.release_date, c.song_artists, e.embedding"
        " FROM tracks t"
        " JOIN track_embeddings e ON e.track_id = t.id LEFT JOIN candidates c ON c.id = t.candidate_id"
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
    # Partial agreement (SPEC.md §15.5): other sources that suggested the same artist, any track.
    artist_sources: dict[str, set] = {}
    if CFG["artist_agreement_credit"]:
        for a, src in conn.execute(
                "SELECT artist, source FROM suggestions WHERE generation IN"
                f" (SELECT DISTINCT generation FROM candidates WHERE id IN ({marks}))",
                [r["candidate_id"] for r in rows]):
            for name in split_artists(a):
                artist_sources.setdefault(name, set()).add(src)
    src_avg = source_averages(conn)

    raw = {c: np.zeros(n) for c in COMPONENTS}

    _, R, wR = positive_set(conn)
    if len(R):
        sims = P @ R.T
        k = min(CFG["audio_match_top_k"] if len(R) >= CFG["audio_match_min_refs"] else 1, len(R))
        top = np.argsort(-sims, axis=1)[:, :k]
        s, w = np.take_along_axis(sims, top, 1), wR[top]
        raw["audio_match"] = (s * w).sum(1) / w.sum(1)

    _, N, _ = negative_set(conn)
    if len(N):
        k = min(CFG["dislike_top_k"], len(N))
        raw["dislike_match"] = np.sort(P @ N.T, axis=1)[:, -k:].mean(1)

    for i, r in enumerate(rows):
        pairs = prov.get(r["candidate_id"], [])
        raw["source_score"][i] = max((src_avg[src] for _, src in pairs), default=0.0)
        raw["seed_score"][i] = max((seed_avg[s] for s, _ in pairs), default=0.0)
        own = {src for _, src in pairs}
        via_artist = set().union(*(artist_sources.get(a, set()) for a in json.loads(r["song_artists"] or "[]")))
        raw["agreement"][i] = math.log(1 + len(pairs) + CFG["artist_agreement_credit"] * len(via_artist - own))

    if learning_ratings(conn) >= CFG["personal_model_min_ratings"]:
        raw["personal_model"] = personal_model(conn)(P)

    # A target I set: credit across ±max_stretch (playable at the target with Match tempo), highest
    # at the target and window_edge_credit at the window's edge; beyond it, falling away fast.
    # Otherwise: closeness to the seeds' / liked tracks' median tempo.
    bpm_target, bpm_source = target_bpm(conn)
    window = bpm_target * CONFIG["bpm"]["max_stretch"] if bpm_target and bpm_source == "set" else 0.0
    raw["bpm_match"] = closeness([r["bpm"] for r in rows], bpm_target, CONFIG["bpm"]["sigma"], window,
                                 CONFIG["bpm"]["window_edge_credit"])
    raw["year_match"] = closeness([_year(r["release_date"]) for r in rows], target_year(conn), CONFIG["year"]["sigma"])

    recent = conn.execute(
        "SELECT e.embedding FROM plays p JOIN track_embeddings e ON e.track_id = p.track_id"
        " ORDER BY p.id DESC LIMIT ?", (CFG["diversity_last_n"],)).fetchall()
    if recent:
        D = np.stack([db.from_blob(r["embedding"]) for r in recent])
        raw["diversity_penalty"] = (P @ D.T).max(1)

    norm = {c: minmax(raw[c]) for c in COMPONENTS}
    score = sum(weights.get(c, 0.0) * norm[c] for c in COMPONENTS)

    seed_label = {r["id"]: f"{r['artist']} – {r['title']}" for r in conn.execute("SELECT id, artist, title FROM tracks")}
    out = [{
        "track_id": r["id"], "artist": r["artist"], "title": r["title"], "candidate_id": r["candidate_id"],
        "raw": {c: float(raw[c][i]) for c in COMPONENTS},
        "norm": {c: float(norm[c][i]) for c in COMPONENTS},
        "score": float(score[i]), "weight_set_id": wid, "target_bpm": bpm_target,
        "provenance": sorted([seed_label[s], src] for s, src in prov.get(r["candidate_id"], [])),
    } for i, r in enumerate(rows)]
    out.sort(key=lambda r: r["score"], reverse=True)
    for rank, r in enumerate(out, 1):
        r["rank"] = rank
    return out
