"""Re-weighting replay (#30/31): start from equal weights; after EVERY rating, look back at the last W ratings and move to
another weight set if it would have ordered them better by at least a margin. Each new track is judged by the weights in
force before it was rated. Compared with staying on equal weights, on the same plays."""
import itertools, pickle, sys
import numpy as np
from scipy.stats import rankdata
R = pickle.load(open(sys.argv[1], "rb"))
y = np.array([r["rating"] for r in R]); pid = np.array([r["pid"] for r in R]); ctl = np.array([r["control"] for r in R], bool)
at = np.array([r["at"] for r in R])
def fill(n):
    a = np.array([r["parts"].get(n, np.nan) for r in R]); return np.where(np.isnan(a), 0, a)
P = np.vstack([fill("seed_top3"), fill("like_top3"), [r["sources"] for r in R], [r["tempo"] for r in R]]).T
NAMES = ["seed", "like", "sources", "tempo"]
G = np.array(sorted({tuple(np.round(np.array(g) / sum(g), 4)) for g in itertools.product(range(0, 5), repeat=4) if sum(g)}))
EQ_I = int(np.argmin(np.abs(G - 0.25).sum(1))); EQ = G[EQ_I]
print(len(G), "weight sets; start", dict(zip(NAMES, EQ)))
def concs(X, yy):      # order score of every weight set on these plays (X: plays x sets)
    d = yy[:, None] > yy[None, :]; n = d.sum() or 1
    D = X[:, None, :] - X[None, :, :]
    return ((D > 0)[d].sum(0) + 0.5 * (D == 0)[d].sum(0)) / n
def precompute(o, W):
    """For each position n in o (from W on): order score of all sets on the W ratings before it."""
    return {n: concs(P[o[n - W:n]] @ G.T, y[o[n - W:n]]) for n in range(W, len(o))}
def walk(o, table, W, margin, step=None):
    cur = EQ_I; w = EQ.copy(); hist = np.zeros((len(o), 4)); sw = 0
    for n in range(len(o)):
        if n in table:
            c = table[n]; j = int(c.argmax())
            base = c[cur] if step is None else float(concs((P[o[n - W:n]] @ w)[:, None], y[o[n - W:n]])[0])
            if c[j] - base >= margin and c[j] > base:
                if step is None:
                    cur, w = j, G[j].copy()
                else:
                    w = w + np.clip(G[j] - w, -step, step); w = np.clip(w, 0, None); w = w / w.sum()
                sw += 1
        hist[n] = w
    return hist, sw
def fair(o, hist, W):
    """Judged fairly though the weights move: each new play against the W plays before it, all scored with the weights in
    force just before that play. Returns one row per pair-bearing play: (right pairs, pairs, kept?)."""
    out = []
    for n in range(W, len(o)):
        win = o[n - W:n]; s = P[win] @ hist[n]; me = P[o[n]] @ hist[n]; yy = y[win]; ym = y[o[n]]
        hi, lo = yy > ym, yy < ym
        right = (me > s[lo]).sum() + .5 * (me == s[lo]).sum() + (me < s[hi]).sum() + .5 * (me == s[hi]).sum()
        out.append((right, hi.sum() + lo.sum(), me > np.median(s), ym))
    return np.array(out, float)
def summary(a):
    k = a[:, 2] > 0; yy = a[:, 3]
    return dict(order=a[:, 0].sum() / max(a[:, 1].sum(), 1), off_kept=(yy[k] == 1).mean(), off_cut=(yy[~k] == 1).mean(),
                add_kept=(yy[k] == 3).mean(), add_cut=(yy[~k] == 3).mean(), adds_kept=int(((yy == 3) & k).sum()), adds=int((yy == 3).sum()), n=len(a))
rng = np.random.default_rng(9)
def boot(a, b, N=2000):
    n = len(a); d = []
    for _ in range(N):
        i = rng.integers(0, n, n); d.append(a[i, 0].sum() / max(a[i, 1].sum(), 1) - b[i, 0].sum() / max(b[i, 1].sum(), 1))
    return np.mean(d), *np.percentile(d, [2.5, 97.5])
def run(label, seqs, W, margins, step=None):
    tables = [precompute(o, W) for o in seqs]
    fixed = [np.tile(EQ, (len(o), 1)) for o in seqs]
    eq = np.vstack([fair(o, h, W) for o, h in zip(seqs, fixed) if len(o) > W])
    s = summary(eq)
    print(f"\n{label}, window {W}{' , steps of '+str(step) if step else ''}: {s['n']} plays judged")
    print(f"  equal weights: order {s['order']:.3f} | gate keeps Off {100*s['off_kept']:.0f}% Add {100*s['add_kept']:.0f}%, cuts Off {100*s['off_cut']:.0f}% Add {100*s['add_cut']:.0f}%, Adds kept {s['adds_kept']}/{s['adds']}")
    for m in margins:
        hs = [walk(o, t, W, m, step) for o, t in zip(seqs, tables)]
        a = np.vstack([fair(o, h[0], W) for o, h in zip(seqs, hs) if len(o) > W]); s = summary(a); d, lo, hi = boot(a, eq)
        print(f"  margin {m:<5}: order {s['order']:.3f} ({d:+.3f}, 95% {lo:+.3f} to {hi:+.3f}) changes {sum(h[1] for h in hs):3d} | keeps Off {100*s['off_kept']:.0f}% Add {100*s['add_kept']:.0f}%, cuts Off {100*s['off_cut']:.0f}% Add {100*s['add_cut']:.0f}%, Adds kept {s['adds_kept']}/{s['adds']}")
    return tables
M = (0.0, 0.005, 0.01, 0.02, 0.03, 0.05, 0.075, 0.1)
ord_ = lambda m: np.where(m)[0]
pump = ord_(pid == 1); deeper = ord_(pid == 3); dnb = ord_(pid == 2)
allp = np.argsort(at, kind="stable")
print("\n===== as you asked: after every rating, last 50 =====")
run("pump", [pump], 50, M)
run("deeper", [deeper], 50, M)
run("all playlists sharing one weight set, in time order", [allp], 50, M)
print("\n===== other window lengths, pump =====")
for W in (25, 30, 75, 100):
    run("pump", [pump], W, (0.0, 0.01, 0.03, 0.05, 0.1))
print("\n===== other windows, shared across playlists =====")
for W in (30, 75, 100, 150):
    run("all playlists", [allp], W, (0.0, 0.01, 0.03, 0.05, 0.1))
print("\n===== small steps: move at most 0.05 per weight per update, shared and pump =====")
run("pump", [pump], 50, (0.0, 0.01, 0.03, 0.05), step=0.05)
run("all playlists", [allp], 50, (0.0, 0.01, 0.03, 0.05), step=0.05)
run("all playlists", [allp], 100, (0.0, 0.01, 0.03, 0.05), step=0.02)
# Would any single weight set, fixed in advance, have beaten equal over the later plays? (the ceiling for any adaptive rule)
print("\n===== ceiling: the best FIXED weights for the plays judged, chosen with hindsight =====")
for lab, o in (("pump", pump), ("all", allp)):
    tail = o[50:]; X = P[tail] @ G.T; c = concs(X, y[tail]); j = int(c.argmax())
    print(f"  {lab}: best {dict(zip(NAMES, G[j].round(2)))} order {c[j]:.3f} vs equal {c[EQ_I]:.3f}")
# how stable are the "best" weights? same-window best sets over time
t = precompute(pump, 50); bests = np.array([G[int(c.argmax())] for c in t.values()])
print("\npump: the last-50 best weights over time (mean, spread, and how often each part gets the largest share):")
print("  mean", dict(zip(NAMES, bests.mean(0).round(2))), "spread", dict(zip(NAMES, bests.std(0).round(2))))
print("  largest share", dict(zip(NAMES, np.bincount(bests.argmax(1), minlength=4))), "of", len(bests))
print("  gain over equal on its own window: median", round(float(np.median([c.max() - c[EQ_I] for c in t.values()])), 3), "; on the next 25 plays: median",
      round(float(np.median([concs((P[pump[n:n+25]] @ G[int(c.argmax())])[:, None], y[pump[n:n+25]])[0] - concs((P[pump[n:n+25]] @ EQ)[:, None], y[pump[n:n+25]])[0] for n, c in t.items() if n + 25 <= len(pump)])), 3))
