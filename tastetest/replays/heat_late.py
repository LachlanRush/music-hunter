import pickle, sys
import numpy as np
from scipy.stats import rankdata, spearmanr
R = pickle.load(open(sys.argv[1], "rb"))
y = np.array([r["rating"] for r in R]); ctl = np.array([r["control"] for r in R], bool); pid = np.array([r["pid"] for r in R])
t = np.array([r["at"] for r in R]); src = np.array([r["sources"] for r in R]); tem = np.array([r["tempo"] for r in R])
def p(n):
    a = np.array([r["parts"].get(n, np.nan) for r in R]); return np.where(np.isnan(a), 0, a)
def auc(s, u):
    a, b = s[u], s[~u]; x = rankdata(np.r_[a, b]); return (x[:len(a)].sum() - len(a)*(len(a)+1)/2) / (len(a)*len(b))
n = ~ctl; cut = np.sort(t[n])[n.sum()//2]
now = np.mean(np.vstack([p("seed_top3"), p("like_top3"), src, tem]), 0); rm = np.mean(np.vstack([p("ratingmap_0.08"), src, tem]), 0)
rna = np.mean(np.vstack([p("ratingmap_noartist_0.08"), src, tem]), 0)
for lab, m in (("earlier half", n & (t <= cut)), ("later half", n & (t > cut))):
    print(lab, "picks", m.sum(), "Adds", (y[m]==3).sum(), "Offs", (y[m]==1).sum())
    for k, s in (("now", now), ("rating map", rm), ("rating map, own artists out", rna)):
        print(f"   {k:30s} rho {spearmanr(s[m], y[m])[0]:+.3f} Off {auc(-s[m], y[m]==1):.3f} Add {auc(s[m], y[m]==3):.3f}")
# shuffled baseline for the Add gain: shuffle which picks were Adds within each playlist
rng = np.random.default_rng(5); m = np.where(n)[0]; real = auc(rm[m], y[m]==3) - auc(now[m], y[m]==3); cnt = 0; N = 5000
for _ in range(N):
    yy = y.copy()
    for q in set(pid):
        i = m[pid[m]==q]; yy[i] = rng.permutation(y[i])
    cnt += (auc(rm[m], yy[m]==3) - auc(now[m], yy[m]==3)) >= real
print(f"Add gain {real:+.3f}; shuffled ratings reach it in {100*cnt/N:.1f}% of draws (one variant; 40 tried in all)")
