"""Point-in-time replay of "sounds like" as a heat map (feedback #35), and of re-weighting on a clear gain (#30/31)."""
import json, pickle, sys
import numpy as np
from scipy.stats import rankdata, spearmanr
from music_hunter import db, names, veto
import sqlite3
from music_hunter.config import CONFIG
from music_hunter.ratings import POSITIVE_WEIGHTS
V = CONFIG["veto"]; ADD_STARS = CONFIG["find"]["add_seed_stars"]; MINREFS = CONFIG["scoring"]["audio_match_min_refs"]
rz = veto.robust_z
HS = (0.03, 0.05, 0.08, 0.12, 0.2)
def topk(S, w, k):
    k = min(k, S.shape[1]); top = np.argsort(-S, axis=1)[:, :k]
    s, ww = np.take_along_axis(S, top, 1), w[top]
    return (s * ww).sum(1) / ww.sum(1)
def kernel(S, w, h):
    return (np.exp(-((1 - S) ** 2) / (2 * h * h)) * w[None, :]).sum(1)
lib = db.connect_library()
recs = []
ARCH = sys.argv[2] if len(sys.argv) > 2 else None
sessions = [(0, ARCH)] if ARCH else lib.execute("SELECT id, name FROM playlists ORDER BY id").fetchall()
for pid, name in sessions:
    if ARCH:
        c = sqlite3.connect(ARCH); c.row_factory = sqlite3.Row
    else:
        c = db.connect(pid)
    who = {r[0]: names.song(r[1], r[2]).artists for r in c.execute("SELECT id, artist, title FROM tracks")}
    rows = c.execute(f"SELECT t.id, t.origin, {'NULL AS stars' if ARCH else 't.stars'}, t.created_at, e.embedding FROM tracks t JOIN track_embeddings e ON e.track_id=t.id").fetchall()
    ids = np.array([r["id"] for r in rows]); E = np.stack([db.from_blob(r["embedding"]) for r in rows]).astype(np.float64)
    idx = {int(i): k for k, i in enumerate(ids)}
    created = np.array([r["created_at"] or "" for r in rows]); ispool = np.array([r["origin"] == "pool" for r in rows])
    refs = [(idx[r["id"]], r["stars"] if not ARCH else 5, r["created_at"] or "") for r in rows if r["origin"] != "pool" and (ARCH or r["stars"] is not None)]
    tid = {k: int(i) for k, i in enumerate(ids)}
    HASV = any(r[1] == "veto" for r in c.execute("PRAGMA table_info(plays)"))
    rated = c.execute(("SELECT p.id, p.track_id, p.rating, p.rated_at, p.shown_at, p.is_control, {VETO} FROM plays p WHERE p.rating IS NOT NULL ORDER BY p.id").replace("{VETO}", "p.veto" if HASV else "NULL AS veto")).fetchall()
    rated = [p for p in rated if p["track_id"] in idx]
    SIM = E @ E.T
    for p in rated:
        v = json.loads(p["veto"]) if p["veto"] else {}
        if "z_components" not in v and not ARCH: continue
        T = p["shown_at"]; me = idx[p["track_id"]]
        mask = np.where(ispool & (created < T))[0]
        if me not in mask: mask = np.append(mask, me)
        pos_me = int(np.where(mask == me)[0][0])
        earlier = [q for q in rated if (q["rated_at"] or "") < T and q["id"] != p["id"]]
        seeds = [(a, b) for a, b, cr in refs if cr < T and a != me]
        seeds += [(idx[q["track_id"]], ADD_STARS) for q in earlier if q["rating"] == 3 and not q["is_control"] and idx[q["track_id"]] != me]
        if not seeds: continue
        si = np.array([s[0] for s in seeds]); st = np.clip(np.array([float(s[1]) for s in seeds]), 1, 5)
        S = SIM[np.ix_(mask, si)].copy(); S[mask[:, None] == si[None, :]] = -np.inf
        plain = topk(S, np.ones(len(si)), V["seed_top_k"])
        spread = 1.4826 * np.median(np.abs(plain - np.median(plain))) or 1.0
        cur_seed = topk(S - (V["star_handicap_sd"] * spread * (5 - st) / 4)[None, :], np.ones(len(si)), V["seed_top_k"])
        S2 = np.where(np.isfinite(S), S, -1.0)
        likes = [(idx[q["track_id"]], POSITIVE_WEIGHTS[q["rating"]]) for q in earlier if q["rating"] in POSITIVE_WEIGHTS and idx[q["track_id"]] != me]
        offs = [idx[q["track_id"]] for q in earlier if q["rating"] == 1 and idx[q["track_id"]] != me]
        parts = {"seed_top3": rz(cur_seed)[pos_me]}
        L = None
        if likes:
            li = np.array([x[0] for x in likes]); lw = np.array([x[1] for x in likes], float)
            L = SIM[np.ix_(mask, li)].copy(); L[mask[:, None] == li[None, :]] = -np.inf
            parts["like_top3"] = rz(topk(L, lw, V["like_top_k"] if len(likes) >= MINREFS else 1))[pos_me]
            L2 = np.where(np.isfinite(L), L, -1.0)
        O2 = None
        if offs:
            oi = np.array(offs); O2 = SIM[np.ix_(mask, oi)].copy(); O2[mask[:, None] == oi[None, :]] = -1.0
        for h in HS:
            sh = kernel(S2, st / 5, h)
            parts[f"seedheat_{h}"] = rz(np.log(sh + 1e-12))[pos_me]
            if likes:
                lh = kernel(L2, lw, h); parts[f"likeheat_{h}"] = rz(np.log(lh + 1e-12))[pos_me]
                parts[f"allheat_{h}"] = rz(np.log(sh + (lh / 2) + 1e-12))[pos_me]     # Add = 2/2 = 5 stars' weight, Close 0.25
            if offs:
                oh = kernel(O2, np.ones(len(oi)), h)
                base = sh + (lh / 2 if likes else 0)
                for a in (0.25, 0.5, 1.0):
                    parts[f"signed{a}_{h}"] = rz(base - a * oh)[pos_me]
                # what the map says at that spot: the weighted average of Add/seed (+1) · Close (0) · Off (−1) nearby
                pw = sh + (kernel(L2, (lw == 2).astype(float), h) if likes else 0)
                cw = kernel(L2, (lw == 0.5).astype(float), h) if likes else 0
                parts[f"ratingmap_{h}"] = rz((pw - oh) / (pw + cw + oh + 1e-9))[pos_me]
        zc = v.get("z_components", {})
        # the rating map again, leaving out every seed and rating by this track's own artists: is it the sound, or the artist?
        mine = who.get(p["track_id"], frozenset())
        keep = lambda arr: np.array([not (who.get(tid[int(a)], frozenset()) & mine) for a in arr], bool)
        ks = keep(si)
        if likes: kl = keep(li)
        if offs: ko = keep(oi)
        for h in (0.05, 0.08, 0.2):
            pw2 = kernel(S2[:, ks], (st / 5)[ks], h) if ks.any() else np.zeros(len(mask))
            cw2 = np.zeros(len(mask))
            if likes and kl.any():
                pw2 = pw2 + kernel(L2[:, kl], (lw[kl] == 2).astype(float), h); cw2 = kernel(L2[:, kl], (lw[kl] == 0.5).astype(float), h)
            oh2 = kernel(O2[:, ko], np.ones(ko.sum()), h) if offs and ko.any() else np.zeros(len(mask))
            parts[f"ratingmap_noartist_{h}"] = rz((pw2 - oh2) / (pw2 + cw2 + oh2 + 1e-9))[pos_me]
        recs.append(dict(pid=pid, play=p["id"], at=T, rating=p["rating"], control=int(p["is_control"]), parts=parts,
                         sources=zc.get("sources", 0.0), tempo=zc.get("tempo", 0.0),
                         log_seed=zc.get("seed_closeness", np.nan), log_like=zc.get("like_closeness", np.nan), log_z=v.get("z", np.nan)))
    print(name, "plays", sum(r["pid"] == pid for r in recs), file=sys.stderr)
pickle.dump(recs, open(sys.argv[1], "wb"))
