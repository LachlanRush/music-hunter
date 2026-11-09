import pickle, sys
import numpy as np
from scipy.stats import rankdata, spearmanr
R = pickle.load(open(sys.argv[1], "rb"))
y = np.array([r["rating"] for r in R]); pid = np.array([r["pid"] for r in R]); ctl = np.array([r["control"] for r in R], bool)
src = np.array([r["sources"] for r in R]); tem = np.array([r["tempo"] for r in R])
def part(name):
    return np.array([r["parts"].get(name, np.nan) for r in R])
def auc(s, t):
    a, b = s[t], s[~t]
    if not len(a) or not len(b): return np.nan
    x = rankdata(np.r_[a, b]); return (x[:len(a)].sum() - len(a) * (len(a) + 1) / 2) / (len(a) * len(b))
def conc(s, yy):
    d = (yy[:, None] > yy[None, :]); ds = s[:, None] - s[None, :]
    return (((ds > 0) * d).sum() + 0.5 * ((ds == 0) * d).sum()) / max(d.sum(), 1)
def fill(x): return np.where(np.isnan(x), 0.0, x)
seed, like = fill(part("seed_top3")), fill(part("like_top3"))
print("baseline check: my seed part vs logged", round(float(np.corrcoef(seed, fill(np.array([r['log_seed'] for r in R])))[0, 1]), 3),
      "| like", round(float(np.corrcoef(like, fill(np.array([r['log_like'] for r in R])))[0, 1]), 3))
def score(*ps): return np.mean(np.vstack([*ps, src, tem]), axis=0)
base = score(seed, like)
print("baseline vs logged total z", round(float(np.corrcoef(base, fill(np.array([r['log_z'] for r in R])))[0, 1]), 3))
variants = {"NOW nearest 3 seeds + nearest 3 likes": base}
for h in (0.05, 0.08, 0.2):
    variants[f"rating map, own artists left out h={h}"] = score(fill(part(f"ratingmap_noartist_{h}")))
    variants[f"NOW + rating map as extra part h={h}"] = score(seed, like, fill(part(f"ratingmap_{h}")))
HS = (0.03, 0.05, 0.08, 0.12, 0.2)
for h in HS: variants[f"seed heat h={h} + likes as now"] = score(fill(part(f"seedheat_{h}")), like)
for h in HS: variants[f"seed heat + like heat h={h}"] = score(fill(part(f"seedheat_{h}")), fill(part(f"likeheat_{h}")))
for h in HS: variants[f"one map (seeds+likes) h={h}"] = score(fill(part(f"allheat_{h}")))
for h in HS:
    for a in (0.25, 0.5, 1.0): variants[f"one map minus Offs a={a} h={h}"] = score(fill(part(f"signed{a}_{h}")))
for h in HS: variants[f"rating map (Add/Close/Off) h={h}"] = score(fill(part(f"ratingmap_{h}")))
def line(k, s, m):
    return f"  {k:40s} rho {spearmanr(s[m], y[m])[0]:+.3f}  Off {auc(-s[m], y[m]==1):.3f}  Add {auc(s[m], y[m]==3):.3f}  order {conc(s[m], y[m]):.3f}"
normal = ~ctl
print(f"\nnormal picks: {normal.sum()}; Off {100*(y[normal]==1).mean():.0f}% Add {100*(y[normal]==3).mean():.0f}%")
for k, s in variants.items(): print(line(k, s, normal))
print(f"\nall rated (incl. below-gate slipped in): {len(y)}")
order = sorted(variants, key=lambda k: -conc(variants[k][normal], y[normal]))
for k in ["NOW nearest 3 seeds + nearest 3 likes"] + order[:6]: print(line(k, variants[k], np.ones(len(y), bool)))
for p in sorted(set(pid)):
    m = (pid == p) & normal
    print(f"\nplaylist {p}: {m.sum()} picks, Off {100*(y[m]==1).mean():.0f}% Add {100*(y[m]==3).mean():.0f}%")
    for k in ["NOW nearest 3 seeds + nearest 3 likes"] + order[:4]: print(line(k, variants[k], m))
    for nm, s in (("part: seed nearest 3", seed), ("part: likes nearest 3", like), ("part: sources", src), ("part: tempo", tem)):
        print(line(nm, s, m))
rng = np.random.default_rng(3); m = np.where(normal)[0]
print("\nbest variants vs NOW, paired resampling (2000):")
for k in order[:5]:
    d = {"rho": [], "Off": [], "Add": [], "order": []}
    for _ in range(2000):
        i = rng.choice(m, len(m)); a, b, yy = variants[k][i], base[i], y[i]
        d["rho"].append(spearmanr(a, yy)[0] - spearmanr(b, yy)[0]); d["Off"].append(auc(-a, yy==1) - auc(-b, yy==1))
        d["Add"].append(auc(a, yy==3) - auc(b, yy==3)); d["order"].append(conc(a, yy) - conc(b, yy))
    print("  " + k)
    for n, v in d.items(): print(f"     {n:6s} {np.nanmean(v):+.3f} (95% {np.nanpercentile(v,2.5):+.3f} to {np.nanpercentile(v,97.5):+.3f})")
# first half / second half for the best
t = np.array([r["at"] for r in R]); cut = np.sort(t[normal])[normal.sum() // 2]
print("\nearlier vs later half of picks (best chosen on all, so the later half is the fairer read):")
for k in ["NOW nearest 3 seeds + nearest 3 likes"] + order[:3]:
    print(line(k + " early", variants[k], normal & (t <= cut))); print(line(k + " late", variants[k], normal & (t > cut)))

# would the gate (top half of each playlist's picks) keep more Adds and fewer Offs?
print("\ntop half of each playlist's picks by score (what a median-style gate keeps):")
for k in ["NOW nearest 3 seeds + nearest 3 likes", "rating map (Add/Close/Off) h=0.08", "rating map, own artists left out h=0.08", "NOW + rating map as extra part h=0.08"]:
    keep = np.zeros(len(y), bool)
    for p in set(pid):
        m = np.where((pid == p) & normal)[0]; o = m[np.argsort(-variants[k][m])][: len(m) // 2]; keep[o] = True
    drop = normal & ~keep
    print(f"  {k:44s} kept: Off {100*(y[keep]==1).mean():.0f}% Add {100*(y[keep]==3).mean():.0f}% | cut: Off {100*(y[drop]==1).mean():.0f}% Add {100*(y[drop]==3).mean():.0f}% | Adds kept {(y[keep]==3).sum()} of {(y[normal]==3).sum()}")
# ---------- re-weighting only on a clear gain (walk-forward, #30/31) ----------
print("\n=== RE-WEIGHTING: walk-forward on the four parts; weights only change when another set beats them on the last W ratings")
import itertools
P = np.vstack([seed, like, src, tem]).T
GRID = sorted({tuple(np.round(np.array(g) / sum(g), 4)) for g in itertools.product(range(0, 4), repeat=4) if sum(g)})
G = np.array(GRID); EQ = np.array([.25] * 4)
print("candidate weight sets:", len(G))
def walk(mask_all, W, every, margin):
    o = np.where(mask_all)[0]
    cur = EQ.copy(); sc = np.full(len(y), np.nan); switches = []
    for n, i in enumerate(o):
        if n >= W and (n - W) % every == 0:
            win = o[n - W:n]; yy = y[win]; X = P[win] @ G.T
            cs = np.array([conc(X[:, j], yy) for j in range(len(G))])
            c0 = conc(P[win] @ cur, yy); j = int(cs.argmax())
            if cs[j] - c0 >= margin and cs[j] > c0:
                cur = G[j].copy(); switches.append((n, tuple(np.round(cur, 2)), round(cs[j] - c0, 3)))
        sc[i] = P[i] @ cur
    return sc, switches
eq = P @ EQ
for W, margin in itertools.product((30, 50, 75), (0.0, 0.03, 0.05, 0.1)):
    out = []
    tot_new, tot_eq, tot_y = [], [], []
    for p in sorted(set(pid)):
        mk = pid == p
        sc, sw = walk(mk, W, 10, margin)
        o = np.where(mk)[0][W:]
        if len(o) < 5: out.append(f"p{p}: too few"); continue
        out.append(f"p{p} n={len(o)} sw={len(sw)} {conc(sc[o], y[o]):.3f}/{conc(eq[o], y[o]):.3f}")
        tot_new += list(rankdata(sc[o]) / len(o)); tot_eq += list(rankdata(eq[o]) / len(o)); tot_y += list(y[o])
    a, b, yy = map(np.array, (tot_new, tot_eq, tot_y))
    print(f"  W={W} margin={margin}: pooled order {conc(a, yy):.3f} vs equal {conc(b, yy):.3f}  Off {auc(-a, yy==1):.3f}/{auc(-b, yy==1):.3f}  Add {auc(a, yy==3):.3f}/{auc(b, yy==3):.3f} | " + "; ".join(out))
sc, sw = walk(pid == 1, 50, 10, 0.05); print("pump switches W=50 margin=0.05:", sw)
# what the best fixed weights would have been in hindsight, per playlist (flattering by construction)
for p in sorted(set(pid)):
    mk = pid == p; X = P[mk] @ G.T; cs = np.array([conc(X[:, j], y[mk]) for j in range(len(G))]); j = int(cs.argmax())
    print(f"hindsight best p{p}: weights seed/like/sources/tempo {G[j].round(2)} order {cs[j]:.3f} vs equal {conc(P[mk] @ EQ, y[mk]):.3f}")
