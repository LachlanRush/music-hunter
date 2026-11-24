"""Lanes: Find music following my stars, beside the random picks (SPEC.md §20.7, 2026-11-24).

A starred song in a style playlist ([lanes] folders) has a song lane: songs that sound like it (MAEST closeness at
least [lanes] song_min_similarity), its own suggestions first (it's a seed: Last.fm, YouTube radio, Deezer, TIDAL
already listed what's like it), one per star. Each artist credited on a starred song has an artist lane: their songs
that sound like their starred songs (artist_min_similarity), one per star of their highest-starred song. However a
song got its stars (a + from a random pick or a lane, an import), it starts its lanes. The veto's score plays no
part (unless [lanes] min_z is set); the playlist's tempo band still applies. A song can be in several lanes (two
starred songs suggesting it, or a song and an artist lane): it plays once and counts for each.

When a lane is short, more is downloaded for it (the 'lanes' background job, analysed on the PC): a song lane from its
seed's own suggestion lists not yet downloaded, best list positions first; an artist lane from suggestions of that
artist, then from the artist's own top tracks (Deezer and Last.fm, searched at most once a day). At most fetch_max
downloads per lane per batch. Downloads that don't pass the filter still join the pool for the random picks.

Once every song of a lane's batch has been played (or can no longer be), the batch is scored: Adds − Offs (heard
counts 0). At refill_min_net or more the lane gets a new batch, sized by the stars at that moment; otherwise it goes
quiet. A quiet song lane wakes when its song's stars go up (stars given again after being cleared count); a quiet
artist lane when another of their songs gets stars. Stars lowered or cleared don't touch a batch already queued.

Picks (ratings.upcoming): each pick comes from the lanes with probability [lanes] share, a random waiting song whose
audio is already here (a lane pick never waits for a download; the rest are fetched in the background straight away),
else a random playable track as before. Lane songs stay in the random picks too: whichever reaches a song first plays
it, and its rating counts for its lanes either way. Each play records its lane (plays.lane_id; NULL = random).
"""

import json
import random
import sqlite3

import numpy as np

from . import db, names
from .config import CONFIG

CFG = CONFIG.get("lanes", {})
GONE = ("dropped", "owned", "unavailable")      # veto states a song never comes back from for a lane
RULES = 3                                       # bumped when the rules for choosing lane songs change
UNTRIED = ("candidate", "not_selected")         # candidates not downloaded yet


def enabled(conn: sqlite3.Connection) -> bool:
    """Lanes run for playlists in [lanes] folders (the style playlists)."""
    if not CFG.get("enabled", False):
        return False
    pid = db.SESSION.get()
    if pid is None:
        return False
    row = db.connect_library().execute("SELECT f.name FROM playlists p JOIN folders f ON f.id = p.folder_id"
                                       " WHERE p.id = ?", (pid,)).fetchone()
    return row is not None and row[0] in CFG.get("folders", [])


def artists(text: str) -> list[tuple[str, str]]:
    """(key, label) of every credited artist: "Upper90, Baron Von Trax" -> [("upper90", "Upper90"), …]."""
    out = []
    for part in names._ARTIST_SPLIT.split(names._BRACKETS.sub("", text or "")):
        key = names.fold(part)
        if key and key not in {k for k, _ in out}:
            out.append((key, part.strip()))
    return out


def starred(conn: sqlite3.Connection) -> list[dict]:
    """The playlist's starred songs with their fingerprints: {key, artist, title, stars, emb}."""
    pid = db.SESSION.get()
    if pid is not None:
        rows = db.connect_library().execute(
            "SELECT t.id, t.artist, t.title, pt.stars, e.embedding FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id"
            " LEFT JOIN track_embeddings e ON e.track_id = t.id WHERE pt.playlist_id = ? AND pt.stars > 0", (pid,))
    else:                                       # (a database without a library: its seeds' stars)
        rows = conn.execute("SELECT t.id, t.artist, t.title, t.stars, e.embedding FROM tracks t LEFT JOIN track_embeddings e"
                            " ON e.track_id = t.id WHERE t.origin = 'original_reference' AND t.stars > 0")
    return [{"key": str(r[0]), "artist": r[1] or "", "title": r[2] or "", "stars": int(r[3]),
             "emb": db.from_blob(r[4]) if r[4] else None} for r in rows]


def _pool(conn: sqlite3.Connection) -> dict[int, dict]:
    """Unplayed analysed pool songs a lane can take: {id: {artists, song, z, usable, embedding}}."""
    out = {}
    for r in conn.execute("SELECT t.id, t.artist, t.title, t.veto_state, t.veto, e.embedding FROM tracks t"
                          " JOIN track_embeddings e ON e.track_id = t.id WHERE t.origin = 'pool'"
                          " AND t.id NOT IN (SELECT track_id FROM plays)"):
        v = json.loads(r["veto"]) if r["veto"] else {}
        rules = v.get("rules", {})
        out[r["id"]] = {"artists": names.split_artists(r["artist"] or ""), "song": names.song(r["artist"] or "", r["title"] or ""),
                        "z": v.get("z", -99.0), "emb": r["embedding"],
                        "usable": (r["veto_state"] or "playable") not in GONE and bool(v)
                                  and not rules.get("tempo_band")}
    return out


def waiting(conn: sqlite3.Connection, pool: dict | None = None, ready: bool = False) -> dict[int, int]:
    """Lane songs waiting to be played: {track_id: lane_id (the first, for a song in several lanes)}. (Without `pool`: read for those songs only — it's on
    the player's path.) ready: only those whose audio is here — the only ones a pick is drawn from, so a lane pick
    never waits for a download (2026-11-24)."""
    if pool is not None:
        out = {}
        for tid, lane_id in conn.execute("SELECT track_id, lane_id FROM lane_songs ORDER BY lane_id"):
            if tid in pool and pool[tid]["usable"]:
                out.setdefault(tid, lane_id)
        return out
    out = {}
    for tid, lane_id, state, veto in conn.execute(
            "SELECT ls.track_id, ls.lane_id, t.veto_state, t.veto FROM lane_songs ls JOIN tracks t ON t.id = ls.track_id"
            " WHERE ls.track_id NOT IN (SELECT track_id FROM plays)" + (" AND t.file_path IS NOT NULL" if ready else "")
            + " ORDER BY ls.lane_id"):
        rules = json.loads(veto).get("rules", {}) if veto else {}
        if (state or "playable") not in GONE and veto and not rules.get("tempo_band"):
            out.setdefault(tid, lane_id)            # (a song in several lanes: recorded under the first)
    return out


def waiting_ids(conn: sqlite3.Connection) -> set[int]:
    """(cheap) every lane song not yet played, usable or not: its audio is kept."""
    return {r[0] for r in conn.execute("SELECT track_id FROM lane_songs WHERE track_id NOT IN (SELECT track_id FROM plays)")}


def missing_audio(conn: sqlite3.Connection) -> list[int]:
    """Waiting lane songs without their audio: fetched in the background (the session's 'restore' job) as soon as a
    lane queues them, so they're ready long before a pick reaches them."""
    w = waiting(conn)
    return [t for t, in conn.execute("SELECT id FROM tracks WHERE file_path IS NULL") if t in w] if w else []


def draw(conn: sqlite3.Connection, rng=random) -> tuple[int, int] | None:
    """A random waiting lane song whose audio is here: (track_id, lane_id), or None."""
    w = waiting(conn, ready=True)
    if not w:
        return None
    tid = rng.choice(sorted(w))
    return tid, w[tid]


def _result(conn: sqlite3.Connection, lane_id: int, batch: int) -> int:
    """Adds − Offs among a batch's played songs (each song's latest rating; heard counts 0)."""
    from .ratings import ADD
    net = 0
    for (rating,) in conn.execute(
            "SELECT (SELECT rating FROM plays p WHERE p.track_id = ls.track_id AND p.rating IS NOT NULL"
            " ORDER BY p.id DESC LIMIT 1) FROM lane_songs ls WHERE ls.lane_id = ? AND ls.batch = ?", (lane_id, batch)):
        net += 1 if rating == ADD else -1 if rating == 1 else 0
    return net


def _batch_done(conn: sqlite3.Connection, lane, pool: dict) -> bool:
    """The current batch has songs, and every one is rated or out of reach (dropped, owned, shut out). Songs still
    owed (none found when it was filled) are let go then: a short batch is scored as it is."""
    placed = conn.execute("SELECT count(*) FROM lane_songs WHERE lane_id = ? AND batch = ?", (lane["id"], lane["batch"])).fetchone()[0]
    if not placed:
        return False
    for (tid,) in conn.execute("SELECT track_id FROM lane_songs WHERE lane_id = ? AND batch = ?", (lane["id"], lane["batch"])):
        rated = conn.execute("SELECT 1 FROM plays WHERE track_id = ? AND rating IS NOT NULL", (tid,)).fetchone()
        if not rated and tid in pool and pool[tid]["usable"]:
            return False
        if not rated and conn.execute("SELECT 1 FROM plays WHERE track_id = ?", (tid,)).fetchone():
            return False                            # on air, not rated yet
    return True


def seed_id(conn: sqlite3.Connection, key: str) -> int | None:
    """The session's seed track for a starred library song (its suggestions are the seed's own lists). A song first
    found by Find music and Added is its own pick row, not a copied seed (feedback #132: Match Box's lane never
    searched, because Trentino was such a song)."""
    row = conn.execute("SELECT id FROM tracks WHERE library_id = ? ORDER BY origin = 'original_reference' DESC, id"
                       " LIMIT 1", (int(key),)).fetchone() or \
        conn.execute("SELECT id FROM tracks WHERE origin = 'original_reference' AND library_id IS NULL AND id = ?",
                     (int(key),)).fetchone()
    return row[0] if row else None


def own_tracks(conn: sqlite3.Connection, seed: int | None) -> set[int]:
    """Analysed songs a seed's own lists suggested."""
    if seed is None:
        return set()
    return {r[0] for r in conn.execute("SELECT DISTINCT c.track_id FROM suggestions g JOIN candidates c"
                                       " ON c.id = g.candidate_id WHERE g.seed_track_id = ? AND c.track_id IS NOT NULL",
                                       (seed,))}


def _matches(conn: sqlite3.Connection, lane, refs: list[dict], pool: dict, own: set) -> list[int]:
    """Songs that may join a lane, best first: usable, not in it before, not the starred song itself, by the artist
    (artist lanes), at least the lane's similarity to `refs` (the starred song, or the artist's starred songs); a song
    lane's own suggestions first."""
    refs = [r for r in refs if r["emb"] is not None]
    if not refs:
        return []
    before = {r[0] for r in conn.execute("SELECT track_id FROM lane_songs WHERE lane_id = ?", (lane["id"],))}
    min_z = CFG.get("min_z")
    ok = [t for t, p in pool.items() if p["usable"] and (min_z is None or p["z"] > min_z) and t not in before]
    if lane["kind"] == "artist":
        ok = [t for t in ok if lane["key"] in pool[t]["artists"]]
    mine = [names.song(r["artist"], r["title"]) for r in refs]
    ok = [t for t in ok if not any(pool[t]["song"].same_song(m) for m in mine)]     # never the starred song itself
    R = np.stack([r["emb"] / (np.linalg.norm(r["emb"]) or 1) for r in refs])
    floor = CFG.get("song_min_similarity" if lane["kind"] == "song" else "artist_min_similarity", 0.0)
    sim = {t: float((R @ db.from_blob(pool[t]["emb"])).max()) for t in ok}
    return sorted((t for t in ok if sim[t] >= floor), key=lambda t: (t not in own, -sim[t]))


def _fill(conn: sqlite3.Connection, lane, refs: list[dict], pool: dict, own: set) -> int:
    """Add up to the songs a lane is owed to its current batch. Returns how many were added."""
    if lane["due"] <= 0:
        return 0
    picked = _matches(conn, lane, refs, pool, own)[:lane["due"]]
    for t in picked:
        conn.execute("INSERT OR IGNORE INTO lane_songs (lane_id, track_id, batch) VALUES (?, ?, ?)", (lane["id"], t, lane["batch"]))
    conn.execute("UPDATE lanes SET due = due - ?, updated_at = datetime('now') WHERE id = ?", (len(picked), lane["id"]))
    return len(picked)


def _stars(conn: sqlite3.Connection):
    """(by_song, by_artist): the starred songs by key, and each credited artist's {label, stars (highest), songs}."""
    stars = starred(conn)
    by_song = {s["key"]: s for s in stars}
    by_artist: dict[str, dict] = {}
    for s in stars:
        for key, label in artists(s["artist"]):
            a = by_artist.setdefault(key, {"label": label, "stars": 0, "songs": set()})
            a["stars"] = max(a["stars"], s["stars"])
            a["songs"].add(s["key"])
    return by_song, by_artist


def _refs(lane, by_song: dict, by_artist: dict) -> list[dict]:
    if lane["kind"] == "song":
        return [by_song[lane["key"]]] if lane["key"] in by_song else []
    return [by_song[k] for k in sorted(by_artist[lane["key"]]["songs"])] if lane["key"] in by_artist else []


def sync(conn: sqlite3.Connection) -> dict:
    """Start lanes for new stars, score finished batches, refill or quieten, wake quiet lanes, top up owed songs.
    Called after every veto update (ratings, star changes, top-ups). Returns counts."""
    if not enabled(conn):
        return {}
    by_song, by_artist = _stars(conn)
    stars = list(by_song.values())
    pool = _pool(conn)
    if db.get_setting(conn, "lane_rules") != RULES:  # the rules changed (2026-11-24: a similarity filter, own lists,
        with conn:                                    # shared songs): lanes none of whose songs have played queue afresh
            for lane in conn.execute("SELECT * FROM lanes WHERE batch = 1").fetchall():
                if conn.execute("SELECT 1 FROM lane_songs ls JOIN plays p ON p.track_id = ls.track_id"
                                " WHERE ls.lane_id = ?", (lane["id"],)).fetchone():
                    continue
                size = (by_song[lane["key"]]["stars"] if lane["key"] in by_song else 0) if lane["kind"] == "song" \
                    else (by_artist[lane["key"]]["stars"] if lane["key"] in by_artist else 0)
                conn.execute("DELETE FROM lane_songs WHERE lane_id = ?", (lane["id"],))
                conn.execute("UPDATE lanes SET due = ?, state = 'active' WHERE id = ?", (size, lane["id"]))
        db.set_setting(conn, "lane_rules", RULES, log=False)
    counts = {"started": 0, "refilled": 0, "quiet": 0, "woken": 0, "added": 0}
    with conn:
        have = {(r["kind"], r["key"]) for r in conn.execute("SELECT kind, key FROM lanes")}
        for s in stars:
            if ("song", s["key"]) not in have:
                conn.execute("INSERT INTO lanes (kind, key, label, batch, due) VALUES ('song', ?, ?, 1, ?)",
                             (s["key"], f"{s['artist']} – {s['title']}", s["stars"]))
                counts["started"] += 1
        for key, a in by_artist.items():
            if ("artist", key) not in have:
                conn.execute("INSERT INTO lanes (kind, key, label, batch, due) VALUES ('artist', ?, ?, 1, ?)",
                             (key, a["label"], a["stars"]))
                counts["started"] += 1
        for lane in conn.execute("SELECT * FROM lanes ORDER BY id").fetchall():
            size = (by_song[lane["key"]]["stars"] if lane["key"] in by_song else 0) if lane["kind"] == "song" \
                else (by_artist[lane["key"]]["stars"] if lane["key"] in by_artist else 0)
            songs_now = sorted(by_artist[lane["key"]]["songs"]) if lane["kind"] == "artist" and lane["key"] in by_artist else []
            if lane["state"] == "quiet":
                wake = json.loads(lane["wake"] or "{}")
                if size and (size > wake.get("stars", 0) if lane["kind"] == "song"
                             else set(songs_now) - set(wake.get("songs", []))):
                    conn.execute("UPDATE lanes SET state = 'active', batch = batch + 1, due = ?, wake = NULL,"
                                 " updated_at = datetime('now') WHERE id = ?", (size, lane["id"]))
                    db.log_event(conn, "lane_woken", lane=lane["id"], label=lane["label"], size=size)
                    counts["woken"] += 1
            elif _batch_done(conn, lane, pool):
                net = _result(conn, lane["id"], lane["batch"])
                if size and net >= CFG.get("refill_min_net", 1):
                    conn.execute("UPDATE lanes SET batch = batch + 1, due = ?, updated_at = datetime('now') WHERE id = ?",
                                 (size, lane["id"]))
                    db.log_event(conn, "lane_refilled", lane=lane["id"], label=lane["label"], batch=lane["batch"], net=net, size=size)
                    counts["refilled"] += 1
                else:
                    conn.execute("UPDATE lanes SET state = 'quiet', due = 0, wake = ?, updated_at = datetime('now') WHERE id = ?",
                                 (json.dumps({"stars": size, "songs": songs_now}), lane["id"]))
                    db.log_event(conn, "lane_quiet", lane=lane["id"], label=lane["label"], batch=lane["batch"], net=net)
                    counts["quiet"] += 1
        for lane in conn.execute("SELECT * FROM lanes WHERE state = 'active' AND due > 0 ORDER BY id").fetchall():
            refs = _refs(lane, by_song, by_artist)
            own = own_tracks(conn, seed_id(conn, lane["key"])) if lane["kind"] == "song" else set()
            counts["added"] += _fill(conn, lane, refs, pool, own)
    if any(counts.values()):
        db.log_event(conn, "lanes_synced", **counts)
    return counts


def _artist_candidates(conn: sqlite3.Connection, key: str) -> list[int]:
    """Candidates not downloaded yet credited to an artist (from any seed's lists), best list position first."""
    return [r[0] for r in conn.execute(
        "SELECT c.id FROM candidates c JOIN suggestions g ON g.candidate_id = c.id"
        f" WHERE c.status IN {UNTRIED} AND c.song_artists LIKE ? GROUP BY c.id ORDER BY min(g.rank), c.id",
        (f'%{json.dumps(key)}%',))]


def _seed_candidates(conn: sqlite3.Connection, seed: int) -> list[int]:
    """A seed's own suggestions not downloaded yet, best list position first."""
    return [r[0] for r in conn.execute(
        "SELECT c.id FROM candidates c JOIN suggestions g ON g.candidate_id = c.id"
        f" WHERE c.status IN {UNTRIED} AND g.seed_track_id = ? GROUP BY c.id ORDER BY min(g.rank), c.id", (seed,))]


def short_lanes(conn: sqlite3.Connection) -> list:
    """Active lanes still owed songs with downloads left in their budget, fewest waiting first."""
    rows = conn.execute(
        "SELECT l.*, (SELECT count(*) FROM lane_fetches f WHERE f.lane_id = l.id AND f.batch = l.batch) AS fetched,"
        " (SELECT count(*) FROM lane_songs s WHERE s.lane_id = l.id AND s.track_id NOT IN (SELECT track_id FROM plays))"
        " AS waiting FROM lanes l WHERE l.state = 'active' AND l.due > 0").fetchall()
    return sorted((r for r in rows if r["fetched"] < CFG.get("fetch_max", 40)), key=lambda r: (r["waiting"], r["id"]))


def wanted(conn: sqlite3.Connection, search=None) -> list[tuple[int, int, int]]:
    """Downloads short lanes want now: [(candidate_id, seed_id, lane_id)], up to fetch_per_round per lane. A seed's
    own lists for a song lane; for an artist lane, that artist's suggestions, then (`search`: a function that lists
    the artist's top tracks as candidates) a search at most once a day."""
    if not enabled(conn):
        return []
    by_song, by_artist = _stars(conn)
    out, seen = [], set()
    for lane in short_lanes(conn):
        tried = {r[0] for r in conn.execute("SELECT candidate_id FROM lane_fetches WHERE lane_id = ?", (lane["id"],))}
        room = min(CFG.get("fetch_per_round", 8), CFG.get("fetch_max", 40) - lane["fetched"])
        if lane["kind"] == "song":
            seed = seed_id(conn, lane["key"])
            cands = _seed_candidates(conn, seed) if seed else []
        else:
            refs = _refs(lane, by_song, by_artist)
            top = max(refs, key=lambda r: r["stars"]) if refs else None
            seed = seed_id(conn, top["key"]) if top else None
            if seed is None:
                continue
            cands = _artist_candidates(conn, lane["key"])
            if not [c for c in cands if c not in tried] and search is not None:
                due = conn.execute("SELECT searched_at IS NULL OR searched_at < datetime('now', '-1 day') FROM lanes"
                                   " WHERE id = ?", (lane["id"],)).fetchone()[0]
                if due:
                    with conn:
                        conn.execute("UPDATE lanes SET searched_at = datetime('now') WHERE id = ?", (lane["id"],))
                    search(lane, seed)
                    cands = _artist_candidates(conn, lane["key"])
        for cid in [c for c in cands if c not in tried and c not in seen][:room]:
            seen.add(cid)
            out.append((cid, seed, lane["id"]))
    return out


def fetchable(conn: sqlite3.Connection) -> bool:
    """Whether a short lane has anything left to download now: candidates, or an artist search due."""
    if wanted(conn):
        return True
    return any(lane["kind"] == "artist" and conn.execute(
        "SELECT searched_at IS NULL OR searched_at < datetime('now', '-1 day') FROM lanes WHERE id = ?",
        (lane["id"],)).fetchone()[0] for lane in short_lanes(conn))


def settle_short(conn: sqlite3.Connection) -> None:
    """Short lanes with nothing left to download: a batch that has songs is let go short (scored as it is once they've
    played); one with none waits for new candidates (a later pull) or stars."""
    with conn:
        for lane in short_lanes(conn):
            if conn.execute("SELECT 1 FROM lane_songs WHERE lane_id = ? AND batch = ?", (lane["id"], lane["batch"])).fetchone():
                conn.execute("UPDATE lanes SET due = 0 WHERE id = ?", (lane["id"],))


def fetched(conn: sqlite3.Connection, lane_id: int, cid: int) -> None:
    """Count a download against its lane's budget for this batch."""
    with conn:
        conn.execute("INSERT OR IGNORE INTO lane_fetches (lane_id, candidate_id, batch) SELECT ?, ?, batch FROM lanes"
                     " WHERE id = ?", (lane_id, cid, lane_id))


def search_artist(conn: sqlite3.Connection, lane, seed: int) -> int:
    """List an artist's own top tracks (Deezer and Last.fm) as candidates, credited to `seed` under the source
    'artist_top' (not counted as a source by the veto). Returns how many new candidates."""
    from .pull import Pull, Seed
    from .sources import Deezer, Item, LastFM

    gen = conn.execute("SELECT max(id) FROM generations").fetchone()[0]
    s = conn.execute("SELECT * FROM tracks WHERE id = ?", (seed,)).fetchone()
    p = Pull(conn, None, None, generation=gen)
    for r in conn.execute("SELECT id, song_title, song_artists, is_extended FROM candidates"
                          f" WHERE status IN {UNTRIED}").fetchall():
        p.current[r["id"]] = p._candidate(r)                      # (merges into candidates already listed)
    items: list = []
    try:
        dz = Deezer()
        hits = dz._get("/search/artist", q=lane["label"]).get("data", [])
        hit = next((h for h in hits if names.fold(h["name"]) == lane["key"]), None)
        if hit:
            items += [Item(t["artist"]["name"], t["title"], float(t["duration"]), deezer_id=t["id"])
                      for t in dz._get(f"/artist/{hit['id']}/top", limit=CFG.get("artist_top_tracks", 30)).get("data", [])]
    except Exception as e:  # noqa: BLE001 — logged; the other source may still answer
        db.log_event(conn, "lane_search_failed", lane=lane["id"], source="deezer", error=str(e)[:200])
    try:
        lf = LastFM()
        tracks = lf._call("artist.getTopTracks", artist=lane["label"],
                          limit=CFG.get("artist_top_tracks", 30))["toptracks"]["track"]
        items += [Item(t["artist"]["name"], t["name"], None) for t in tracks]
    except Exception as e:  # noqa: BLE001
        db.log_event(conn, "lane_search_failed", lane=lane["id"], source="lastfm", error=str(e)[:200])
    before = len(p.current)
    seed_obj = Seed(s["id"], s["artist"], s["title"], s["duration_s"] or 0, s["video_id"], s["stars"])
    for rank, item in enumerate(items, 1):
        p.process(seed_obj, "artist_top", rank, item)
    db.log_event(conn, "lane_searched", lane=lane["id"], label=lane["label"], listed=len(items), new=len(p.current) - before)
    return len(p.current) - before


def summary(conn: sqlite3.Connection) -> list[dict]:
    """Every lane: kind, label, state, batch, songs waiting, and its plays' Off / heard / Add counts."""
    from .ratings import ADD
    out = []
    for lane in conn.execute("SELECT * FROM lanes ORDER BY kind DESC, id"):
        rows = conn.execute("SELECT p.rating FROM lane_songs ls JOIN plays p ON p.track_id = ls.track_id"
                            " WHERE ls.lane_id = ? AND p.rating IS NOT NULL", (lane["id"],)).fetchall()
        r = [x[0] for x in rows]
        out.append({"id": lane["id"], "kind": lane["kind"], "label": lane["label"], "state": lane["state"],
                    "batch": lane["batch"], "due": lane["due"],
                    "waiting": conn.execute("SELECT count(*) FROM lane_songs WHERE lane_id = ? AND track_id NOT IN"
                                            " (SELECT track_id FROM plays)", (lane["id"],)).fetchone()[0],
                    "plays": len(r), "off": r.count(1), "add": r.count(ADD)})
    return out
