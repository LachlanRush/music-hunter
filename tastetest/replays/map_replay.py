"""The heat map as described (#35): seeds and Adds raise the map, stronger and/or wider with their stars; Close adds nothing;
Off lowers it. Tried on the full fingerprint and on flat maps of it (PCA and UMAP, 2D and more). Each play is scored from
what was known just before it. The flat maps are fitted on the fingerprints only, so no rating reaches them."""
import json, pickle, sqlite3, sys, time
import numpy as np
from scipy.stats import norm, rankdata
from sklearn.decomposition import PCA
import umap
from music_hunter import db, veto
from music_hunter.config import CONFIG
ADD_STARS = CONFIG["find"]["add_seed_stars"]
ARCH = sys.argv[2] if len(sys.argv) > 2 else None
def rankz(v):
    return norm.ppf((rankdata(v) - 0.5) / len(v))
lib = db.connect_library()
sessions = [(0, ARCH)] if ARCH else lib.execute("SELECT id, name FROM playlists ORDER BY id").fetchall()
recs = []; maps = {}
for pid, name in sessions:
    t0 = time.time()
    if ARCH:
        c = sqlite3.connect(ARCH); c.row_factory = sqlite3.Row
    else:
        c = db.connect(pid)
    rows = c.execute(f"SELECT t.id, t.origin, {'NULL AS stars' if ARCH else 't.stars'}, t.created_at, t.video_id, e.embedding FROM tracks t JOIN track_embeddings e ON e.track_id=t.id").fetchall()
    E = np.stack([db.from_blob(r["embedding"]) for r in rows]).astype(np.float64); E /= np.linalg.norm(E, axis=1, keepdims=True)
    idx = {r["id"]: k for k, r in enumerate(rows)}; created = np.array([r["created_at"] or "" for r in rows])
    ispool = np.array([r["origin"] == "pool" for r in rows]); vid = np.array([r["video_id"] or f"x{k}" for k, r in enumerate(rows)])
    refs = [(idx[r["id"]], 5 if ARCH else r["stars"], r["created_at"] or "") for r in rows if r["origin"] != "pool" and (ARCH or r["stars"] is not None)]
    pc = PCA(n_components=20, random_state=0).fit(E); Zp = pc.transform(E)
    u = umap.UMAP(n_components=2, n_neighbors=30, min_dist=0.1, metric="cosine", random_state=0).fit_transform(E)
    # how well does each flat map keep a track's 10 nearest neighbours?
    def keep(X):
        n = min(600, len(E)); a = np.argsort(-(E[:n] @ E.T), axis=1)[:, 1:11]
        d = ((X[:n, None, :] - X[None, :, :]) ** 2).sum(-1); b = np.argsort(d, axis=1)[:, 1:11]
        return float(np.mean([len(set(a[i]) & set(b[i])) / 10 for i in range(n)]))
    spaces = {"pca2": Zp[:, :2], "pca5": Zp[:, :5], "pca20": Zp[:, :20], "umap2": u}
    info = {k: keep(v) for k, v in spaces.items()}
    print(name, "tracks", len(E), "variance kept pca2/5/20", pc.explained_variance_ratio_[:2].sum().round(3), pc.explained_variance_ratio_[:5].sum().round(3), pc.explained_variance_ratio_.sum().round(3), "| nearest-10 kept", {k: round(v, 2) for k, v in info.items()}, flush=True)
    scales = {k: float(np.sqrt(((v - v.mean(0)) ** 2).sum(1).mean())) for k, v in spaces.items()}
    D2 = {k: ((v[:, None, :] - v[None, :, :]) ** 2).sum(-1) for k, v in spaces.items()}
    D2["full"] = (1 - E @ E.T) ** 2; scales["full"] = 1.0
    HS = {"full": (0.03, 0.05, 0.08, 0.12), **{k: tuple(f * scales[k] for f in (0.03, 0.06, 0.12, 0.25)) for k in spaces}}
    HN = {"full": ("0.03", "0.05", "0.08", "0.12"), **{k: ("0.03", "0.06", "0.12", "0.25") for k in spaces}}
    maps[pid] = dict(name=name, umap=u, pca=Zp[:, :2], ids=np.array([r["id"] for r in rows]), origin=np.array([r["origin"] for r in rows]), stars=np.array([r["stars"] if r["stars"] is not None else 0 for r in rows], float), created=created)
    hasv = any(r[1] == "veto" for r in c.execute("PRAGMA table_info(plays)"))
    plays = c.execute(f"SELECT p.id, p.track_id, p.rating, p.rated_at, p.shown_at, p.is_control, {'p.veto' if hasv else 'NULL AS veto'} FROM plays p WHERE p.rating IS NOT NULL ORDER BY p.id").fetchall()
    plays = [p for p in plays if p["track_id"] in idx]
    maps[pid]["plays"] = [(idx[p["track_id"]], p["rating"]) for p in plays]
    for p in plays:
        v = json.loads(p["veto"]) if p["veto"] else {}
        if "z_components" not in v and not ARCH: continue
        T = p["shown_at"]; me = idx[p["track_id"]]
        mask = np.where(ispool & (created < T))[0]
        if me not in mask: mask = np.append(mask, me)
        pm = int(np.where(mask == me)[0][0])
        earlier = [q for q in plays if (q["rated_at"] or "") < T and q["id"] != p["id"]]
        pts = {}                                                          # one point per song: a seed row wins over its Add
        for q in earlier:
            if q["rating"] == 3 and not q["is_control"] and idx[q["track_id"]] != me: pts[vid[idx[q["track_id"]]]] = (idx[q["track_id"]], float(ADD_STARS))
        for a, s, cr in refs:
            if cr < T and a != me and vid[a] != vid[me]: pts[vid[a]] = (a, float(np.clip(s, 1, 5)))
        if not pts: continue
        pi = np.array([x[0] for x in pts.values()]); st = np.array([x[1] for x in pts.values()])
        oi = np.array([idx[q["track_id"]] for q in earlier if q["rating"] == 1 and idx[q["track_id"]] != me], int)
        out = {}
        for sp, dd in D2.items():
            dp = dd[np.ix_(mask, pi)]; dn = dd[np.ix_(mask, oi)] if len(oi) else None
            for h, hn in zip(HS[sp], HN[sp]):
                for area, hi in (("sameArea", np.full(len(pi), h)), ("widerStars", h * st / 3)):
                    K = np.exp(-dp / (2 * hi[None, :] ** 2)); K[mask[:, None] == pi[None, :]] = 0
                    Kn = None
                    if dn is not None:
                        Kn = np.exp(-dn / (2 * h * h)); Kn[mask[:, None] == oi[None, :]] = 0; Kn = Kn.sum(1)
                    for strength, w in (("sameStrength", np.ones(len(pi))), ("strongerStars", st / 3)):
                        pos = K @ w
                        for a in (0.0, 0.5, 1.0):
                            hv = pos - a * Kn if Kn is not None else pos
                            out[f"{sp}|h{hn}|{area}|{strength}|off{a}|total"] = rankz(hv)[pm]
                            if a > 0 and Kn is not None:
                                out[f"{sp}|h{hn}|{area}|{strength}|off{a}|balance"] = rankz(hv / (pos + a * Kn + 1e-9))[pm]
        zc = v.get("z_components", {})
        recs.append(dict(pid=pid, play=p["id"], at=T, rating=p["rating"], control=int(p["is_control"]), maps=out,
                         sources=zc.get("sources", 0.0), tempo=zc.get("tempo", 0.0)))
    print("  ", name, sum(r["pid"] == pid for r in recs), "plays,", round(time.time() - t0), "s", flush=True)
pickle.dump(recs, open(sys.argv[1], "wb")); pickle.dump(maps, open(sys.argv[1] + ".maps", "wb"))
