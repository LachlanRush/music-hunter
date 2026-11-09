"""Do the sweep's leads hold in the older runs? Those ranked their picks, so controls are the fairest part; few, so all plays too."""
import json, re, sqlite3
from datetime import datetime
import numpy as np
from scipy.stats import rankdata, spearmanr
from music_hunter import names
ts = lambda s: datetime.fromisoformat(s[:19])
blob = lambda b: np.frombuffer(b, dtype=np.float32).astype(np.float64)
def auc(s, t):
    a, b = s[t], s[~t]
    if not len(a) or not len(b): return np.nan
    x = rankdata(np.r_[a, b]); return (x[:len(a)].sum() - len(a) * (len(a) + 1) / 2) / (len(a) * len(b))
allrows = {}
for v in ("v1", "v2", "v3"):
    c = sqlite3.connect(f"runs/{v}/data/music_hunter.db"); c.row_factory = sqlite3.Row
    cols = [r[1] for r in c.execute("PRAGMA table_info(tracks)")]
    has = lambda n: f"t.{n}" if n in cols else "NULL"
    plays = c.execute(f"SELECT q.*, t.artist, t.title, {has('duration_s')} AS duration_s, {has('release_date')} AS rd, {has('candidate_id')} AS cid FROM plays q JOIN tracks t ON t.id=q.track_id ORDER BY q.id").fetchall()
    sug = {}
    try:
        for r in c.execute("SELECT candidate_id, source FROM suggestions WHERE candidate_id IS NOT NULL"): sug.setdefault(r[0], set()).add(r[1])
    except sqlite3.OperationalError: pass
    prov = lambda q: {x[1] for x in json.loads(q["provenance"])} if q["provenance"] else set()
    E = {}
    if v == "v3":
        E = {r[0]: blob(r[1]) for r in c.execute("SELECT track_id, embedding FROM track_embeddings")}
        seeds = [r[0] for r in c.execute("SELECT id FROM tracks WHERE origin!='pool'") if r[0] in E]
    run, last, rows = 0, None, []
    top = c.execute("SELECT max(rating) FROM plays").fetchone()[0]
    for i, q in enumerate(plays):
        t0 = ts(q["shown_at"]); run = run + 1 if last and (t0 - last).total_seconds() < 1800 else 1; last = t0
        if q["rating"] is None: continue
        s = names.song(q["artist"], q["title"]); m = re.match(r"(\d{4})", q["rd"] or "")
        src = sug.get(q["cid"]) or prov(q)
        f = {"sitting": float(run), "length": q["duration_s"] or np.nan, "artists": float(len(s.artists)), "year": float(m.group(1)) if m else np.nan, "lastfm": float("lastfm" in src), "n_src": float(len(src))}
        if i and plays[i - 1]["rating"]: f["prev_top"] = float(plays[i - 1]["rating"] == top)
        d = t0; f["weekend"] = float(d.weekday() >= 5)
        if v == "v3" and q["track_id"] in E:
            e = E[q["track_id"]]; off = [p["track_id"] for p in plays[:i] if p["rating"] == 1 and p["track_id"] in E]
            sc = np.sort([e @ E[a] for a in seeds])[-3:].mean()
            f["seeds"] = float(sc)
            if len(off) >= 3:
                oc = np.sort([e @ E[a] for a in off])[-3:].mean(); f["offs"] = float(oc); f["seeds-offs"] = float(sc - oc)
            ch = [blob(r[0]) for r in c.execute("SELECT embedding FROM chunk_embeddings WHERE track_id=?", (q["track_id"],))]
            if ch:
                C = np.stack(ch); C /= np.linalg.norm(C, axis=1, keepdims=True); f["best_section"] = float((np.sort(C @ np.stack([E[a] for a in seeds]).T, axis=1)[:, -3:].mean(1)).max())
        rows.append(dict(y=q["rating"], top=top, ctl=q["is_control"], f=f))
    allrows[v] = rows
    print(f"\n{v}: {len(rows)} plays, {sum(r['ctl'] for r in rows)} controls; sat for up to {max(r['f']['sitting'] for r in rows):.0f} plays; dates weekend share {np.mean([r['f']['weekend'] for r in rows]):.2f}")
    for k in ("sitting", "length", "artists", "year", "lastfm", "n_src", "prev_top", "weekend", "seeds", "offs", "seeds-offs", "best_section"):
        for lab, rs in (("all", rows), ("controls", [r for r in rows if r["ctl"]])):
            x = np.array([r["f"].get(k, np.nan) for r in rs], float); y = np.array([r["y"] for r in rs]); m = ~np.isnan(x)
            if m.sum() < 8 or np.std(x[m]) == 0: continue
            print(f"   {k:13s} {lab:8s} n={m.sum():3d} rho {spearmanr(x[m], y[m])[0]:+.2f} bottom-rating AUC {auc(-x[m], y[m]==1):.2f}")
