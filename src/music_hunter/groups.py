"""Group picks: a share of Find music's picks chosen to find Adds (SPEC.md §20.9, 2026-11-25, my idea of a grid).

Rows are attributes and columns their buckets (fifths of each measured attribute); every rated song in the playlist's folder (lane songs included: the
lanes build this data) passes through one bucket per row. A bucket's Add chance is its Add rate relative to the
overall rate, judged against what each song's other buckets already predict (an Off that passed through one good
bucket, but whose other buckets were all bad, hardly counts against the good one), and shrunk towards it while few
songs have passed through ([groups] shrink; leaning on its neighbouring buckets first). Each bucket then counts by
where it sits between its row's lowest and highest bucket. Rows: length; approachable,
happy, vocal share, bright (the vocal analysis); artist rated before; closeness to seeds and to likes; number of
sources; and the veto score in its 0.5 SD bands. A song's grid value is the sum of where its buckets sit; to it is added
how its 10 most similar rated songs went (Add rate − Off rate). Both are standardised over every analysed song in the
score range the evidence covers ([groups] min_z and up), played or not.

A fixed standard (2026-11-25, my note): a song qualifies when its group score is in the top [groups] top_share of all
those songs, so the bar doesn't sink as the best are played. Group picks are at most [groups] share of the picks: a
random song that qualifies and whose audio is here; when none is ready, the pick goes to a lane (else, a random
playable song). The best qualifying songs keep their audio (or get it back, the 'restore' job), and when fewer than
[groups] min_supply qualify, more candidates are downloaded (the 'groups' job, up to [groups] fetch_per_day).

Chosen on a replay (2026-11-25, 471 random picks, each scored only from earlier ratings): Add-AUC 0.68–0.70, its top
10% of random picks Add 21–25% / Off 17–19% (random 8% / 36%, lanes 14% / 24%). Each pick records its group score
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
GONE = ("dropped", "owned", "unavailable", "surplus")


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
    row (backfitting). While few songs have passed through a bucket it leans on its neighbouring buckets (themselves
    leaning on average), not straight on average, so a run of similar buckets holds each other up when a row is cut
    finer (my note: finer buckets shouldn't dilute a strong range)."""
    lift = {row: {} for row in buckets}
    L = {row: np.ones(len(add)) for row in buckets}
    for _ in range(rounds):
        for row, col in buckets.items():
            others = base * np.prod([L[j] for j in buckets if j != row], axis=0)
            labels = set(col)
            obs = {b: add[col == b].sum() for b in labels}
            exp = {b: others[col == b].sum() for b in labels}
            for b in labels:
                target = 1.0
                if b.isdigit():                            # an ordered row: its neighbours
                    nb = [str(int(b) + d) for d in (-1, 1) if str(int(b) + d) in labels]
                    if nb:
                        target = (sum(obs[x] for x in nb) + m * base) / (sum(exp[x] for x in nb) + m * base)
                lift[row][b] = (obs[b] + m * base * target) / (exp[b] + m * base)
            L[row] = np.array([lift[row][b] for b in col])
    return lift


def _placed(lift: dict) -> dict:
    """Each bucket as where it sits between its row's lowest and highest bucket (−0.5 to +0.5), so every row has the
    same say and no single noisy bucket dominates (my note: how close it is to the row's max and min). Replayed at 5
    buckets: better than raw lifts (Add-AUC 0.682 → 0.697, top 10% Off 29% → 19%)."""
    out = {}
    for row, lifts in lift.items():
        logs = {b: np.log(v) for b, v in lifts.items()}
        lo, hi = min(logs.values()), max(logs.values())
        out[row] = {b: ((v - lo) / (hi - lo) - 0.5) if hi > lo else 0.0 for b, v in logs.items()}
    return out


def update(conn: sqlite3.Connection) -> dict:
    """Score every analysed song and keep the result (setting group_scores) for the picks: the standard (bar) and
    the unplayed songs meeting it. Run after every rating."""
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
            q = CFG.get("buckets", 5)
            edges[row] = np.unique(np.quantile(vals, np.arange(1, q) / q))
    buckets = {row: np.array([_bucket(row, t["a"][row], edges) for t in train]) for row in ROWS}
    add = (y == ADD).astype(float)
    base, m = add.mean(), CFG.get("shrink", 8.0)

    # Every analysed song in the score range the evidence covers ([groups] min_z and up, not shut out by the tempo
    # band or the backstop), played or not: the standard is set over all of them, so it stays put as the best are
    # played (2026-11-25, my note: a fixed standard, not the top of whatever is left).
    voc = _vocals(conn)
    rows_ = conn.execute("SELECT t.id, t.artist, t.duration_s, t.video_id, t.veto, t.veto_state, e.embedding,"
                         " t.id IN (SELECT track_id FROM plays) AS played FROM tracks t"
                         " JOIN track_embeddings e ON e.track_id = t.id WHERE t.origin = 'pool' AND t.veto IS NOT NULL"
                         ).fetchall()
    placed = _placed(_explained(buckets, add, base, m))
    grid, sim, ids, played = [], [], [], []
    for r in rows_:
        v = json.loads(r["veto"])
        rules = v.get("rules", {})
        if (v.get("z") is None or v["z"] < CFG.get("min_z", 0.0) or rules.get("tempo_band") or rules.get("backstop")
                or (r["veto_state"] or "playable") in GONE):
            continue
        a = _attrs(r["veto"], r["duration_s"], voc.get(r["video_id"], {}))
        a["artist known"] = bool(_artists(r["artist"]) & seen)
        e = db.from_blob(r["embedding"])
        grid.append(sum(placed[row].get(_bucket(row, a[row], edges), 0.0) for row in ROWS))
        s = _similar(E, y, e / (np.linalg.norm(e) or 1))
        sim.append(0.0 if s is None else s)
        ids.append(r["id"])
        played.append(bool(r["played"]))
    if len(ids) < CFG.get("min_songs", 50):
        db.set_setting(conn, "group_scores", None, log=False)
        return {}

    def z(v):
        v = np.array(v, float)
        return (v - v.mean()) / (v.std() or 1)
    score = z(grid) + z(sim)
    bar = float(np.quantile(score, 1 - CFG.get("top_share", 0.1)))
    order = np.argsort(-score)
    rank = {ids[i]: k for k, i in enumerate(order)}
    unplayed = [i for i in order if not played[i]]
    out = {"at": time.time(), "ratings": len(train), "adds": int(add.sum()), "songs": len(ids), "bar": round(bar, 3),
           "scores": {str(ids[i]): {"score": round(float(score[i]), 3), "grid": round(float(grid[i]), 3),
                                    "similar": round(float(sim[i]), 3), "pct": round(1 - rank[ids[i]] / len(ids), 3)}
                      for i in unplayed},
           "qualify": [ids[i] for i in unplayed if score[i] >= bar]}
    db.set_setting(conn, "group_scores", out, log=False)
    db.log_event(conn, "groups_update", ratings=len(train), adds=int(add.sum()), songs=len(ids),
                 qualify=len(out["qualify"]), seconds=round(time.monotonic() - t0, 2))
    return out


def qualifying(conn: sqlite3.Connection) -> list[int]:
    """Unplayed songs meeting the standard, best first (audio or not)."""
    return (db.get_setting(conn, "group_scores") or {}).get("qualify", [])


def ready(conn: sqlite3.Connection) -> list[int]:
    """Songs meeting the standard whose audio is here: a group pick never waits for a download."""
    q = qualifying(conn)
    if not q:
        return []
    marks = ",".join("?" * len(q))
    have = {t for t, in conn.execute(f"SELECT id FROM tracks WHERE id IN ({marks}) AND file_path IS NOT NULL"
                                     " AND id NOT IN (SELECT track_id FROM plays)", q)}
    return [t for t in q if t in have]


def keep_ids(conn: sqlite3.Connection) -> set:
    """Songs whose audio is kept (or fetched again) for group picks: the best [groups] keep of those meeting the
    standard."""
    return set(qualifying(conn)[:CFG.get("keep", 40)]) if enabled(conn) else set()


def missing_audio(conn: sqlite3.Connection) -> list[int]:
    keep = [t for t in qualifying(conn)[:CFG.get("keep", 40)]]
    if not keep or not enabled(conn):
        return []
    marks = ",".join("?" * len(keep))
    gone = {t for t, in conn.execute(f"SELECT id FROM tracks WHERE id IN ({marks}) AND file_path IS NULL", keep)}
    return [t for t in keep if t in gone]


def short(conn: sqlite3.Connection) -> bool:
    """Too few songs meet the standard: more are downloaded (the 'groups' job), up to [groups] fetch_per_day."""
    if not enabled(conn) or db.get_setting(conn, "group_scores") is None:
        return False
    if len(qualifying(conn)) >= CFG.get("min_supply", 15):
        return False
    today = sum(json.loads(d).get("tried", 0) for d, in conn.execute(
        "SELECT data FROM events WHERE kind = 'groups_fetched' AND at >= date('now')"))
    return today < CFG.get("fetch_per_day", 300)


def record(conn: sqlite3.Connection, track_id: int) -> str | None:
    """The song's group score when it's shown (JSON), for Results and later checks."""
    g = db.get_setting(conn, "group_scores") or {}
    s = (g.get("scores") or {}).get(str(track_id))
    return json.dumps({**s, "ratings": g.get("ratings"), "adds": g.get("adds")}) if s else None
