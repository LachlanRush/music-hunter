"""Everything measurable against the clean random picks. Each value is what was known just before the play."""
import json, re, sqlite3, sys
from datetime import datetime
import numpy as np
from scipy.stats import rankdata, spearmanr
from music_hunter import db, names
S = sys.argv[1]
lib = sqlite3.connect(f"{S}/data/music_hunter.db"); lib.row_factory = sqlite3.Row
pls = [(r[0], r[1]) for r in lib.execute("SELECT id, name FROM playlists ORDER BY id")]
blob = lambda b: np.frombuffer(b, dtype=np.float32).astype(np.float64)
ts = lambda s: datetime.fromisoformat(s[:19])
VAL = {1: -1, 2: 0, 3: 1}
conns = {p: sqlite3.connect(f"{S}/data/sessions/playlist-{p}.db") for p, _ in pls}
for c in conns.values(): c.row_factory = sqlite3.Row
hist = []
for p, c in conns.items():
    for r in c.execute("SELECT t.artist, t.title, q.rating, q.rated_at FROM plays q JOIN tracks t ON t.id=q.track_id WHERE q.rating IS NOT NULL"):
        s = names.song(r[0], r[1]); hist.append((s.artists, s.title, VAL[r[2]], r[3] or ""))
rows = []
for pid, name in pls:
    c = conns[pid]
    tr = {r["id"]: r for r in c.execute("SELECT * FROM tracks")}
    E = {r[0]: blob(r[1]) for r in c.execute("SELECT track_id, embedding FROM track_embeddings")}
    chunks = {}
    for r in c.execute("SELECT track_id, embedding FROM chunk_embeddings ORDER BY track_id, chunk_index"):
        chunks.setdefault(r[0], []).append(blob(r[1]))
    sug = {}
    for r in c.execute("SELECT candidate_id, source, rank, seed_track_id FROM suggestions WHERE candidate_id IS NOT NULL"):
        sug.setdefault(r[0], []).append(r)
    pool_seed = {r[0]: r[1] for r in c.execute("SELECT id, pool_seed FROM candidates")}
    refs = [(t["id"], t["stars"] if t["stars"] is not None else 3, t["created_at"] or "") for t in tr.values() if t["origin"] != "pool" and t["id"] in E]
    plays = c.execute("SELECT * FROM plays WHERE rating IS NOT NULL ORDER BY id").fetchall()
    allp = c.execute("SELECT * FROM plays ORDER BY id").fetchall()
    run = 0; last = None; sesspos = {}
    for q in allp:
        t0 = ts(q["shown_at"]); run = run + 1 if last and (t0 - last).total_seconds() < 1800 else 1; sesspos[q["id"]] = run; last = t0
    for i, p in enumerate(plays):
        v = json.loads(p["veto"]) if p["veto"] else {}
        if p["is_control"] or "z_components" not in v: continue
        t = tr[p["track_id"]]; T = p["shown_at"]; me = p["track_id"]
        if me not in E: continue
        comp = json.loads(p["components"]) if p["components"] else {}
        f = {}
        for k, x in v["z_components"].items(): f["part: " + k] = x
        f["whole score"] = v.get("z", np.nan)
        for k, x in comp.items(): f["old part: " + k] = x.get("raw", np.nan)
        s = names.song(t["artist"], t["title"])
        f["track: length"] = t["duration_s"] or np.nan
        m = re.match(r"(\d{4})", t["release_date"] or ""); f["track: release year"] = float(m.group(1)) if m else np.nan
        f["track: loudness"] = t["loudness_lufs"] if t["loudness_lufs"] is not None else np.nan
        f["track: true peak"] = t["true_peak_db"] if t["true_peak_db"] is not None else np.nan
        f["track: BPM read"] = v.get("tempo", {}).get("read", np.nan) if v.get("tempo") else np.nan
        low = t["title"].lower()
        f["title: remix"] = float("remix" in low or " edit" in low or " rework" in low or " bootleg" in low)
        f["title: extended"] = float("extended" in low)
        f["title: feat."] = float("feat" in low)
        f["credits: number of artists"] = float(len(s.artists))
        su = sug.get(t["candidate_id"], [])
        f["found: distinct seeds"] = float(len({x["seed_track_id"] for x in su}))
        f["found: all suggestions"] = float(len(su))
        ranks = [x["rank"] for x in su if x["rank"]]
        f["found: best list position"] = float(min(ranks)) if ranks else np.nan
        f["found: mean list position"] = float(np.mean(ranks)) if ranks else np.nan
        srcs = {x["source"] for x in su}
        for k in ("lastfm", "ytmusic", "ytrelated", "deezer", "tidal"): f[f"source: {k}"] = float(k in srcs)
        os_ = pool_seed.get(t["candidate_id"])
        f["found: owner seed's stars"] = float(tr[os_]["stars"]) if os_ in tr and tr[os_]["stars"] is not None else np.nan
        # sound, point in time
        earlier = [q for q in plays if (q["rated_at"] or "") < T and q["id"] != p["id"] and q["track_id"] in E]
        seeds = [a for a, st, cr in refs if cr < T and a != me] + [q["track_id"] for q in earlier if q["rating"] == 3 and not q["is_control"] and q["track_id"] != me]
        offs = [q["track_id"] for q in earlier if q["rating"] == 1 and q["track_id"] != me]
        adds = [q["track_id"] for q in earlier if q["rating"] == 3 and q["track_id"] != me]
        closes = [q["track_id"] for q in earlier if q["rating"] == 2 and q["track_id"] != me]
        e = E[me]
        top = lambda ids, k=3: float(np.mean(np.sort([e @ E[a] for a in ids])[-k:])) if len(ids) >= k else np.nan
        f["sound: nearest 3 Offs"] = top(offs); f["sound: nearest 3 Adds only"] = top(adds); f["sound: nearest 3 Closes only"] = top(closes)
        f["sound: Adds minus Offs"] = f["sound: nearest 3 Adds only"] - f["sound: nearest 3 Offs"]
        f["sound: nearest seed only"] = top(seeds, 1)
        f["sound: nearest 3 seeds, no stars"] = top(seeds)
        f["sound: 3 seeds minus 3 Offs"] = f["sound: nearest 3 seeds, no stars"] - f["sound: nearest 3 Offs"]
        recent = [q["track_id"] for q in allp if q["shown_at"] < T and q["track_id"] in E][-5:]
        f["sound: like the last 5 played"] = float(np.mean([e @ E[a] for a in recent])) if recent else np.nan
        ch = chunks.get(me)
        if ch and len(ch) >= 2 and seeds:
            Ch = np.stack(ch); Ch = Ch / np.linalg.norm(Ch, axis=1, keepdims=True)
            Se = np.stack([E[a] for a in seeds]); cl = np.sort(Ch @ Se.T, axis=1)[:, -3:].mean(1)
            f["sections: weakest section to seeds"] = float(cl.min()); f["sections: weakest third to seeds"] = float(np.sort(cl)[: max(1, len(cl) // 3)].mean())
            f["sections: best section to seeds"] = float(cl.max()); f["sections: uneven (spread)"] = float(cl.std())
            cc = Ch @ e; f["sections: how much the track changes"] = float(1 - cc.mean()); f["sections: most different section"] = float(1 - cc.min())
        # artist, point in time
        hh = [x for x in hist if x[3] < T and x[0] & s.artists and x[1] != s.title]
        f["artist: earlier ratings count"] = float(len(hh)); f["artist: earlier average (3+)"] = float(np.mean([x[2] for x in hh])) if len(hh) >= 3 else np.nan
        f["artist: heard before"] = float(len(hh) > 0)
        f["artist: is a seed's artist"] = float(any(names.song(tr[a]["artist"], tr[a]["title"]).artists & s.artists for a in seeds))
        # listening context
        f["context: position in sitting"] = float(sesspos[p["id"]])
        prev = [q for q in allp if q["id"] < p["id"]]
        f["context: previous was Off"] = float(prev[-1]["rating"] == 1) if prev and prev[-1]["rating"] else np.nan
        f["context: previous was Add"] = float(prev[-1]["rating"] == 3) if prev and prev[-1]["rating"] else np.nan
        d = ts(T); f["context: hour of day"] = float(d.hour); f["context: weekend"] = float(d.weekday() >= 5)
        f["context: plays so far in playlist"] = float(i)
        f["context: tempo stretched"] = abs((p["playback_rate"] or 1) - 1)
        rows.append(dict(pid=pid, play=p["id"], at=T, rating=p["rating"], auto=p["auto_rated"], f=f))
y = np.array([r["rating"] for r in rows]); pid = np.array([r["pid"] for r in rows]); at = np.array([r["at"] for r in rows])
print(f"{len(rows)} random picks: Off {(y==1).sum()} Close {(y==2).sum()} Add {(y==3).sum()}; by playlist", {int(p): int((pid==p).sum()) for p in set(pid)})
names_ = sorted({k for r in rows for k in r["f"]})
def auc(s, t):
    a, b = s[t], s[~t]
    if not len(a) or not len(b): return np.nan
    x = rankdata(np.r_[a, b]); return (x[:len(a)].sum() - len(a) * (len(a) + 1) / 2) / (len(a) * len(b))
def pct(x, g):          # rank within each playlist, so playlists can be pooled
    o = np.full(len(x), np.nan)
    for p in set(g):
        m = (g == p) & ~np.isnan(x)
        if m.sum() > 1: o[m] = (rankdata(x[m]) - 1) / (m.sum() - 1)
    return o
rng = np.random.default_rng(11); N = 4000
res = []; X = {}
Yr = np.empty(len(y)); 
for p in set(pid): Yr[pid == p] = rankdata(y[pid == p]) / (pid == p).sum()
cut = np.sort(at)[len(at) // 2]
for k in names_:
    x = np.array([r["f"].get(k, np.nan) for r in rows], float); z = pct(x, pid); m = ~np.isnan(z)
    if m.sum() < 40 or np.nanstd(z) == 0: continue
    X[k] = z
    rho = spearmanr(z[m], y[m])[0]
    idx = np.where(m)[0]; cnt = 0
    zm = z[m]; yr = rankdata(y[m]); zr = rankdata(zm); zr = (zr - zr.mean()) / zr.std()
    for _ in range(N):
        sh = yr.copy()
        for p in set(pid[m]):
            j = np.where(pid[m] == p)[0]; sh[j] = rng.permutation(yr[j])
        cnt += abs(np.mean(zr * (sh - sh.mean()) / sh.std())) >= abs(rho) - 1e-12
    per = {int(p): spearmanr(z[m & (pid == p)], y[m & (pid == p)])[0] if (m & (pid == p)).sum() > 8 and np.std(z[m & (pid == p)]) > 0 else np.nan for p in sorted(set(pid))}
    e, l = m & (at <= cut), m & (at > cut)
    res.append(dict(k=k, n=int(m.sum()), rho=rho, p=(cnt + 1) / (N + 1), off=auc(-z[m], y[m] == 1), add=auc(z[m], y[m] == 3), per=per,
                    early=spearmanr(z[e], y[e])[0] if np.std(z[e]) > 0 else np.nan, late=spearmanr(z[l], y[l])[0] if np.std(z[l]) > 0 else np.nan))
res.sort(key=lambda r: r["p"])
for i, r in enumerate(res): r["q"] = min(1, r["p"] * len(res) / (i + 1))
for i in range(len(res) - 2, -1, -1): res[i]["q"] = min(res[i]["q"], res[i + 1]["q"])
print(f"\n{len(res)} measures; q = chance-corrected across all of them\n{'measure':42s} {'n':>4} {'rho':>6} {'p':>6} {'q':>5} {'Off':>5} {'Add':>5}  pump  d&b  deep | early late")
for r in res:
    print(f"{r['k']:42s} {r['n']:4d} {r['rho']:+.3f} {r['p']:.4f} {r['q']:.2f} {r['off']:.3f} {r['add']:.3f} " + " ".join(f"{r['per'].get(p, np.nan):+.2f}" for p in (1, 2, 3)) + f" | {r['early']:+.2f} {r['late']:+.2f}")
# added value on top of the whole score
base = pct(np.array([r["f"].get("whole score", np.nan) for r in rows], float), pid); ok = ~np.isnan(base)
print(f"\nAdding each to the current score at a 25% share (direction taken from its rho); current: rho {spearmanr(base[ok], y[ok])[0]:+.3f} Off {auc(-base[ok], y[ok]==1):.3f} Add {auc(base[ok], y[ok]==3):.3f}")
out = []
for r in res:
    if r["k"] == "whole score": continue
    z = X[r["k"]]; z = np.where(np.isnan(z), .5, z); z = z if r["rho"] > 0 else 1 - z
    s = .75 * base + .25 * z
    d = []
    idx = np.where(ok)[0]
    for _ in range(600):
        i = rng.choice(idx, len(idx)); d.append(spearmanr(s[i], y[i])[0] - spearmanr(base[i], y[i])[0])
    out.append((np.mean(d), *np.percentile(d, [2.5, 97.5]), auc(-s[ok], y[ok]==1), auc(s[ok], y[ok]==3), r["k"]))
for g, lo, hi, o, a, k in sorted(out, reverse=True)[:14]:
    print(f"  {k:42s} rho gain {g:+.3f} ({lo:+.3f} to {hi:+.3f}) Off {o:.3f} Add {a:.3f}")
json.dump(dict(res=[{**r, "per": {str(a): b for a, b in r["per"].items()}} for r in res]), open(sys.argv[2], "w"))
