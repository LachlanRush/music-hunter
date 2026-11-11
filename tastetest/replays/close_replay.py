"""What dropping Close as a like does, and what treating Close as Off would do (2026-11-11, my question).

Point in time on every normal pick with a logged veto: closeness to earlier likes, as the veto computes it, with
  A  Close 0.5 + Add 2 (the rule until 2026-11-11)
  B  Add only (Close becomes "heard": what happens from now on)
  C  Close as Off: Add-only likes as B, and Close counts −1 in the artist value instead of 0
Each judged on its own and inside the whole veto score (the logged seed, sources and tempo parts, equal weights).
Run with MUSIC_HUNTER_ROOT=<snapshot>."""
import json
import numpy as np
from scipy.stats import spearmanr
from sklearn.metrics import roc_auc_score
from music_hunter import db, names, veto
from music_hunter.config import CONFIG

V = CONFIG["veto"]; MINREFS = CONFIG["scoring"]["audio_match_min_refs"]
rz = veto.robust_z


def topk(S, w, k):
    k = min(k, S.shape[1]); top = np.argsort(-S, axis=1)[:, :k]
    s, ww = np.take_along_axis(S, top, 1), w[top]
    return (s * ww).sum(1) / ww.sum(1)


lib = db.connect_library()
recs = []
for pid, name in lib.execute("SELECT id, name FROM playlists ORDER BY id").fetchall():
    try:
        c = db.connect(pid)
        rated = c.execute("SELECT p.id, p.track_id, p.rating, p.rated_at, p.shown_at, p.is_control, p.veto, p.played_seconds"
                          " FROM plays p WHERE p.rating IS NOT NULL ORDER BY p.id").fetchall()
    except Exception:
        continue
    rows = c.execute("SELECT t.id, t.origin, t.created_at, t.artist, t.title, e.embedding FROM tracks t"
                     " JOIN track_embeddings e ON e.track_id = t.id").fetchall()
    if not rows or not rated:
        continue
    ids = np.array([r["id"] for r in rows]); E = np.stack([db.from_blob(r["embedding"]) for r in rows]).astype(np.float64)
    idx = {int(i): k for k, i in enumerate(ids)}
    who = {r["id"]: names.song(r["artist"], r["title"]).artists for r in rows}
    created = np.array([r["created_at"] or "" for r in rows]); ispool = np.array([r["origin"] == "pool" for r in rows])
    SIM = E @ E.T
    rated = [p for p in rated if p["track_id"] in idx]
    for p in rated:
        v = json.loads(p["veto"]) if p["veto"] else {}
        zc = v.get("z_components")
        if not zc or p["is_control"]:
            continue
        T = p["shown_at"]; me = idx[p["track_id"]]
        mask = np.where(ispool & (created < T))[0]
        if me not in mask:
            mask = np.append(mask, me)
        pos = int(np.where(mask == me)[0][0])
        earlier = [q for q in rated if (q["rated_at"] or "") < T and q["id"] != p["id"] and idx[q["track_id"]] != me]
        out = {}
        for key, wmap, mins in (("A", {3: 2.0, 2: 0.5}, 0), ("B", {3: 2.0}, 0), ("L60", {3: 2.0, 2: 0.5}, 60),
                                ("L120", {3: 2.0, 2: 0.5}, 120), ("L180", {3: 2.0, 2: 0.5}, 180)):
            likes = [(idx[q["track_id"]], wmap[q["rating"]]) for q in earlier if q["rating"] in wmap
                     and (q["rating"] == 3 or (q["played_seconds"] or 0) >= mins)]
            if not likes:
                out[key] = 0.0
                continue
            li = np.array([x[0] for x in likes]); lw = np.array([x[1] for x in likes])
            L = SIM[np.ix_(mask, li)].copy(); L[mask[:, None] == li[None, :]] = -np.inf
            out[key] = float(rz(topk(L, lw, V["like_top_k"] if len(likes) >= MINREFS else 1))[pos])
        # artist value within this playlist (Add +1, Close 0 or −1, Off −1), 5+ earlier ratings, other songs only
        mine, song = who[p["track_id"]], None
        hist = [q for q in earlier if who.get(q["track_id"], frozenset()) & mine]
        art = {}
        for key, close in (("A", 0.0), ("C", -1.0)):
            vals = [{3: 1.0, 2: close, 1: -1.0}[q["rating"]] for q in hist]
            art[key] = float(np.mean(vals)) / V.get("artist_scale", 0.5) if len(vals) >= V.get("artist_min_ratings", 5) else 0.0
        recs.append(dict(pid=pid, rating=p["rating"], seed=zc.get("seed_closeness", 0.0), sources=zc.get("sources", 0.0),
                         tempo=zc.get("tempo", 0.0), likeA=out["A"], likeB=out["B"], L60=out["L60"], L120=out["L120"], L180=out["L180"],
                         played=p["played_seconds"] or 0, artA=art["A"], artC=art["C"]))

R = {k: np.array([r[k] for r in recs]) for k in recs[0]}
off, add = (R["rating"] == 1).astype(int), (R["rating"] == 3).astype(int)
share = V.get("artist_share", 0.4)


def report(label, score):
    rho = spearmanr(score, R["rating"])[0]
    print(f"{label:44s} order ρ {rho:+.3f}   spots Offs {roc_auc_score(off, -score):.3f}   spots Adds {roc_auc_score(add, score):.3f}")


print(f"{len(recs)} normal picks: {off.sum()} Off, {(R['rating'] == 2).sum()} Close, {add.sum()} Add\n")
report("like closeness, A: Close + Add", R["likeA"])
report("like closeness, B: Add only (Close = heard)", R["likeB"])
for k in ("L60", "L120", "L180"):
    report(f"like closeness, Add + Close played ≥ {k[1:]} s", R[k])
base = lambda like: (R["seed"] + like + R["sources"] + R["tempo"]) / 4
report("whole score, A", (1 - share) * base(R["likeA"]) + share * R["artA"])
report("whole score, B", (1 - share) * base(R["likeB"]) + share * R["artA"])
report("whole score, C: B + Close as −1 for artists", (1 - share) * base(R["likeB"]) + share * R["artC"])
rng = np.random.default_rng(0)
d = []
for _ in range(2000):
    i = rng.integers(0, len(recs), len(recs))
    a, b = R["likeA"][i], R["likeB"][i]
    if off[i].min() == off[i].max():
        continue
    d.append(roc_auc_score(off[i], -b) - roc_auc_score(off[i], -a))
print(f"\nOff spotting, B − A: {np.mean(d):+.3f} (95% {np.percentile(d, 2.5):+.3f} to {np.percentile(d, 97.5):+.3f})")

cl = R["rating"] == 2
print("\nplayed seconds (median): Off", np.median(R["played"][R["rating"] == 1]), " Close", np.median(R["played"][cl]),
      " Add", np.median(R["played"][R["rating"] == 3]))
