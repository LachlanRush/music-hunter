"""Results across every playlist's Find music (SPEC.md §18.30): one curve — the share of plays rated Off and Add by
the track's veto score when it played, from the gate (0) up to the highest-scoring play. Rates only; nothing names a
play."""

import json

import numpy as np

from . import db, find, library
from .config import CONFIG


def overview() -> dict:
    zs, ratings = [], []
    lib = db.connect_library()
    for p in library.list_playlists(lib):
        if not find.started(p["id"]):
            continue
        for veto, rating in db.connect(p["id"]).execute(
                "SELECT veto, rating FROM plays WHERE rating IS NOT NULL AND veto IS NOT NULL"):
            z = json.loads(veto).get("z")
            if z is not None and z >= 0:
                zs.append(z)
                ratings.append(rating)
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
            "thin_n": CONFIG["ui"]["results_thin_n"], "bandwidth": bw,
            "off": round(100 * float((r == 1).mean()), 1), "add": round(100 * float((r == 3).mean()), 1)}
