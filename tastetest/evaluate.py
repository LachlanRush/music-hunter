"""Step 3: which representation captures my taste? (SPEC.md §16.1)

    uv run python evaluate.py          # writes runs/taste-test/results.md and results.json
    uv run python evaluate.py maest    # only the named feature sets → results-maest.md (quick check)

For every feature set (and every layer of layered models) and two methods — nearest
neighbours (rating of the most similar rated tracks) and ridge regression — predictions
are made out-of-fold with cross-validation grouped by artist (no artist in both training
and test), repeated over several artist splits. Target: rating rank within its run
(v1 1–5 and v2 1–4 on one 0–1 scale); "liked" = v1 ≥ 4, v2 On target/Add.

Scored by: rank correlation with ratings (pooled, and per run), AUC for liked vs not, and
top-N precision. Signal vs noise: the best layer/method of each feature set is re-run on
ratings shuffled within each run (the best layer re-chosen every time, so picking the
best of many layers is paid for), giving a p-value; bootstrap gives 95% ranges.
Also: section analysis (mean vs weakest vs spread of per-section predictions) and the
added value of audio on top of the non-audio facts (sources, release year, BPM).
"""

import json
from dataclasses import dataclass

import numpy as np
from scipy.stats import rankdata, spearmanr
from sklearn.decomposition import PCA
from sklearn.linear_model import RidgeCV
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from common import CFG, FEATURES, OUT, load_dataset, load_features

ALPHAS = np.logspace(-1, 5, 13)


@dataclass
class Data:
    keys: list
    y: np.ndarray        # rank within run, 0–1
    liked: np.ndarray
    run: np.ndarray
    groups: np.ndarray   # artist


def folds(groups: np.ndarray, k: int, seed: int) -> list[np.ndarray]:
    """Test-index sets for k folds that never split an artist."""
    rng = np.random.default_rng(seed)
    uniq = rng.permutation(np.unique(groups))
    assign = {g: i % k for i, g in enumerate(uniq)}
    f = np.array([assign[g] for g in groups])
    return [np.where(f == i)[0] for i in range(k)]


def knn_predict(Xtr, ytr, Xte):
    mu = Xtr.mean(0)
    A, B = Xtr - mu, Xte - mu
    A /= np.linalg.norm(A, axis=1, keepdims=True) + 1e-9
    B /= np.linalg.norm(B, axis=1, keepdims=True) + 1e-9
    sims = B @ A.T
    idx = np.argsort(-sims, axis=1)[:, :CFG["knn_k"]]
    return ytr[idx].mean(1)


def ridge_predict(Xtr, ytr, Xte):
    model = make_pipeline(StandardScaler(), RidgeCV(alphas=ALPHAS))
    return model.fit(Xtr, ytr).predict(Xte)


METHODS = {"knn": knn_predict, "ridge": ridge_predict}


def oof(X, y, groups, method, repeats):
    """Out-of-fold predictions averaged over `repeats` artist-grouped splits."""
    pred = np.zeros(len(y))
    for rep in range(repeats):
        for test in folds(groups, CFG["cv_folds"], seed=rep):
            train = np.setdiff1d(np.arange(len(y)), test)
            pred[test] += METHODS[method](X[train], y[train], X[test])
    return pred / repeats


def score(pred, d: Data) -> dict:
    out = {"spearman": float(spearmanr(pred, d.y)[0])}
    for run in ("v1", "v2"):
        m = d.run == run
        if m.sum() > 5:
            out[f"spearman_{run}"] = float(spearmanr(pred[m], d.y[m])[0])
    out["auc"] = float(roc_auc_score(d.liked, pred)) if 0 < d.liked.sum() < len(d.liked) else None
    n = CFG["top_n"]
    precs = []
    for rep in range(CFG["cv_repeats"]):
        for test in folds(d.groups, CFG["cv_folds"], seed=rep):
            top = test[np.argsort(-pred[test])[:n]]
            precs.append(d.liked[top].mean())
    out["top_n_precision"] = float(np.mean(precs))
    return out


def bootstrap_ci(pred, y, n):
    rng = np.random.default_rng(0)
    vals = []
    for _ in range(n):
        i = rng.integers(0, len(y), len(y))
        vals.append(spearmanr(pred[i], y[i])[0])
    return [float(np.nanpercentile(vals, 2.5)), float(np.nanpercentile(vals, 97.5))]


def shuffled(y, run, rng):
    out = y.copy()
    for r in np.unique(run):
        m = np.where(run == r)[0]
        out[m] = y[rng.permutation(m)]
    return out


def feature_sets(d: Data, only: list[str] | None = None):
    """(name, list of (variant, X)) for every features file, aligned to the dataset order."""
    for f in sorted(FEATURES.glob("*.npz")):
        if only and f.stem not in only:
            continue
        z = load_features(f.stem)
        pos = {k: i for i, k in enumerate(z["keys"])}
        keep = [i for i, k in enumerate(d.keys) if k in pos]
        if len(keep) < 0.8 * len(d.keys):
            print(f"  {f.stem}: only {len(keep)} of {len(d.keys)} tracks — skipped")
            continue
        T = z["track"][[pos[d.keys[i]] for i in keep]].astype(np.float64)
        if T.ndim == 3:   # layered model: one variant per layer
            yield f.stem, keep, [(f"layer {l}", T[:, l]) for l in range(T.shape[1])], z
        else:
            yield f.stem, keep, [("all", T)], z


def subset(d: Data, keep) -> Data:
    keep = np.array(keep)
    return Data([d.keys[i] for i in keep], d.y[keep], d.liked[keep], d.run[keep], d.groups[keep])


def evaluate(only: list[str] | None = None):
    records = load_dataset()
    d = Data([r["key"] for r in records], np.array([r["rank"] for r in records]),
             np.array([r["liked"] for r in records]), np.array([r["run"] for r in records]),
             np.array([r["artist_group"] for r in records]))
    rows, sections = [], []
    rng = np.random.default_rng(1)
    for name, keep, variants, z in feature_sets(d, only):
        ds = subset(d, keep)
        results = []
        for variant, X in variants:
            for method in METHODS:
                pred = oof(X, ds.y, ds.groups, method, CFG["cv_repeats"])
                results.append((variant, method, pred, score(pred, ds)))
        best = max(results, key=lambda r: r[3]["spearman"])
        variant, method, pred, s = best
        # Shuffled baseline: re-pick the best layer for each shuffle (one split each, for speed).
        null = []
        for _ in range(CFG["permutations"]):
            ys = shuffled(ds.y, ds.run, rng)
            null.append(max(spearmanr(oof(X, ys, ds.groups, method, 1), ys)[0] for _, X in variants))
        p = (1 + sum(v >= s["spearman"] for v in null)) / (1 + len(null))
        row = {"features": name, "best": f"{variant} · {method}", "n": len(ds.y), **s,
               "ci": bootstrap_ci(pred, ds.y, CFG["bootstrap"]), "p_shuffled": p,
               "shuffled_95th": float(np.percentile(null, 95)), "variants_tested": len(variants) * len(METHODS)}
        rows.append(row)
        print(f"  {name:15s} best {row['best']:16s} ρ {s['spearman']:+.3f} {row['ci']} p={p:.3f}"
              f" AUC {s['auc'] if s['auc'] is None else round(s['auc'], 3)} top{CFG['top_n']} {s['top_n_precision']:.2f}", flush=True)
        # Interpretable features: which ones track ratings on their own?
        if "names" in z:
            names = list(z["names"])
            X = variants[0][1]
            corr = [(names[j], float(spearmanr(X[:, j], ds.y)[0])) for j in range(X.shape[1])]
            row["top_single_features"] = sorted(corr, key=lambda t: -abs(t[1]))[:8]
        # Section analysis: per-section predictions → mean vs weakest vs spread.
        if "chunks" in z and variant.startswith(("layer", "all")):
            sections.append(section_analysis(name, variant, z, ds))
    if only:   # quick check of a few feature sets: don't touch the full results
        write_markdown(rows, sections, {}, d, OUT / f"results-{'-'.join(only)}.md")
        return
    added = added_value(d, rows)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps({"rows": rows, "sections": sections, "added_value": added}, indent=1))
    write_markdown(rows, sections, added, d)


def section_analysis(name, variant, z, ds: Data) -> dict:
    """Ridge on sections (each section labelled with its track's rank), aggregated per track."""
    layer = int(variant.split()[1]) if variant.startswith("layer") else None
    pos = {k: i for i, k in enumerate(z["keys"])}
    per_track = []
    for k in ds.keys:
        c = z["chunks"][pos[k]].astype(np.float64)
        per_track.append(c[:, layer] if layer is not None else c.reshape(len(c), -1))
    agg = {"mean": np.zeros(len(ds.y)), "weakest": np.zeros(len(ds.y)), "spread": np.zeros(len(ds.y))}
    for test in folds(ds.groups, CFG["cv_folds"], seed=0):
        train = np.setdiff1d(np.arange(len(ds.y)), test)
        Xtr = np.vstack([per_track[i] for i in train])
        ytr = np.concatenate([[ds.y[i]] * len(per_track[i]) for i in train])
        model = make_pipeline(StandardScaler(), RidgeCV(alphas=ALPHAS)).fit(Xtr, ytr)
        for i in test:
            p = model.predict(per_track[i])
            agg["mean"][i], agg["weakest"][i], agg["spread"][i] = p.mean(), p.min(), -p.std()
    out = {"features": name, "variant": variant}
    for k, v in agg.items():
        out[k] = float(spearmanr(v, ds.y)[0])
    print(f"  sections {name} {variant}: mean {out['mean']:+.3f} weakest {out['weakest']:+.3f} spread {out['spread']:+.3f}")
    return out


def added_value(d: Data, rows) -> dict:
    """v2 only (v1 has no release year/BPM): non-audio facts alone vs facts + best audio (PCA in-fold)."""
    records = {r["key"]: r for r in load_dataset()}
    v2 = [i for i, k in enumerate(d.keys) if records[k]["run"] == "v2"]
    ds = subset(d, v2)
    years = np.array([records[k]["release_year"] or np.nan for k in ds.keys], dtype=float)
    years = np.where(np.isnan(years), np.nanmedian(years), years)
    bpm = np.array([records[k]["bpm"] or np.nan for k in ds.keys], dtype=float)
    bpm = np.where(np.isnan(bpm), np.nanmedian(bpm), bpm)
    facts = np.column_stack([[records[k]["exact_sources"] for k in ds.keys], years, -np.abs(bpm - np.median(bpm))])
    base = oof(facts, ds.y, ds.groups, "ridge", CFG["cv_repeats"])
    out = {"facts_only": score(base, ds)}
    audio_rows = [r for r in rows if r["features"] not in ("descriptors", "signal", "stems", "mulan_text", "discogs_styles")]
    for r in sorted(audio_rows, key=lambda r: -r["spearman"])[:3]:
        z = load_features(r["features"])
        pos = {k: i for i, k in enumerate(z["keys"])}
        if not all(k in pos for k in ds.keys):
            continue
        T = z["track"][[pos[k] for k in ds.keys]].astype(np.float64)
        variant = r["best"].split(" · ")[0]
        A = T[:, int(variant.split()[1])] if variant.startswith("layer") else T
        pred = np.zeros(len(ds.y))
        for rep in range(CFG["cv_repeats"]):
            for test in folds(ds.groups, CFG["cv_folds"], seed=rep):
                train = np.setdiff1d(np.arange(len(ds.y)), test)
                pca = PCA(n_components=min(CFG["pca_components"], len(train) - 1)).fit(A[train])
                Xtr = np.column_stack([facts[train], pca.transform(A[train])])
                Xte = np.column_stack([facts[test], pca.transform(A[test])])
                pred[test] += ridge_predict(Xtr, ds.y[train], Xte)
        out[f"facts + {r['features']} ({variant})"] = score(pred / CFG["cv_repeats"], ds)
    for k, v in out.items():
        print(f"  added value {k}: ρ {v['spearman']:+.3f}")
    return out


def write_markdown(rows, sections, added, d: Data, path=None):
    base_rate = d.liked.mean()
    lines = ["# Taste-model test results", "",
             f"{len(d.y)} rated plays (v1 {int((d.run == 'v1').sum())}, v2 {int((d.run == 'v2').sum())}),"
             f" {int(d.liked.sum())} liked (base rate {base_rate:.0%}). Cross-validated by artist"
             f" ({CFG['cv_folds']} folds × {CFG['cv_repeats']} repeats); shuffled baseline {CFG['permutations']}×.", "",
             "| Features | Best layer · method | Rank corr. ρ (95% range) | v1 ρ | v2 ρ | AUC liked | Top-"
             f"{CFG['top_n']} liked | Shuffled 95th pct | p (vs shuffled) |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: -r["spearman"]):
        lines.append(f"| {r['features']} | {r['best']} | {r['spearman']:+.3f} ({r['ci'][0]:+.2f} to {r['ci'][1]:+.2f}) |"
                     f" {r.get('spearman_v1', float('nan')):+.2f} | {r.get('spearman_v2', float('nan')):+.2f} |"
                     f" {r['auc']:.2f} | {r['top_n_precision']:.0%} | {r['shuffled_95th']:+.3f} | {r['p_shuffled']:.3f} |")
    lines += ["", "## Strongest single interpretable features (rank correlation with ratings)", ""]
    for r in rows:
        if r.get("top_single_features"):
            lines.append(f"- **{r['features']}**: " + ", ".join(f"{n} {v:+.2f}" for n, v in r["top_single_features"]))
    lines += ["", "## Section analysis (per-section predictions aggregated per track)", "",
              "| Features | Variant | Mean | Weakest section | Spread (less = better) |", "|---|---|---|---|---|"]
    for s in sections:
        lines.append(f"| {s['features']} | {s['variant']} | {s['mean']:+.3f} | {s['weakest']:+.3f} | {s['spread']:+.3f} |")
    lines += ["", "## Added value on top of sources, release year and BPM (v2 only)", "",
              "| Model | ρ | AUC | Top-N liked |", "|---|---|---|---|"]
    for k, v in added.items():
        lines.append(f"| {k} | {v['spearman']:+.3f} | {v['auc']:.2f} | {v['top_n_precision']:.0%} |")
    path = path or OUT / "results.md"
    path.write_text("\n".join(lines) + "\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    import sys
    evaluate(sys.argv[1:] or None)
