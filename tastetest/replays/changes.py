import pickle, sys
import numpy as np
from scipy.stats import rankdata, spearmanr
R = pickle.load(open(sys.argv[1], "rb"))
y = np.array([r["rating"] for r in R]); pid = np.array([r["pid"] for r in R]); at = np.array([r["at"] for r in R])
def pct(k, flip=False):
    x = np.array([r["f"].get(k, np.nan) for r in R], float); o = np.full(len(x), .5)
    for p in set(pid):
        m = (pid == p) & ~np.isnan(x); o[m] = (rankdata(x[m]) - 1) / max(1, m.sum() - 1)
    return 1 - o if flip else o
def auc(s, t):
    a, b = s[t], s[~t]; x = rankdata(np.r_[a, b]); return (x[:len(a)].sum() - len(a) * (len(a) + 1) / 2) / (len(a) * len(b))
base = pct("whole score"); ln = pct("track: length"); lf = pct("source: lastfm"); na = pct("credits: number of artists", True); so = pct("sound: 3 seeds minus 3 Offs"); sit = pct("context: position in sitting")
print("how the leads overlap (correlation):")
for a, A, b, B in (("length", ln, "Last.fm", lf), ("length", ln, "score", base), ("Last.fm", lf, "score", base), ("length", ln, "artists", na), ("Last.fm", lf, "artists", na), ("length", ln, "sitting", sit)):
    print(f"   {a:8s} {b:8s} {np.corrcoef(A, B)[0,1]:+.2f}")
V = {"now": base, "+ length": .8 * base + .2 * ln, "+ Last.fm": .8 * base + .2 * lf, "+ one artist": .8 * base + .2 * na, "+ seeds minus Offs": .8 * base + .2 * so,
     "+ length, Last.fm": .67 * base + .165 * ln + .165 * lf, "+ length, Last.fm, seeds-Offs": .58 * base + .14 * ln + .14 * lf + .14 * so}
def keepstat(s, mask):
    k = np.zeros(len(y), bool)
    for p in set(pid):
        i = np.where((pid == p) & mask)[0]; k[i[np.argsort(-s[i])][: len(i) // 2]] = True
    c = mask & ~k
    return f"keeps Off {100*(y[k]==1).mean():.0f}% Add {100*(y[k]==3).mean():.0f}% ({(y[k]==3).sum()} of {(y[mask]==3).sum()} Adds) | cuts Off {100*(y[c]==1).mean():.0f}% Add {100*(y[c]==3).mean():.0f}%"
al = np.ones(len(y), bool); late = at > np.sort(at)[len(at) // 2]
for k, s in V.items():
    print(f"{k:30s} rho {spearmanr(s, y)[0]:+.3f} Off {auc(-s, y==1):.3f} Add {auc(s, y==3):.3f} | {keepstat(s, al)}")
print("later half only (the earlier half alone would have given the same directions):")
for k, s in V.items():
    print(f"{k:30s} rho {spearmanr(s[late], y[late])[0]:+.3f} Off {auc(-s[late], y[late]==1):.3f} Add {auc(s[late], y[late]==3):.3f} | {keepstat(s, late)}")
rng = np.random.default_rng(2); n = len(y)
for k in ("+ length, Last.fm", "+ length, Last.fm, seeds-Offs"):
    d = []
    for _ in range(2000):
        i = rng.integers(0, n, n); yy = y[i]
        d.append((spearmanr(V[k][i], yy)[0] - spearmanr(base[i], yy)[0], auc(-V[k][i], yy==1) - auc(-base[i], yy==1), auc(V[k][i], yy==3) - auc(base[i], yy==3)))
    d = np.array(d); print(k, "vs now:", " ".join(f"{a} {d[:,j].mean():+.3f} ({np.percentile(d[:,j],2.5):+.3f} to {np.percentile(d[:,j],97.5):+.3f})" for j, a in enumerate(("rho", "Off", "Add"))))
print("\nlength within early and late parts of a sitting (is it just sitting?):")
s_ = np.array([r["f"]["context: position in sitting"] for r in R])
for lab, m in (("plays 1-10", s_ <= 10), ("plays 11+", s_ > 10)):
    print(f"   {lab}: n={m.sum()} length rho {spearmanr(ln[m], y[m])[0]:+.2f} Last.fm rho {spearmanr(lf[m], y[m])[0]:+.2f} score rho {spearmanr(base[m], y[m])[0]:+.2f}")
print("Off and Add for the first 10 of a sitting vs later, by score half:")
for lab, m in (("plays 1-10", s_ <= 10), ("plays 11+", s_ > 10)):
    for h, q in (("score top half", base > .5), ("score bottom half", base <= .5)):
        print(f"   {lab} {h}: n={(m&q).sum()} Off {100*(y[m&q]==1).mean():.0f}% Add {100*(y[m&q]==3).mean():.0f}%")
