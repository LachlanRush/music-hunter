import pickle, sys
import numpy as np
from scipy.stats import rankdata, spearmanr
SP = sys.argv[1]
R = pickle.load(open(f"{SP}/map.pkl", "rb")); H = pickle.load(open(f"{SP}/heat.pkl", "rb")); V3 = pickle.load(open(f"{SP}/map_v3.pkl", "rb")); H3 = pickle.load(open(f"{SP}/heat_v3.pkl", "rb"))
hk = {(r["pid"], r["play"]): r for r in H}
R = [r for r in R if (r["pid"], r["play"]) in hk]
y = np.array([r["rating"] for r in R]); pid = np.array([r["pid"] for r in R]); ctl = np.array([r["control"] for r in R], bool); at = np.array([r["at"] for r in R])
src = np.array([r["sources"] for r in R]); tem = np.array([r["tempo"] for r in R])
def f(rs, n):
    a = np.array([x["parts"].get(n, np.nan) for x in rs]); return np.where(np.isnan(a), 0, a)
hs = [hk[(r["pid"], r["play"])] for r in R]
now = np.mean(np.vstack([f(hs, "seed_top3"), f(hs, "like_top3"), src, tem]), 0)
def auc(s, t):
    a, b = s[t], s[~t]
    if not len(a) or not len(b): return np.nan
    x = rankdata(np.r_[a, b]); return (x[:len(a)].sum() - len(a) * (len(a) + 1) / 2) / (len(a) * len(b))
def conc(s, yy):
    d = yy[:, None] > yy[None, :]; ds = s[:, None] - s[None, :]
    return (((ds > 0) & d).sum() + .5 * ((ds == 0) & d).sum()) / max(d.sum(), 1)
n = ~ctl
keys = sorted({k for r in R for k in r["maps"]})
def mscore(k):
    a = np.array([r["maps"].get(k, np.nan) for r in R]); return np.mean(np.vstack([np.where(np.isnan(a), 0, a), src, tem]), 0)
S = {k: mscore(k) for k in keys}
rows = []
for k, s in S.items():
    rows.append((k, spearmanr(s[n], y[n])[0], auc(-s[n], y[n] == 1), auc(s[n], y[n] == 3), conc(s[n], y[n])))
print(f"{len(keys)} variants; picks {n.sum()}, Offs {(y[n]==1).sum()}, Adds {(y[n]==3).sum()}")
print(f"NOW: rho {spearmanr(now[n], y[n])[0]:+.3f} Off {auc(-now[n], y[n]==1):.3f} Add {auc(now[n], y[n]==3):.3f} order {conc(now[n], y[n]):.3f}")
def show(rs, lab, N=6):
    print(lab)
    for k, r, o, a, c in rs[:N]: print(f"  {k:62s} rho {r:+.3f} Off {o:.3f} Add {a:.3f} order {c:.3f}")
rows.sort(key=lambda x: -x[4]); show(rows, "best by order, any")
for sp in ("full", "pca2", "pca5", "pca20", "umap2"):
    show([x for x in rows if x[0].startswith(sp + "|")], f"best in {sp}", 3)
print("\nEffect of each choice: mean across all variants that have it")
for pos, name in ((0, "space"), (1, "width"), (2, "area"), (3, "strength"), (4, "Off"), (5, "value")):
    groups = {}
    for x in rows: groups.setdefault(x[0].split("|")[pos], []).append(x)
    print(" ", name, {g: (round(np.mean([v[2] for v in vs]), 3), round(np.mean([v[3] for v in vs]), 3), round(np.mean([v[4] for v in vs]), 3)) for g, vs in sorted(groups.items())}, "(Off, Add, order)")
# user's described version, spelled out
print("\nYour description at each width, full fingerprint and flat maps (stronger + wider with stars, Close nothing, Off lowers it, total heat):")
for sp in ("full", "pca2", "umap2"):
    for x in sorted([x for x in rows if x[0].startswith(sp + "|") and "widerStars|strongerStars|off1.0|total" in x[0]], key=lambda v: v[0]):
        print(f"  {x[0]:62s} rho {x[1]:+.3f} Off {x[2]:.3f} Add {x[3]:.3f} order {x[4]:.3f}")
rng = np.random.default_rng(1); m = np.where(n)[0]
print("\nTop 5 vs NOW, paired resampling:")
for k, *_ in rows[:5]:
    d = []
    for _ in range(1500):
        i = rng.choice(m, len(m)); a, b, yy = S[k][i], now[i], y[i]
        d.append((conc(a, yy) - conc(b, yy), auc(-a, yy == 1) - auc(-b, yy == 1), auc(a, yy == 3) - auc(b, yy == 3)))
    d = np.array(d); print(f"  {k}\n     order {d[:,0].mean():+.3f} ({np.percentile(d[:,0],2.5):+.3f} to {np.percentile(d[:,0],97.5):+.3f})  Off {d[:,1].mean():+.3f} ({np.percentile(d[:,1],2.5):+.3f} to {np.percentile(d[:,1],97.5):+.3f})  Add {d[:,2].mean():+.3f} ({np.percentile(d[:,2],2.5):+.3f} to {np.percentile(d[:,2],97.5):+.3f})")
cut = np.sort(at[n])[n.sum() // 2]
print("\nBy playlist and half (NOW, then best):")
best = rows[0][0]
for lab, mm in [(f"playlist {p}", (pid == p) & n) for p in sorted(set(pid))] + [("earlier", n & (at <= cut)), ("later", n & (at > cut))]:
    print(f"  {lab:12s} picks {mm.sum():3d} Adds {(y[mm]==3).sum():2d} | NOW Off {auc(-now[mm], y[mm]==1):.3f} Add {auc(now[mm], y[mm]==3):.3f} order {conc(now[mm], y[mm]):.3f} | best Off {auc(-S[best][mm], y[mm]==1):.3f} Add {auc(S[best][mm], y[mm]==3):.3f} order {conc(S[best][mm], y[mm]):.3f}")
keep = lambda s: np.concatenate([(lambda ix: ix[np.argsort(-s[ix])][: len(ix) // 2])(np.where((pid == p) & n)[0]) for p in set(pid)])
for lab, s in (("NOW", now), ("best", S[best])):
    k = np.zeros(len(y), bool); k[keep(s)] = True; dr = n & ~k
    print(f"  top half {lab:5s}: kept Off {100*(y[k]==1).mean():.0f}% Add {100*(y[k]==3).mean():.0f}% ; cut Off {100*(y[dr]==1).mean():.0f}% Add {100*(y[dr]==3).mean():.0f}%; Adds kept {(y[k]==3).sum()}/{(y[n]==3).sum()}")
# independent v3 (no sources/tempo logged: sound part only)
hk3 = {r["play"]: r for r in H3}; V3 = [r for r in V3 if r["play"] in hk3]; y3 = np.array([r["rating"] for r in V3])
nw3 = (f([hk3[r["play"]] for r in V3], "seed_top3") + f([hk3[r["play"]] for r in V3], "like_top3")) / 2
print(f"\nv3 independent ({len(V3)} plays, Adds {(y3==3).sum()}, sound part only): NOW Off {auc(-nw3, y3==1):.3f} Add {auc(nw3, y3==3):.3f} order {conc(nw3, y3):.3f}")
v3rows = []
for k in keys:
    a = np.array([r["maps"].get(k, np.nan) for r in V3])
    if np.isnan(a).all(): continue
    a = np.where(np.isnan(a), 0, a); v3rows.append((k, auc(-a, y3 == 1), auc(a, y3 == 3), conc(a, y3)))
d3 = {x[0]: x for x in v3rows}
print("  the Pi's top 8 on v3:")
for k, *_ in rows[:8]:
    if k in d3: print(f"  {k:62s} Off {d3[k][1]:.3f} Add {d3[k][2]:.3f} order {d3[k][3]:.3f}")
print("  how many variants beat NOW on BOTH the Pi's picks and v3, by order:", sum(1 for x in rows if x[0] in d3 and x[4] > conc(now[n], y[n]) and d3[x[0]][3] > conc(nw3, y3)), "of", len(rows))
both = sorted([(min(x[4] - conc(now[n], y[n]), d3[x[0]][3] - conc(nw3, y3)), x[0], x[2], x[3], x[4], d3[x[0]][1], d3[x[0]][2], d3[x[0]][3]) for x in rows if x[0] in d3], reverse=True)[:8]
for g, k, o, a, c, o3, a3, c3 in both: print(f"  {k:62s} Pi Off {o:.3f} Add {a:.3f} order {c:.3f} | v3 Off {o3:.3f} Add {a3:.3f} order {c3:.3f}")
