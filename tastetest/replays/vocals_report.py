import json, pickle, sys
from collections import Counter
import numpy as np
from scipy.stats import rankdata, spearmanr, mannwhitneyu
F = "runs/taste-test/vocals/features"
D = json.load(open("runs/taste-test/vocals/dataset.json")); H = {(r["pid"], r["play"]): r for r in pickle.load(open(sys.argv[1], "rb"))}
st = np.load(f"{F}/stems.npz", allow_pickle=True); de = np.load(f"{F}/descriptors.npz", allow_pickle=True)
fe = {}
for z in (st, de):
    names = [str(n) for n in z["names"]]
    for k, row in zip(z["keys"], z["track"]):
        fe.setdefault(str(k), {}).update(dict(zip(names, map(float, row))))
rows = [dict(r, **{"f": fe[r["video_id"]]}) for r in D if r["video_id"] in fe]
NAME = {1: "pump", 2: "Interesting d&b", 3: "deeper"}
rows = sorted(rows, key=lambda r: (r["pid"], r["play"]))
y = np.array([r["rating"] for r in rows]); pid = np.array([r["pid"] for r in rows]); ctl = np.array([r["control"] for r in rows], bool)
tags = [set(r["tags"]) for r in rows]
vt = np.array([bool({"vocals", "lyrics"} & t) for t in tags]); gv = np.array(["great vocals" in t for t in tags]); anyt = np.array([bool(t) for t in tags])
FN = list(rows[0]["f"]); X = {n: np.array([r["f"][n] for r in rows]) for n in FN}
vs = X["vocal_share_mean"]; inst = X["voice_instrumental:instrumental"]; fem = X["gender:female"]; sec = X["vocal_sections_share"]
def auc(s, t):
    a, b = s[t], s[~t]
    if len(a) < 2 or len(b) < 2: return np.nan
    x = rankdata(np.r_[a, b]); return (x[:len(a)].sum() - len(a) * (len(a) + 1) / 2) / (len(a) * len(b))
def p_mw(s, t):
    return mannwhitneyu(s[t], s[~t]).pvalue if t.sum() > 1 and (~t).sum() > 1 else np.nan
print(f"{len(rows)} rated plays with audio features; pump {(pid==1).sum()}, d&b {(pid==2).sum()}, deeper {(pid==3).sum()}")
print("\nvocal share (Demucs, share of the track's energy): median by playlist:", {NAME[p]: round(float(np.median(vs[pid == p])), 3) for p in NAME}, "| Essentia voice prob median", {NAME[p]: round(float(np.median(1 - inst[pid == p])), 3) for p in NAME})
print("agreement Demucs vs Essentia voice: rho", round(spearmanr(vs, 1 - inst)[0], 3))
print("\n=== A. Do the 'vocals' tags line up with something measured? (Off/Close plays that have any tag: tagged vocals vs other tags)")
for lab, m in [("all", np.ones(len(y), bool))] + [(NAME[p], pid == p) for p in NAME]:
    u = m & (y < 3) & anyt
    print(f" {lab:16s} tagged {vt[u].sum():3d} vs other tags {(~vt[u]).sum():3d}: vocal share {np.median(vs[u&vt]):.3f} vs {np.median(vs[u&~vt]):.3f} (AUC {auc(vs[u], vt[u]):.3f}, p {p_mw(vs[u], vt[u]):.3f}) | voice prob AUC {auc(1-inst[u], vt[u]):.3f} (p {p_mw(1-inst[u], vt[u]):.3f}) | female AUC {auc(fem[u], vt[u]):.3f} (p {p_mw(fem[u], vt[u]):.3f})")
print(" 'vocals' tagged share by vocal-share third (Off/Close with any tag):")
u = (y < 3) & anyt; q = np.quantile(vs[u], [1/3, 2/3])
for lab, mm in (("low", vs <= q[0]), ("mid", (vs > q[0]) & (vs <= q[1])), ("high", vs > q[1])):
    print(f"   {lab:4s} ({mm[u].sum():3d}) vocals tag {100*vt[u&mm].mean():.0f}%")
print(f" Adds: 'great vocals' {gv.sum()} vs other Adds {((y==3)&~gv).sum()}: vocal share {np.median(vs[gv]):.3f} vs {np.median(vs[(y==3)&~gv]):.3f}; voice prob {np.median(1-inst[gv]):.3f} vs {np.median(1-inst[(y==3)&~gv]):.3f}")
print("\n=== B. Does the amount of vocals track ratings? (normal picks)")
n = ~ctl
for lab, m in [("all", n)] + [(NAME[p], n & (pid == p)) for p in NAME]:
    print(f" {lab:16s} picks {m.sum():3d} | vocal share rho {spearmanr(vs[m], y[m])[0]:+.3f} (p {spearmanr(vs[m], y[m])[1]:.3f}) Off {auc(vs[m], y[m]==1):.3f} Add {auc(vs[m], y[m]==3):.3f} | voice prob rho {spearmanr(1-inst[m], y[m])[0]:+.3f} | female rho {spearmanr(fem[m], y[m])[0]:+.3f}")
    qq = np.quantile(vs[m], [1/3, 2/3])
    print("     thirds by vocal share:", "; ".join(f"{l} ({mm.sum()}): Off {100*(y[mm]==1).mean():.0f}% Add {100*(y[mm]==3).mean():.0f}%" for l, mm in (("low", m & (vs <= qq[0])), ("mid", m & (vs > qq[0]) & (vs <= qq[1])), ("high", m & (vs > qq[1])))))
print("\n=== C. Every measure vs ratings, by playlist (rho; * p<.05 — 22 measures × 3 playlists, about 3 expected by chance)")
for fn in FN:
    print(f" {fn:34s}" + "  ".join(f"{NAME[p][:6]} {spearmanr(X[fn][n&(pid==p)], y[n&(pid==p)])[0]:+.2f}{'*' if spearmanr(X[fn][n&(pid==p)], y[n&(pid==p)])[1] < .05 else ' '}" for p in NAME))
print("\n=== D. Would it add anything to the score? Point in time: at each play, learn from that playlist's earlier ratings which way each measure leans (shrunk), and add it as one more part")
Rec = [H.get((r["pid"], r["play"])) for r in rows]
ok = np.array([x is not None for x in Rec])
def part(x, nm):
    v = x["parts"].get(nm, np.nan); return 0.0 if np.isnan(v) else v
now = np.array([np.mean([part(x, "seed_top3"), part(x, "like_top3"), x["sources"], x["tempo"]]) if x else np.nan for x in Rec])
val = (y - 2).astype(float)
def learned(fn, shrink=30, min_n=20):
    out = np.zeros(len(y)); seen = np.zeros(len(y), bool)
    for p in NAME:
        idx = np.where((pid == p) & ok)[0]
        for k, i in enumerate(idx):
            if k < min_n: continue
            e = idx[:k]; xe = X[fn][e]; s = xe.std()
            if s < 1e-9: continue
            r = spearmanr(xe, val[e])[0]
            if np.isnan(r): continue
            out[i] = (r * k / (k + shrink)) * (X[fn][i] - np.median(xe)) / (1.4826 * np.median(np.abs(xe - np.median(xe))) or s); seen[i] = True
    return out, seen
def conc(s, yy):
    d = yy[:, None] > yy[None, :]; ds = s[:, None] - s[None, :]
    return (((ds > 0) & d).sum() + .5 * ((ds == 0) & d).sum()) / max(d.sum(), 1)
rng = np.random.default_rng(2)
for lab, fns in (("vocal share", ["vocal_share_mean"]), ("voice probability", ["voice_instrumental:instrumental"]), ("female vocal", ["gender:female"]), ("vocal share + female", ["vocal_share_mean", "gender:female"]), ("sections with vocals", ["vocal_sections_share"]), ("all 3", ["vocal_share_mean", "voice_instrumental:instrumental", "gender:female"])):
    parts = [learned(fn) for fn in fns]; add = np.sum([a for a, _ in parts], axis=0); seen = parts[0][1]
    m = seen & n & ok
    for wt in (0.25, 0.5):
        sc = now + wt * add
        d = [conc(sc[i], y[i]) - conc(now[i], y[i]) for i in (rng.choice(np.where(m)[0], m.sum()) for _ in range(800))]
        print(f"  {lab:24s} weight {wt}: picks {m.sum()} order {conc(now[m], y[m]):.3f} → {conc(sc[m], y[m]):.3f} ({np.mean(d):+.3f}, 95% {np.percentile(d,2.5):+.3f} to {np.percentile(d,97.5):+.3f}) Off {auc(-now[m], y[m]==1):.3f} → {auc(-sc[m], y[m]==1):.3f} Add {auc(now[m], y[m]==3):.3f} → {auc(sc[m], y[m]==3):.3f}")
    for p in NAME:
        mm = m & (pid == p); sc = now + 0.5 * add
        if mm.sum() > 5: print(f"      {NAME[p]:16s} {mm.sum():3d}: order {conc(now[mm], y[mm]):.3f} → {conc(sc[mm], y[mm]):.3f}; Off {auc(-now[mm], y[mm]==1):.3f} → {auc(-sc[mm], y[mm]==1):.3f}; Add {auc(now[mm], y[mm]==3):.3f} → {auc(sc[mm], y[mm]==3):.3f}")
print("\n=== E. The tags themselves")
cnt = {1: Counter(), 2: Counter(), 3: Counter()}
for t, r in zip(tags, y):
    cnt[int(r)].update(t or {"(no tag)"})
for r, lab in ((1, "Off"), (2, "Close"), (3, "Add")): print(f" {lab}: {dict(cnt[r].most_common())}")
print(" by playlist, share of Off/Close tagged vocals:", {NAME[p]: f"{vt[(pid==p)&(y<3)].sum()}/{((pid==p)&(y<3)).sum()}" for p in NAME})
print(" does the score already separate each reason? score of plays with the tag vs other Off/Close plays (AUC; below .5 = lower score)")
for tg in ("vocals", "lyrics", "wrong energy", "harsh sound", "boring", "wrong genre", "bassline"):
    has = np.array([tg in t for t in tags]); u = (y < 3) & ok & anyt
    if has[u].sum() > 4: print(f"  {tg:14s} {has[u].sum():3d}: score {auc(now[u], has[u]):.3f} (p {p_mw(now[u], has[u]):.3f}); seed part {auc(np.array([part(x,'seed_top3') if x else 0 for x in Rec])[u], has[u]):.3f}; like part {auc(np.array([part(x,'like_top3') if x else 0 for x in Rec])[u], has[u]):.3f}; vocal share {auc(vs[u], has[u]):.3f}")
print(" tags by tempo/energy measures: 'wrong energy' vs other tags")
has = np.array(["wrong energy" in t for t in tags]); u = (y < 3) & anyt
for fn in ("deam:arousal", "emomusic:arousal", "danceability:danceable", "mood_aggressive:aggressive", "timbre:bright"):
    print(f"   {fn:28s} AUC {auc(X[fn][u], has[u]):.3f} (p {p_mw(X[fn][u], has[u]):.3f})")
