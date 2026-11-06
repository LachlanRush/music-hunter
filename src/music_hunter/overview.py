"""A playlist's Find music results (SPEC.md §18.30): one curve — the share of plays rated Off and Add by
the track's veto score when it played, from the gate (0) up to the highest-scoring play. Rates only; nothing names a
play."""

import json

import numpy as np

from . import db, find
from .config import CONFIG


def _curve(zs: list[float], ratings: list[int]) -> dict:
    if len(zs) < 2:
        return {"plays": len(zs), "curve": [], "rug": [], "max": 0}
    z, r = np.array(zs), np.array(ratings)
    top, bw = float(z.max()), CONFIG["ui"]["results_bandwidth_sd"]
    curve = []
    for x in np.linspace(0, top, 81):
        w = np.exp(-0.5 * ((z - x) / bw) ** 2)            # nearby plays count most
        n = w.sum()
        curve.append({"x": round(float(x), 3), "off": round(float(100 * w[r == 1].sum() / n), 1),
                      "add": round(float(100 * w[r == 3].sum() / n), 1),
                      "n": round(float(n ** 2 / (w ** 2).sum()), 1)})   # effective number of plays behind the point
    return {"plays": len(zs), "max": round(top, 3), "curve": curve, "rug": sorted(round(float(v), 2) for v in z),
            "off": round(100 * float((r == 1).mean()), 1), "add": round(100 * float((r == 3).mean()), 1)}


def overview(playlist_id: int) -> dict:
    """A playlist's curve: its Find music's rated plays above the gate."""
    p = db.connect_library().execute("SELECT name FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    zs, ratings = [], []
    if find.started(playlist_id):
        for veto, rating in db.connect(playlist_id).execute(
                "SELECT veto, rating FROM plays WHERE rating IS NOT NULL AND veto IS NOT NULL"):
            z = json.loads(veto).get("z")
            if z is not None and z >= 0:
                zs.append(z)
                ratings.append(rating)
    return {"name": p["name"] if p else "", **_curve(zs, ratings), "thin_n": CONFIG["ui"]["results_thin_n"]}
