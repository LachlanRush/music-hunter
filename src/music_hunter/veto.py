"""The veto — SPEC.md §17.2–17.4 (v4).

Every analysed pool track (played, playable or vetoed) is scored on three components, each a robust z-score
against all tracks analysed this run (median and MAD, so a long low tail doesn't stretch the scale):
  seed closeness — mean cosine to the k nearest seeds (original seeds and learning Adds), MAEST;
  like closeness — weighted mean cosine to the k nearest liked plays (Close / Add weights), once there are likes;
  sources        — number of distinct sources that suggested the track.
The veto score is their weighted mean, standardised the same way. An unplayed track is vetoed when its score
falls more than cut_sd below the median (or its seed closeness is more than backstop_sd below — clearly out of
the area), and becomes playable again only above return_sd (two lines, so it doesn't flip-flop).

Statistics are anchored to everything analysed this run, so the line doesn't drop as the best tracks are
played out of the pool. Vetoed tracks lose their audio but keep their fingerprint (pool.py brings audio back
if one returns). Every alternative rule's verdict is stored with the track, and logged with each play.
"""

import json
import sqlite3

import numpy as np

from . import db
from .config import CONFIG, resolve

CFG = CONFIG["veto"]
COMPONENTS = ("seed_closeness", "like_closeness", "sources")


def robust_z(x: np.ndarray) -> np.ndarray:
    med = np.median(x)
    mad = 1.4826 * np.median(np.abs(x - med))
    scale = mad if mad > 1e-9 else x.std()
    return (x - med) / scale if scale > 1e-9 else np.zeros_like(x)


def _topk_mean(sims: np.ndarray, weights: np.ndarray, k: int) -> np.ndarray:
    """Weighted mean of each row's k largest similarities (sims: tracks × references)."""
    k = min(k, sims.shape[1])
    top = np.argsort(-sims, axis=1)[:, :k]
    s, w = np.take_along_axis(sims, top, 1), weights[top]
    return (s * w).sum(1) / w.sum(1)


def analysed_tracks(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT t.id, t.candidate_id, t.file_path, t.veto_state, e.embedding,"
        " t.id IN (SELECT track_id FROM plays) AS played"
        " FROM tracks t JOIN track_embeddings e ON e.track_id = t.id WHERE t.origin = 'pool' ORDER BY t.id").fetchall()


def compute(conn: sqlite3.Connection) -> dict[int, dict]:
    """Veto details for every analysed pool track: {track_id: {...}} (nothing written)."""
    from .ratings import ADD, POSITIVE_WEIGHTS

    rows = analysed_tracks(conn)
    if not rows:
        return {}
    ids = np.array([r["id"] for r in rows])
    P = np.stack([db.from_blob(r["embedding"]) for r in rows]).astype(np.float64)

    seeds = conn.execute(
        "SELECT t.id, e.embedding FROM tracks t JOIN track_embeddings e ON e.track_id = t.id"
        " WHERE t.origin = 'original_reference'"
        f" OR t.id IN (SELECT track_id FROM plays WHERE is_control = 0 AND rating = {ADD})").fetchall()
    S = np.stack([db.from_blob(r["embedding"]) for r in seeds]).astype(np.float64)
    seed_ids = np.array([r["id"] for r in seeds])
    sims = P @ S.T
    sims[ids[:, None] == seed_ids[None, :]] = -np.inf          # an Added track isn't its own seed
    seed_close = _topk_mean(sims, np.ones(len(seed_ids)), CFG["seed_top_k"])

    likes = conn.execute(
        "SELECT p.track_id, p.rating, e.embedding FROM plays p JOIN track_embeddings e ON e.track_id = p.track_id"
        f" WHERE p.rating IN ({','.join(str(int(k)) for k in POSITIVE_WEIGHTS)})").fetchall()
    like_close = None
    if likes:
        L = np.stack([db.from_blob(r["embedding"]) for r in likes]).astype(np.float64)
        lw = np.array([POSITIVE_WEIGHTS[r["rating"]] for r in likes])
        ls = P @ L.T
        ls[ids[:, None] == np.array([r["track_id"] for r in likes])[None, :]] = -np.inf   # not itself
        k = CFG["like_top_k"] if len(likes) >= CONFIG["scoring"]["audio_match_min_refs"] else 1
        with np.errstate(invalid="ignore"):
            like_close = _topk_mean(ls, lw, k)
        like_close[~np.isfinite(like_close)] = np.nan                # a liked track with no other likes yet
        like_close = np.where(np.isnan(like_close), np.nanmedian(like_close), like_close)

    marks = ",".join("?" * len(rows))
    n_src = {}
    for cid, src in conn.execute(f"SELECT DISTINCT candidate_id, source FROM suggestions WHERE candidate_id IN ({marks})",
                                 [r["candidate_id"] for r in rows]):
        n_src[cid] = n_src.get(cid, 0) + 1
    sources = np.array([n_src.get(r["candidate_id"], 0) for r in rows], float)

    raw = {"seed_closeness": seed_close, "sources": sources}
    if like_close is not None:
        raw["like_closeness"] = like_close
    z = {c: robust_z(v) for c, v in raw.items()}
    w = {c: CFG["weights"].get(c, 0.0) for c in z}
    score = sum(w[c] * z[c] for c in z) / (sum(w.values()) or 1.0)
    zs = robust_z(score)
    pct = np.argsort(np.argsort(score)) / max(1, len(score) - 1)
    out = {}
    for i, r in enumerate(rows):
        out[int(r["id"])] = {
            "score": float(score[i]), "z": float(zs[i]), "percentile": float(pct[i]),
            "raw": {c: float(v[i]) for c, v in raw.items()}, "z_components": {c: float(v[i]) for c, v in z.items()},
            "rules": {"fixed_10pct": bool(pct[i] < 0.10), "fixed_20pct": bool(pct[i] < 0.20),
                      "sd_1.0": bool(zs[i] < -1.0), "sd_1.25": bool(zs[i] < -1.25), "sd_1.5": bool(zs[i] < -1.5),
                      "seed_closeness_below_0.75": bool(seed_close[i] < 0.75),
                      "backstop": bool(z["seed_closeness"][i] < -CFG["backstop_sd"])},
            "analysed": len(rows),
        }
    return out


def update(conn: sqlite3.Connection) -> dict:
    """Re-score every analysed track, move unplayed tracks across the line, drop vetoed tracks' audio.

    Returns counts: {analysed, playable, vetoed, newly_vetoed, returned, active}.
    """
    details = compute(conn)
    if not details:
        return {"analysed": 0, "playable": 0, "vetoed": 0, "newly_vetoed": 0, "returned": 0, "active": False}
    active = CFG["enabled"] and len(details) >= CFG["min_analysed"]
    rows = {r["id"]: r for r in analysed_tracks(conn)}
    vetoed_now, returned = [], []
    with conn:
        for tid, d in details.items():
            r = rows[tid]
            state = r["veto_state"] or "playable"
            if active and not r["played"]:
                if state != "vetoed" and (d["z"] < -CFG["cut_sd"] or d["rules"]["backstop"]):
                    state = "vetoed"
                    vetoed_now.append(tid)
                elif state == "vetoed" and d["z"] > -CFG["return_sd"] and not d["rules"]["backstop"]:
                    state = "playable"
                    returned.append(tid)
            elif not active and state == "vetoed" and not r["played"]:
                state = "playable"
                returned.append(tid)
            d["state"] = state
            conn.execute("UPDATE tracks SET veto_state = ?, veto = ? WHERE id = ?", (state, json.dumps(d), tid))
        for tid in vetoed_now:                        # vetoed: the fingerprint stays, the audio goes
            fp = rows[tid]["file_path"]
            if fp and fp.startswith(CONFIG["paths"]["cache"] + "/"):
                resolve(fp).unlink(missing_ok=True)
                conn.execute("UPDATE tracks SET file_path = NULL WHERE id = ?", (tid,))
    counts = {"analysed": len(details), "active": active, "newly_vetoed": len(vetoed_now), "returned": len(returned),
              "playable": playable_count(conn), "vetoed": conn.execute(
                  "SELECT count(*) FROM tracks WHERE origin = 'pool' AND veto_state = 'vetoed'"
                  " AND id NOT IN (SELECT track_id FROM plays)").fetchone()[0]}
    if vetoed_now or returned:
        db.log_event(conn, "veto_update", **counts)
    return counts


PLAYABLE = ("t.origin = 'pool' AND coalesce(t.veto_state, 'playable') = 'playable'"
            " AND t.id NOT IN (SELECT track_id FROM plays)")


def playable_count(conn: sqlite3.Connection) -> int:
    return conn.execute(f"SELECT count(*) FROM tracks t WHERE {PLAYABLE}").fetchone()[0]


def settings_version(conn: sqlite3.Connection) -> int:
    """ID of the current run settings (veto, controls, pool size), creating it on first use."""
    import hashlib

    settings = {"veto": CONFIG["veto"], "controls": CONFIG["controls"], "pool_target": CONFIG["pool"]["target_size"],
                "ratings": CONFIG["ratings"]}
    text = json.dumps(settings, sort_keys=True)
    h = hashlib.sha1(text.encode()).hexdigest()[:16]
    row = conn.execute("SELECT id FROM settings_versions WHERE hash = ?", (h,)).fetchone()
    if row:
        return row[0]
    with conn:
        return conn.execute("INSERT INTO settings_versions (hash, settings) VALUES (?, ?)", (h, text)).lastrowid
