"""Would another audio model have ranked v3 better? (after the first v3 run, 2026-10-30)

    TASTE_RUN= uv run python v3check.py

1. Transfer: each model's taste model trained on all v1+v2 ratings (rank within run), then asked to predict the
   v3 ratings it never saw — rank correlation, AUC for Add, and top-10 precision, against shuffled-rating runs.
2. Replay of "sounds like my likes": for each v3 play, closeness (top-3 cosine, Close 0.5 / Add 2) to the tracks
   liked earlier in v3 — as the player's audio_match did with MAEST.
For reference: how the player's own logged score tracked the v3 ratings.
Writes runs/taste-test/v3check.md.
"""

import json
import sqlite3

import numpy as np
from scipy.stats import spearmanr
from sklearn.linear_model import RidgeCV
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from common import OUT, ROOT, load_dataset, load_features

MODELS = {   # name: (features file, layer index or None) — best configurations from the v1/v2 test
    "MAEST (used in v3)": ("maest_gpu", 8),
    "MERT-v2 30 s": ("mert2_30s", 8),
    "MERT-v2 full song": ("mert2_full", 8),
    "MuQ-MuLan descriptions": ("mulan_text", None),
    "MuQ-MuLan audio": ("mulan", None),
    "MuQ": ("muq", 4),
    "MERT v1": ("mert", 11),
    "Discogs-EffNet": ("effnet", None),
    "Essentia descriptors": ("descriptors", None),
    "CLAP (v1/v2)": ("clap", None),
}
SHUFFLES = 200
rng = np.random.default_rng(3)


def features(name, layer, keys):
    z = load_features(name)
    pos = {k: i for i, k in enumerate(z["keys"])}
    missing = [k for k in keys if k not in pos]
    if missing:
        return None
    T = z["track"][[pos[k] for k in keys]].astype(np.float64)
    return T[:, layer] if layer is not None else T


def fit_predict(Xtr, ytr, Xte):
    model = make_pipeline(StandardScaler(), RidgeCV(alphas=np.logspace(-1, 5, 13)))
    return model.fit(Xtr, ytr).predict(Xte)


def top_precision(pred, liked, n=10):
    return float(np.mean(liked[np.argsort(-pred)[:n]]))


def main():
    recs = load_dataset()
    train = [r for r in recs if r["run"] in ("v1", "v2")]
    test = sorted([r for r in recs if r["run"] == "v3"], key=lambda r: r["play_number"])
    y_tr = np.array([r["rank"] for r in train])
    y_te = np.array([r["rating"] for r in test], float)
    add = (y_te == 3).astype(int)
    print(f"train {len(train)} (v1+v2), test {len(test)} (v3); v3 Add rate {add.mean():.0%}")

    rows = []
    for label, (name, layer) in MODELS.items():
        Xtr = features(name, layer, [r["key"] for r in train])
        Xte = features(name, layer, [r["key"] for r in test])
        if Xtr is None or Xte is None:
            rows.append((label, None))
            print(f"  {label}: features missing")
            continue
        pred = fit_predict(Xtr, y_tr, Xte)
        rho = spearmanr(pred, y_te)[0]
        null = [spearmanr(fit_predict(Xtr, rng.permutation(y_tr), Xte), y_te)[0] for _ in range(SHUFFLES)]
        p = (1 + sum(v >= rho for v in null)) / (SHUFFLES + 1)
        # Replay of "sounds like my likes" within v3 (normalised track vectors, earlier likes only)
        V = Xte / np.linalg.norm(Xte, axis=1, keepdims=True)
        rep, rep_y = [], []
        for i in range(len(test)):
            liked = [j for j in range(i) if y_te[j] >= 2]
            if len(liked) < 3:
                continue
            w = np.array([2.0 if y_te[j] == 3 else 0.5 for j in liked])
            s = V[liked] @ V[i]
            k = 3 if len(liked) >= 5 else 1
            top = np.argsort(-s)[:k]
            rep.append((s[top] * w[top]).sum() / w[top].sum())
            rep_y.append(y_te[i])
        rows.append((label, {"rho": rho, "p": p, "auc": roc_auc_score(add, pred), "top10": top_precision(pred, add),
                             "replay": spearmanr(rep, rep_y)[0], "replay_n": len(rep)}))
        print(f"  {label:24s} transfer ρ {rho:+.2f} (p {p:.3f}) AUC {rows[-1][1]['auc']:.2f}"
              f" top-10 {rows[-1][1]['top10']:.0%} · replay ρ {rows[-1][1]['replay']:+.2f}")

    # The player's own logged score, for reference
    c = sqlite3.connect(ROOT / "data" / "music_hunter.db")
    logged = c.execute("SELECT score, rating FROM plays WHERE rating IS NOT NULL").fetchall()
    own = spearmanr([s for s, _ in logged], [r for _, r in logged])[0]

    lines = ["# Would another model have ranked v3 better?", "",
             f"Trained on v1+v2 ({len(train)} ratings), tested on the {len(test)} v3 ratings it never saw."
             f" v3 Add rate {add.mean():.0%} (top-10 precision at chance ≈ that).", "",
             f"The player's own logged score vs v3 rating: ρ {own:+.2f}.", "",
             "| Model | Transfer ρ (v1+v2 → v3) | p (vs shuffled) | AUC Add | Top-10 Add | Replay ρ (likes so far) |",
             "|---|---|---|---|---|---|"]
    for label, m in rows:
        if m is None:
            lines.append(f"| {label} | – | – | – | – | – |")
        else:
            lines.append(f"| {label} | {m['rho']:+.2f} | {m['p']:.3f} | {m['auc']:.2f} | {m['top10']:.0%} |"
                         f" {m['replay']:+.2f} (n={m['replay_n']}) |")
    (OUT / "v3check.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
