"""Step 4: do several models together beat any one of them? (SPEC.md §16.1)

    uv run python ensemble.py

Like agreement between sources, combine models' out-of-fold predictions (artist-grouped,
as in evaluate.py): average their predicted ranks, or count "votes" (how many models put a
track in their top third). Combinations were chosen after seeing the single-model results,
so treat their p-values as slightly optimistic.
"""

import numpy as np
from scipy.stats import rankdata, spearmanr

from common import CFG, OUT, load_dataset, load_features
from evaluate import Data, oof, score, shuffled

MODELS = {   # name: (features file, layer or None, method) — best configurations from evaluate.py
    "mulan_text": ("mulan_text", None, "ridge"),
    "mulan": ("mulan", None, "ridge"),
    "mert": ("mert", 11, "ridge"),
    "muq": ("muq", 4, "ridge"),
    "effnet": ("effnet", None, "ridge"),
    "descriptors": ("descriptors", None, "ridge"),
    "styles": ("discogs_styles", None, "ridge"),
}
COMBOS = {
    "top 3 (MuLan text, MERT, MuLan audio)": ["mulan_text", "mert", "mulan"],
    "top 3, different families (MuLan text, MuQ, EffNet)": ["mulan_text", "muq", "effnet"],
    "MuQ-MuLan only (text + audio)": ["mulan_text", "mulan"],
    "all 7 that beat chance": list(MODELS),
}


def features(name, layer, keys):
    z = load_features(name)
    pos = {k: i for i, k in enumerate(z["keys"])}
    T = z["track"][[pos[k] for k in keys]].astype(np.float64)
    return T[:, layer] if layer is not None else T


def main():
    recs = load_dataset()
    d = Data([r["key"] for r in recs], np.array([r["rank"] for r in recs]), np.array([r["liked"] for r in recs]),
             np.array([r["run"] for r in recs]), np.array([r["artist_group"] for r in recs]))
    X = {m: features(f, l, d.keys) for m, (f, l, _) in MODELS.items()}
    preds = {m: oof(X[m], d.y, d.groups, MODELS[m][2], CFG["cv_repeats"]) for m in MODELS}
    rows = []
    for m, p in preds.items():
        rows.append((f"single: {m}", score(p, d), None))
    rng = np.random.default_rng(7)
    for label, members in COMBOS.items():
        avg = np.mean([rankdata(preds[m]) for m in members], axis=0)
        votes = np.sum([rankdata(preds[m]) > len(d.y) * 2 / 3 for m in members], axis=0) + 1e-3 * avg   # ties by avg
        null = []
        for _ in range(100):
            ys = shuffled(d.y, d.run, rng)
            null.append(spearmanr(np.mean([rankdata(oof(X[m], ys, d.groups, MODELS[m][2], 1)) for m in members],
                                          axis=0), ys)[0])
        s_avg = score(avg, d)
        p = (1 + sum(v >= s_avg["spearman"] for v in null)) / 101
        rows.append((f"average: {label}", s_avg, p))
        rows.append((f"votes: {label}", score(votes, d), None))
    # With the non-audio facts (v2 only: sources, release year, BPM)
    v2 = d.run == "v2"
    byk = {r["key"]: r for r in recs}
    keys2 = [k for k, m in zip(d.keys, v2) if m]
    yrs = np.array([byk[k]["release_year"] or np.nan for k in keys2], float)
    bpm = np.array([byk[k]["bpm"] or np.nan for k in keys2], float)
    facts = np.column_stack([[byk[k]["exact_sources"] for k in keys2], np.where(np.isnan(yrs), np.nanmedian(yrs), yrs),
                             -np.abs(np.where(np.isnan(bpm), np.nanmedian(bpm), bpm) - np.nanmedian(bpm))])
    d2 = Data(keys2, d.y[v2], d.liked[v2], d.run[v2], d.groups[v2])
    fp = oof(facts, d2.y, d2.groups, "ridge", CFG["cv_repeats"])
    rows.append(("v2: facts only (sources, year, BPM)", score(fp, d2), None))
    for label, members in (("MuQ-MuLan", ["mulan_text", "mulan"]), ("top 3", COMBOS["top 3 (MuLan text, MERT, MuLan audio)"])):
        audio = np.mean([rankdata(preds[m][v2]) for m in members], axis=0)
        rows.append((f"v2: facts + {label} (average rank)", score((rankdata(fp) + audio) / 2, d2), None))

    lines = ["# Combining models (out-of-fold predictions, artist-grouped)", "",
             f"Base rate liked: {d.liked.mean():.0%} (all), {d2.liked.mean():.0%} (v2).", "",
             "| Model / combination | ρ | v1 ρ | v2 ρ | AUC liked | Top-10 liked | p (vs shuffled) |", "|---|---|---|---|---|---|---|"]
    for label, s, p in rows:
        lines.append(f"| {label} | {s['spearman']:+.3f} | {s.get('spearman_v1', float('nan')):+.2f} |"
                     f" {s.get('spearman_v2', float('nan')):+.2f} | {s['auc']:.2f} | {s['top_n_precision']:.0%} |"
                     f" {'' if p is None else f'{p:.3f}'} |")
    (OUT / "ensemble.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
