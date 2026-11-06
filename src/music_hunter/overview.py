"""A playlist's Find music results (SPEC.md §18.30): one curve — the share of plays rated Off and Add by the track's
veto score when it played, from the lowest-scoring play to the highest, with the gate (0) marked. Plays drawn from a
seed share that was over-full (more than [pool] surplus_factor × its target at the latest pull before the play — the
pool bug fixed in §18.28) are left out. Rates only; nothing names a play."""

import json

import numpy as np

from . import db, find
from .config import CONFIG


def _curve(zs: list[float], ratings: list[int]) -> dict:
    if len(zs) < 2:
        return {"plays": len(zs), "curve": [], "rug": [], "min": 0, "max": 0}
    z, r = np.array(zs), np.array(ratings)
    # From the third-lowest play (a stray one or two far below would stretch the axis), and never right of the gate.
    lo = min(0.0, float(np.floor(np.sort(z)[min(2, len(z) - 1)] * 2) / 2))
    top, bw = float(z.max()), CONFIG["ui"]["results_bandwidth_sd"]
    curve = []
    for x in np.linspace(lo, top, 101):
        w = np.exp(-0.5 * ((z - x) / bw) ** 2)            # nearby plays count most
        n = w.sum()
        curve.append({"x": round(float(x), 3), "off": round(float(100 * w[r == 1].sum() / n), 1),
                      "add": round(float(100 * w[r == 3].sum() / n), 1),
                      "n": round(float(n ** 2 / (w ** 2).sum()), 1)})   # effective number of plays behind the point
    above = z >= 0
    return {"plays": len(zs), "above_gate": int(above.sum()), "min": round(lo, 3), "max": round(top, 3),
            "curve": curve, "rug": sorted(round(float(v), 2) for v in z if v >= lo),
            "off": round(100 * float((r[above] == 1).mean()), 1) if above.any() else None,
            "add": round(100 * float((r[above] == 3).mean()), 1) if above.any() else None}


def overfull_plays(sess) -> set[int]:
    """Plays whose track came from a seed share holding more than surplus_factor × its target at the latest pull
    before the play — what pool.park_surplus now prevents (§18.28)."""
    label = {r[0]: f"{r[1]} – {r[2]}" for r in sess.execute("SELECT id, artist, title FROM tracks")}
    factor = CONFIG["pool"]["surplus_factor"]
    pulls = []
    for at, stats in sess.execute("SELECT coalesce(finished_at, started_at), stats FROM generations ORDER BY id"):
        shares = json.loads(stats or "{}").get("seed_pools", {})
        pulls.append((at, {k for k, v in shares.items() if v.get("target") and v["unplayed"] > factor * v["target"]}))
    out = set()
    for pid, shown, owner in sess.execute(
            "SELECT p.id, p.shown_at, coalesce(c.pool_seed, (SELECT min(seed_track_id) FROM suggestions s"
            " WHERE s.candidate_id = c.id)) FROM plays p JOIN tracks t ON t.id = p.track_id"
            " LEFT JOIN candidates c ON c.id = t.candidate_id"):
        over = set()
        for at, o in pulls:
            if at <= shown:
                over = o
        if label.get(owner) in over:
            out.add(pid)
    return out


def overview(playlist_id: int) -> dict:
    """A playlist's curve: its Find music's rated plays, without those from over-full seed shares."""
    p = db.connect_library().execute("SELECT name FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    zs, ratings, left_out = [], [], 0
    if find.started(playlist_id):
        sess = db.connect(playlist_id)
        skip = overfull_plays(sess)
        for pid, veto, rating in sess.execute(
                "SELECT id, veto, rating FROM plays WHERE rating IS NOT NULL AND veto IS NOT NULL"):
            z = json.loads(veto).get("z")
            if z is None:
                continue
            if pid in skip:
                left_out += 1
                continue
            zs.append(z)
            ratings.append(rating)
    return {"name": p["name"] if p else "", **_curve(zs, ratings), "left_out": left_out,
            "thin_n": CONFIG["ui"]["results_thin_n"]}
