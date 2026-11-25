"""Group picks: a share of Find music's picks chosen to find Adds (SPEC.md §20.9, 2026-11-25, my idea of a grid).

Rows are attributes and columns their buckets; every rated song in the playlist's folder (lane songs included: the
lanes build this data) passes through one bucket per row. A bucket's Add chance is its Add rate relative to the
overall rate, judged against what each song's other buckets already predict (an Off that passed through one good
bucket, but whose other buckets were all bad, hardly counts against the good one), and shrunk towards it while few
songs have passed through ([groups] shrink). Rows: length; approachable,
happy, vocal share, bright (the vocal analysis); artist rated before; closeness to seeds and to likes; number of
sources; and the veto score in its 0.5 SD bands. A song's grid value is the sum of its buckets' log lifts; to it is added
how its 10 most similar rated songs went (Add rate − Off rate). Both are standardised over the playable songs, and
the group pick is a random song from the top [groups] top_share of them.

Chosen on a replay (2026-11-25, 471 random picks, each scored only from earlier ratings): Add-AUC 0.68, its top 10%
of random picks Add 21–23% / Off 23–27% (random 8% / 36%, lanes 14% / 24%). Each pick records its group score
(plays.groups) and group picks are marked (plays.picked_by = 'groups'), so Results compares them with the lanes and
random picks.
"""

import json
import sqlite3
import time

import numpy as np

from . import db, names
from .config import CONFIG

CFG = CONFIG.get("groups", {})
ROWS = ("length", "approachable", "happy", "vocal share", "bright", "artist known", "seed closeness",
        "like closeness", "sources", "score")
Z_EDGES = (0, 0.5, 1, 1.5, 2)
ADD, OFF = 3, 1


def enabled(conn: sqlite3.Connection) -> bool:
    from . import lanes
    return bool(CFG.get("enabled", False)) and lanes.enabled(conn)


def _sessions(pid: int | None) -> list:
    """This playlist and the others in its folder that have Find music."""
    from . import find
    if pid is None:
        return [None]
    lib = db.connect_library()
    row = lib.execute("SELECT folder_id FROM playlists WHERE id = ?", (pid,)).fetchone()
    pids = [pid]
    if row and row[0] is not None:
        pids += [p for p, in lib.execute("SELECT id FROM playlists WHERE folder_id = ? AND id != ?", (row[0], pid))
                 if find.started(p)]
    return pids


def _vocals(conn) -> dict:
    out = {}
    for (d,) in conn.execute("SELECT data FROM events WHERE kind = 'vocals'"):
        d = json.loads(d)
        out[d.get("video_id")] = d.get("reason", {})
    return out


def _artists(text: str) -> frozenset:
    return frozenset(names.fold(a) for a in names._ARTIST_SPLIT.split(names._BRACKETS.sub("", text or "")) if a.strip())


def _attrs(veto_json, duration, voc) -> dict:
    v = json.loads(veto_json) if veto_json else {}
    zc, raw = v.get("z_components", {}), v.get("raw", {})
    return {"length": duration, "approachable": voc.get("approachability"), "happy": voc.get("mood_happy:happy"),
            "vocal share": voc.get("vocal_share_mean"), "bright": voc.get("timbre:bright"),
            "seed closeness": zc.get("seed_closeness"), "like closeness": zc.get("like_closeness"),
            "sources": raw.get("sources"), "score": v.get("z")}


def _training(conn: sqlite3.Connection, pid: int | None) -> list[dict]:
    """Every rated pick in the folder (not the checks below the gate), oldest first, as it was when it played."""
    rows = []
    for p in _sessions(pid):
        s = conn if p == pid else db.connect(p)
        try:
            voc = _vocals(s)
            emb = {t: db.from_blob(b) for t, b in s.execute(
                "SELECT e.track_id, e.embedding FROM track_embeddings e JOIN plays p ON p.track_id = e.track_id"
                " WHERE p.rating IS NOT NULL")}
            for r in s.execute("SELECT p.track_id, p.rating, p.rated_at, p.veto, t.artist, t.duration_s, t.video_id"
                               " FROM plays p JOIN tracks t ON t.id = p.track_id WHERE p.rating IS NOT NULL"
                               " AND p.is_control = 0"):
                e = emb.get(r["track_id"])
                if e is None:
                    continue
                rows.append({"at": r["rated_at"] or "", "r": r["rating"], "e": e / (np.linalg.norm(e) or 1),
                             "arts": _artists(r["artist"]),
                             "a": _attrs(r["veto"], r["duration_s"], voc.get(r["video_id"], {}))})
        finally:
            if p != pid:
                s.close()
    rows.sort(key=lambda r: r["at"])
    return rows


def _similar(E_train, y_train, e, k=10) -> float | None:
    """Add rate − Off rate of the k most similar rated songs."""
    if len(y_train) < k:
        return None
    nn = np.argsort(-(E_train @ e))[:k]
    return float((y_train[nn] == ADD).mean() - (y_train[nn] == OFF).mean())


def _bucket(row: str, x, edges: dict) -> str:
    if x is None or (row not in edges and row != "score" and not isinstance(x, bool)):
        return "?"                                     # (unknown, or nothing to compare it with yet)
    if isinstance(x, bool):
        return str(x)
    if row == "score":
        return str(int(np.searchsorted(Z_EDGES, x)))
    return str(int(np.searchsorted(edges[row], x)))


def _explained(buckets: dict, add: np.ndarray, base: float, m: float, rounds: int = 8) -> dict:
    """Each bucket's lift, judged against what its songs' other buckets already predict (my note, 2026-11-25): an Off
    that passed through one good bucket but whose other buckets were bad was expected anyway, so it hardly counts
    against the good one; likewise an Add the other buckets already explain hardly raises it. Fitted in turn, row by
    row (backfitting), each lift shrunk towards 1 while little has passed through. Replayed: Add-AUC 0.666 → 0.684."""
    lift = {row: {} for row in buckets}
    L = {row: np.ones(len(add)) for row in buckets}
    for _ in range(rounds):
        for row, col in buckets.items():
            others = base * np.prod([L[j] for j in buckets if j != row], axis=0)
            for b in set(col):
                mk = col == b
                lift[row][b] = (add[mk].sum() + m * base) / (others[mk].sum() + m * base)
            L[row] = np.array([lift[row][b] for b in col])
    return lift


def update(conn: sqlite3.Connection) -> dict:
    """Score every playable song and keep the result (setting group_scores) for the picks. Cheap enough to run after
    every rating (~0.2 s on the PC for ~900 folder ratings)."""
    from .ratings import playable_ids

    pid = db.SESSION.get()
    if not enabled(conn):
        return {}
    t0 = time.monotonic()
    train = _training(conn, pid)
    y = np.array([t["r"] for t in train])
    if len(train) < CFG.get("min_ratings", 30) or (y == ADD).sum() < CFG.get("min_adds", 3):
        db.set_setting(conn, "group_scores", None, log=False)
        return {}
    E = np.stack([t["e"] for t in train])
    # Artist known: rated before (for a training song, before it; for a candidate, ever).
    seen: set = set()
    for t in train:
        t["a"]["artist known"] = bool(t["arts"] & seen)
        seen |= t["arts"]
    edges = {}
    for row in ROWS:
        vals = [t["a"][row] for t in train if isinstance(t["a"][row], (int, float)) and not isinstance(t["a"][row], bool)]
        if vals and row not in ("score", "artist known"):
            edges[row] = np.quantile(vals, [1 / 3, 2 / 3])
    buckets = {row: np.array([_bucket(row, t["a"][row], edges) for t in train]) for row in ROWS}
    add = (y == ADD).astype(float)
    base, m = add.mean(), CFG.get("shrink", 8.0)

    voc = _vocals(conn)
    cands = playable_ids(conn)
    if len(cands) < CFG.get("min_playable", 10):      # (a top share of a handful isn't a choice)
        db.set_setting(conn, "group_scores", None, log=False)
        return {}
    marks = ",".join("?" * len(cands))
    info = {r["id"]: r for r in conn.execute(f"SELECT id, artist, duration_s, video_id, veto FROM tracks WHERE id IN ({marks})",
                                             cands)}
    embs = {t: db.from_blob(b) for t, b in conn.execute(
        f"SELECT track_id, embedding FROM track_embeddings WHERE track_id IN ({marks})", cands)}
    lift = _explained(buckets, add, base, m)
    grid, sim, ids = [], [], []
    for tid in cands:
        r, e = info.get(tid), embs.get(tid)
        if r is None or e is None:
            continue
        a = _attrs(r["veto"], r["duration_s"], voc.get(r["video_id"], {}))
        a["artist known"] = bool(_artists(r["artist"]) & seen)
        g = sum(np.log(lift[row].get(_bucket(row, a[row], edges), 1.0)) for row in ROWS)
        s = _similar(E, y, e / (np.linalg.norm(e) or 1))
        grid.append(g)
        sim.append(0.0 if s is None else s)
        ids.append(tid)
    if not ids:
        return {}

    def z(v):
        v = np.array(v, float)
        return (v - v.mean()) / (v.std() or 1)
    score = z(grid) + z(sim)
    order = np.argsort(-score)
    n_top = max(CFG.get("top_min", 3), int(round(len(ids) * CFG.get("top_share", 0.1))))
    rank = {ids[i]: k for k, i in enumerate(order)}
    out = {"at": time.time(), "ratings": len(train), "adds": int(add.sum()), "playable": len(ids),
           "scores": {str(t): {"score": round(float(score[i]), 3), "grid": round(float(grid[i]), 3),
                               "similar": round(float(sim[i]), 3), "pct": round(1 - rank[t] / len(ids), 3)}
                      for i, t in enumerate(ids)},
           "top": [ids[i] for i in order[:n_top]]}
    db.set_setting(conn, "group_scores", out, log=False)
    db.log_event(conn, "groups_update", ratings=len(train), adds=int(add.sum()), playable=len(ids),
                 seconds=round(time.monotonic() - t0, 2))
    return out


def top(conn: sqlite3.Connection, playable: set) -> list[int]:
    """The group picks to draw from now: the top share, still playable."""
    g = db.get_setting(conn, "group_scores") or {}
    return [t for t in g.get("top", []) if t in playable]


def record(conn: sqlite3.Connection, track_id: int) -> str | None:
    """The song's group score when it's shown (JSON), for Results and later checks."""
    g = db.get_setting(conn, "group_scores") or {}
    s = (g.get("scores") or {}).get(str(track_id))
    return json.dumps({**s, "ratings": g.get("ratings"), "adds": g.get("adds")}) if s else None
