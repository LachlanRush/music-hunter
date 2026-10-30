"""One pull ("generation") of candidates — SPEC.md §4.1 steps 1–4, §16.3–16.4.

Seeds are matched to YouTube Music, every source is queried for every seed, and each raw
suggestion is filtered and either becomes a candidate, is merged into one (keeping its
provenance), or is dropped with a reason.

v3: suggestions are compared by name, ISRC and YouTube ID only; finding a Last.fm, Deezer or
TIDAL suggestion on YouTube Music (and fetching Deezer's ISRC and release date) waits until the
candidate is picked for the pool (`Matcher`, called by pool.Filler). With ~100 suggestions per
source per seed, searching all of them up front would take over an hour; most are never picked.
"""

import json
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass

from . import db, names
from .config import CONFIG
from .ratings import ADD
from .sources import Deezer, Item, LastFM, Tidal, YouTubeMusic

TOLERANCE = CONFIG["sources"]["duration_tolerance_seconds"]
FAMILIES = CONFIG["sources"]["families"]


@dataclass
class Seed:
    track_id: int
    artist: str
    title: str
    duration_s: float
    video_id: str | None = None

    @property
    def label(self) -> str:
        return f"{self.artist} – {self.title}"


def seeds(conn: sqlite3.Connection) -> list[Seed]:
    """The originals plus every learning play rated Add (SPEC.md §15.2).

    Rated pool tracks came from YouTube Music, so they already have their video ID.
    """
    rows = conn.execute(
        "SELECT t.id, t.artist, t.title, t.duration_s, coalesce(m.video_id, t.video_id) FROM tracks t"
        " LEFT JOIN seed_matches m ON m.track_id = t.id"
        " WHERE t.origin = 'original_reference'"
        f"    OR t.id IN (SELECT track_id FROM plays WHERE is_control = 0 AND rating = {ADD})"
        " ORDER BY t.id").fetchall()
    return [Seed(*r) for r in rows]


def match_seeds(conn: sqlite3.Connection, yt: YouTubeMusic) -> list[Seed]:
    """Match seeds without a YouTube Music ID: songs search, same song, duration within tolerance."""
    out = []
    for seed in seeds(conn):
        if seed.video_id is None:
            target = names.song(seed.artist, seed.title)
            results = yt.search_songs(f"{seed.artist} {seed.title}")
            same = [r for r in results if r.duration_s and names.song(r.artist, r.title).same_song(target)]
            close = [r for r in same if abs(r.duration_s - seed.duration_s) <= TOLERANCE]
            best = min(close, key=lambda r: abs(r.duration_s - seed.duration_s), default=None)
            reason = None if best else ("no_name_match" if not same else "duration_mismatch")
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO seed_matches"
                    " (track_id, video_id, yt_artist, yt_title, yt_duration_s, reason) VALUES (?, ?, ?, ?, ?, ?)",
                    (seed.track_id, best and best.video_id, best and best.artist, best and best.title,
                     best and best.duration_s, reason))
            seed.video_id = best and best.video_id
            status = f"{best.video_id} ({best.artist} – {best.title}, {best.duration_s:.0f}s)" if best else reason
            print(f"  seed match: {seed.label} ({seed.duration_s:.0f}s) -> {status}")
        out.append(seed)
    return out


def optional(source_class):
    """A source that needs credentials, or None (with a note) when they aren't set."""
    try:
        return source_class()
    except RuntimeError as e:
        print(f"  {source_class.__name__} skipped: {e}")
        return None


@dataclass
class Candidate:
    id: int
    song: names.Song


# Shown tracks and pool tracks, for the "already rated/shown" and "already in pool" filters.
SHOWN = "(t.origin = 'original_reference' OR t.rating IS NOT NULL OR t.id IN (SELECT track_id FROM plays))"
UNPLAYED_POOL = ("c.status = 'pool' AND t.rating IS NULL AND t.id NOT IN (SELECT track_id FROM plays)")


class Pull:
    def __init__(self, conn: sqlite3.Connection, yt: YouTubeMusic, lastfm: LastFM | None,
                 deezer: Deezer | None = None, tidal: Tidal | None = None):
        self.conn, self.yt, self.lastfm, self.deezer, self.tidal = conn, yt, lastfm, deezer, tidal
        with conn:
            self.generation = conn.execute("INSERT INTO generations DEFAULT VALUES").lastrowid
        # Already rated or shown: originals, rated tracks and anything played.
        shown = conn.execute(f"SELECT t.artist, t.title, t.video_id, c.isrc FROM tracks t"
                             f" LEFT JOIN candidates c ON c.id = t.candidate_id WHERE {SHOWN}").fetchall()
        self.known = [names.song(r["artist"], r["title"]) for r in shown]
        self.known_video = {r["video_id"] for r in shown if r["video_id"]}
        self.known_isrc = {r["isrc"] for r in shown if r["isrc"]}
        # Already in the pool (downloaded, unplayed) from earlier generations.
        pool = conn.execute(
            "SELECT c.id, c.song_title, c.song_artists, c.is_extended, c.video_id, c.isrc FROM candidates c"
            f" JOIN tracks t ON t.id = c.track_id WHERE {UNPLAYED_POOL}").fetchall()
        self.pool = [self._candidate(r) for r in pool]
        self.pool_video = {r["video_id"]: r["id"] for r in pool if r["video_id"]}
        self.pool_isrc = {r["isrc"]: r["id"] for r in pool if r["isrc"]}
        self.current: dict[int, Candidate] = {}
        self.by_video: dict[str, int] = {}
        self.by_isrc: dict[str, int] = {}
        self.lookups: dict[str, dict] = {}       # per seed: was it found on Deezer / TIDAL

    @staticmethod
    def _candidate(r) -> Candidate:
        return Candidate(r["id"], names.Song(r["song_title"], frozenset(json.loads(r["song_artists"])),
                                             bool(r["is_extended"])))

    def run(self, seed_list: list[Seed]) -> int:
        started = time.monotonic()
        db.log_event(self.conn, "pull_start", generation=self.generation, seeds=len(seed_list))
        for seed in seed_list:
            for source, items in self._lists(seed):
                for rank, item in enumerate(items, 1):
                    self.process(seed, source, rank, item)

        stats = summary(self.conn, self.generation)
        stats["seed_lookups"] = self.lookups
        stats["candidate_phase_seconds"] = round(time.monotonic() - started, 1)
        stats["api"] = {name: src.stats.as_dict() for name, src in (
            ("ytmusic", self.yt), ("lastfm", self.lastfm), ("deezer", self.deezer), ("tidal", self.tidal)) if src}
        with self.conn:
            self.conn.execute("UPDATE generations SET finished_at = datetime('now'), stats = ? WHERE id = ?",
                              (json.dumps(stats), self.generation))
        db.log_event(self.conn, "pull_done", generation=self.generation, candidates=stats["candidates"],
                     seconds=stats["candidate_phase_seconds"], api=stats["api"])
        return self.generation

    def _lists(self, seed: Seed):
        """Each source's list for one seed, as (source, items). A failing source is logged and skipped."""
        look = self.lookups.setdefault(seed.label, {})

        def attempt(source: str, fetch, what: str):
            try:
                items = fetch()
            except Exception as e:  # logged per seed and source, the pull carries on
                look[source] = f"error: {str(e)[:120]}"
                print(f"  {source:9s} failed for {seed.label}: {e}")
                return []
            print(f"  {source:9s} {len(items):3d} {what} {seed.label}")
            return items

        if self.lastfm:
            yield "lastfm", attempt("lastfm", lambda: self.lastfm.similar_by_artist(seed.artist),
                                    "via similar artists of")
        if seed.video_id:
            yield "ytmusic", attempt("ytmusic", lambda: self.yt.radio(seed.video_id), "radio from")
            yield "ytrelated", attempt("ytrelated", lambda: self.yt.related(seed.video_id), "related to")
        else:
            look["ytmusic"] = "seed not matched on YouTube Music"
            print(f"  YouTube skipped for {seed.label}: seed not matched on YouTube Music")
        found = None
        if self.deezer:
            try:
                found = self.deezer.find(seed.artist, seed.title, seed.duration_s)
                look["deezer"] = "found" if found else "not found"
            except Exception as e:
                look["deezer"] = f"error: {str(e)[:120]}"
            if found:
                yield "deezer", attempt("deezer", lambda: self.deezer.artist_radio(found), "from artist radio of")
            else:
                print(f"  deezer      – {seed.label}: {look['deezer']}")
        if self.tidal:
            isrc = found and found.get("isrc")
            tidal_id = None
            if isrc:
                try:
                    tidal_id = self.tidal.find(isrc)
                    look["tidal"] = "found" if tidal_id else f"ISRC {isrc} not on TIDAL"
                except Exception as e:
                    look["tidal"] = f"error: {str(e)[:120]}"
            else:
                look["tidal"] = "no ISRC (seed not found on Deezer)"
            if tidal_id:
                yield "tidal", attempt("tidal", lambda: self.tidal.similar(tidal_id), "similar to")
            else:
                print(f"  tidal       – {seed.label}: {look['tidal']}")

    def process(self, seed: Seed, source: str, rank: int, item: Item) -> None:
        """Filter one raw suggestion. `item` is recorded as the source gave it."""
        song = names.song(item.artist, item.title)
        rec = lambda outcome, reason, cid=None: self._record(seed, source, rank, item, outcome, reason, cid)

        if item.isrc and item.isrc in self.known_isrc:
            return rec("dropped", "already_rated_or_shown_isrc")
        if (item.video_id and item.video_id in self.known_video) or any(song.same_song(k) for k in self.known):
            return rec("dropped", "already_rated_or_shown")
        in_pool = ((item.isrc and self.pool_isrc.get(item.isrc)) or (item.video_id and self.pool_video.get(item.video_id))
                   or next((c.id for c in self.pool if song.same_song(c.song)), None))
        if in_pool:
            return rec("merged", "already_in_pool", in_pool)

        if item.isrc and item.isrc in self.by_isrc:
            cid = self.by_isrc[item.isrc]
            return rec("merged", self._absorb(cid, item, song, "duplicate_isrc"), cid)
        cid = (item.video_id and self.by_video.get(item.video_id)) or self._find(song)
        if cid:
            return rec("merged", self._absorb(cid, item, song, "duplicate"), cid)

        with self.conn:
            cid = self.conn.execute(
                "INSERT INTO candidates (generation, video_id, artist, title, duration_s, song_title, song_artists,"
                " is_extended, isrc, release_date, matched, deezer_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.generation, item.video_id, item.artist, item.title, item.duration_s, song.title,
                 json.dumps(sorted(song.artists)), int(song.is_extended), item.isrc, item.release_date,
                 int(bool(item.video_id)), item.deezer_id)).lastrowid
        self.current[cid] = Candidate(cid, song)
        if item.video_id:
            self.by_video[item.video_id] = cid
        if item.isrc:
            self.by_isrc[item.isrc] = cid
        rec("new", None, cid)

    def _absorb(self, cid: int, item: Item, song: names.Song, reason: str) -> str:
        """Merge a duplicate suggestion into candidate `cid`: keep IDs it adds, prefer the Extended version.

        A YouTube ID is final (matched) unless the candidate is known in an Extended version and this ID
        isn't it; then the ID is kept as a fallback and the Extended version is searched for when selected.
        Returns the merge reason.
        """
        cand = self.current[cid]
        row = self.conn.execute("SELECT * FROM candidates WHERE id = ?", (cid,)).fetchone()
        upd = {k: v for k, v in (("isrc", item.isrc), ("release_date", item.release_date),
                                 ("deezer_id", item.deezer_id)) if v and not row[k]}
        if song.is_extended and not cand.song.is_extended:
            upd.update(artist=item.artist, title=item.title, duration_s=item.duration_s, is_extended=1,
                       video_id=item.video_id or row["video_id"], matched=int(bool(item.video_id)))
            cand.song = names.Song(cand.song.title, cand.song.artists | song.artists, True)
            reason = "other_version"
        elif item.video_id and not row["video_id"]:
            upd.update(video_id=item.video_id, matched=int(song.is_extended or not cand.song.is_extended))
        elif item.video_id and item.video_id != row["video_id"]:
            reason = "other_version"
        if upd:
            with self.conn:
                self.conn.execute(f"UPDATE candidates SET {', '.join(f'{k} = ?' for k in upd)} WHERE id = ?",
                                  (*upd.values(), cid))
        if item.video_id:
            self.by_video.setdefault(item.video_id, cid)
        if item.isrc:
            self.by_isrc.setdefault(item.isrc, cid)
        return reason

    def _find(self, song: names.Song) -> int | None:
        return next((c.id for c in self.current.values() if song.same_song(c.song)), None)

    def _record(self, seed, source, rank, item: Item, outcome, reason, cid=None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO suggestions (generation, seed_track_id, source, rank, artist, title, via,"
                " duration_s, video_id, outcome, reason, candidate_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.generation, seed.track_id, source, rank, item.artist, item.title, item.via,
                 item.duration_s, item.video_id, outcome, reason, cid))


class Matcher:
    """Readies a candidate picked for the pool (v3): Deezer details, YouTube Music match, late duplicates.

    Last.fm, Deezer and TIDAL suggestions are found on YouTube Music as in SPEC.md §4.1 step 4: same
    normalised song; duration within tolerance of the source's (Last.fm's from track.getInfo) when known,
    else the name match alone; an Extended version preferred if the search returned one. An ISRC or
    YouTube ID learnt here can reveal a duplicate of a shown track (dropped) or of another candidate
    (merged into it, provenance kept).
    """

    def __init__(self, conn: sqlite3.Connection, yt: YouTubeMusic, lastfm: LastFM | None, deezer: Deezer | None):
        self.conn, self.yt, self.lastfm, self.deezer = conn, yt, lastfm, deezer
        self.merged_into: int | None = None       # set when resolve() merged the candidate into another

    def resolve(self, cid: int) -> str | None:
        """None if the candidate is ready to download, else why not (its status is updated)."""
        self.merged_into = None
        c = self.conn.execute("SELECT * FROM candidates WHERE id = ?", (cid,)).fetchone()
        if c["deezer_id"] and not c["isrc"] and self.deezer:
            item = Item(c["artist"], c["title"], None, deezer_id=c["deezer_id"])
            try:
                self.deezer.details(item)
            except Exception as e:
                db.log_event(self.conn, "deezer_details_failed", candidate_id=cid, reason=str(e)[:200])
            with self.conn:
                self.conn.execute("UPDATE candidates SET isrc = coalesce(isrc, ?), release_date ="
                                  " coalesce(release_date, ?) WHERE id = ?", (item.isrc, item.release_date, cid))
            c = self.conn.execute("SELECT * FROM candidates WHERE id = ?", (cid,)).fetchone()
        if c["isrc"] and (why := self._duplicate(c, "isrc", c["isrc"])):
            return why
        if not c["matched"]:
            match, note = self._match_youtube(c)
            if match is None and not c["video_id"]:
                return self._set(cid, "match_failed", note)
            if match is not None:
                with self.conn:
                    self.conn.execute("UPDATE candidates SET video_id = ?, artist = ?, title = ?, duration_s = ?,"
                                      " matched = 1 WHERE id = ?",
                                      (match.video_id, match.artist, match.title, match.duration_s, cid))
            else:                                     # the Extended version wasn't found: use the known version
                with self.conn:
                    self.conn.execute("UPDATE candidates SET matched = 1 WHERE id = ?", (cid,))
            c = self.conn.execute("SELECT * FROM candidates WHERE id = ?", (cid,)).fetchone()
        return self._duplicate(c, "video_id", c["video_id"])

    def _duplicate(self, c, column: str, value: str) -> str | None:
        shown = self.conn.execute(f"SELECT 1 FROM tracks t JOIN candidates c ON c.id = t.candidate_id"
                                  f" WHERE c.{column} = ? AND c.id != ? AND {SHOWN}", (value, c["id"])).fetchone()
        if shown or (column == "video_id" and self.conn.execute(
                f"SELECT 1 FROM tracks t WHERE t.video_id = ? AND {SHOWN}", (value,)).fetchone()):
            return self._set(c["id"], "known", f"already_rated_or_shown_{column}")
        other = self.conn.execute(
            f"SELECT c.id FROM candidates c LEFT JOIN tracks t ON t.id = c.track_id WHERE c.{column} = ?"
            f" AND c.id != ? AND (({UNPLAYED_POOL}) OR (c.status = 'candidate' AND c.generation = ?))",
            (value, c["id"], c["generation"])).fetchone()
        if not other:
            return None
        reason = f"duplicate_{column}"
        with self.conn:
            self.conn.execute("UPDATE suggestions SET candidate_id = ?, outcome = 'merged', reason = ?"
                              " WHERE candidate_id = ?", (other["id"], reason, c["id"]))
            self.conn.execute("UPDATE candidates SET isrc = coalesce(isrc, ?), release_date = coalesce(release_date, ?)"
                              " WHERE id = ?", (c["isrc"], c["release_date"], other["id"]))
        self.merged_into = other["id"]
        return self._set(c["id"], "merged", f"{reason} (into candidate {other['id']})")

    def _set(self, cid: int, status: str, reason: str) -> str:
        with self.conn:
            self.conn.execute("UPDATE candidates SET status = ?, fail_reason = ? WHERE id = ?", (status, reason, cid))
        return f"{status}: {reason}"

    def _match_youtube(self, c) -> tuple[Item | None, str | None]:
        """(match, None) or (None, drop reason)."""
        target = names.song(c["artist"], c["title"])
        results = self.yt.search_songs(f"{c['artist']} {c['title']}")
        if not results:
            return None, "yt_match_failed:no_results"
        same = [r for r in results if r.duration_s and names.song(r.artist, r.title).same_song(target)]
        if not same:
            return None, "yt_match_failed:no_name_match"
        duration = c["duration_s"]
        if duration is None and self.lastfm:
            lf = self.conn.execute("SELECT artist, title FROM suggestions WHERE candidate_id = ? AND source = 'lastfm'"
                                   " LIMIT 1", (c["id"],)).fetchone()
            duration = lf and self.lastfm.duration(lf["artist"], lf["title"])
        extended = [r for r in same if names.song(r.artist, r.title).is_extended]
        if duration is None:
            return (extended or same)[0], None
        close = [r for r in same if abs(r.duration_s - duration) <= TOLERANCE]
        if not close:
            return None, "yt_match_failed:duration_mismatch"
        return (extended or sorted(close, key=lambda r: abs(r.duration_s - duration)))[0], None


def families(conn: sqlite3.Connection, candidate_ids) -> dict[int, set[str]]:
    """Source families (config [sources.families]) that suggested each candidate."""
    ids = list(candidate_ids)
    out: dict[int, set[str]] = {cid: set() for cid in ids}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for cid, src in conn.execute(f"SELECT DISTINCT candidate_id, source FROM suggestions WHERE candidate_id"
                                     f" IN ({','.join('?' * len(chunk))})", chunk):
            out[cid].add(FAMILIES.get(src, src))
    return out


def summary(conn: sqlite3.Connection, generation: int) -> dict:
    """Pool stats for SPEC.md §10: raw counts per seed/source, removals per reason, final size."""
    raw = {}
    for r in conn.execute(
            "SELECT t.artist || ' – ' || t.title AS seed, s.source, count(*) AS n FROM suggestions s"
            " JOIN tracks t ON t.id = s.seed_track_id WHERE s.generation = ? GROUP BY 1, 2", (generation,)):
        raw.setdefault(r["seed"], {})[r["source"]] = r["n"]
    reasons = Counter()
    for r in conn.execute("SELECT source, outcome, reason, count(*) AS n FROM suggestions"
                          " WHERE generation = ? AND reason IS NOT NULL GROUP BY 1, 2, 3", (generation,)):
        reasons[f"{r['source']}: {r['outcome']} — {r['reason']}"] += r["n"]
    seed_failures = [r[0] for r in conn.execute(
        "SELECT t.artist || ' – ' || t.title FROM seed_matches m JOIN tracks t ON t.id = m.track_id"
        " WHERE m.video_id IS NULL")]
    n = conn.execute("SELECT count(*) FROM candidates WHERE generation = ?", (generation,)).fetchone()[0]
    # Overlap between sources (agreement was the strongest signal in v1).
    combos = Counter()
    for (srcs,) in conn.execute(
            "SELECT group_concat(DISTINCT source) FROM (SELECT candidate_id, source FROM suggestions"
            " WHERE generation = ? AND candidate_id IS NOT NULL ORDER BY source) GROUP BY candidate_id", (generation,)):
        combos["+".join(sorted(srcs.split(",")))] += 1
    by_family = Counter()
    fams = families(conn, [r[0] for r in conn.execute("SELECT id FROM candidates WHERE generation = ?", (generation,))])
    for f in fams.values():
        by_family[len(f)] += 1
    artist_sources: dict[str, set] = {}
    for r in conn.execute("SELECT artist, source FROM suggestions WHERE generation = ?", (generation,)):
        for a in names.split_artists(r["artist"]):
            artist_sources.setdefault(a, set()).add(r["source"])
    artist_overlap = Counter(len(v) for v in artist_sources.values())
    return {"raw_suggestions": raw, "filter_reasons": dict(sorted(reasons.items())),
            "seed_match_failures": seed_failures, "candidates": n,
            "source_combinations": dict(sorted(combos.items(), key=lambda kv: -kv[1])),
            "candidates_by_family_count": {str(k): v for k, v in sorted(by_family.items())},
            "artists_by_source_count": {str(k): v for k, v in sorted(artist_overlap.items())}}


def reset(conn: sqlite3.Connection) -> None:
    """Development helper: forget all pulls, seed matches and pool tracks (cache files stay)."""
    with conn:
        pool = [r[0] for r in conn.execute("SELECT id FROM tracks WHERE origin = 'pool'")]
        for table in ("chunk_embeddings", "track_embeddings"):
            conn.executemany(f"DELETE FROM {table} WHERE track_id = ?", [(t,) for t in pool])
        conn.execute("UPDATE candidates SET track_id = NULL, pool_seed = NULL")
        for table in ("suggestions", "seed_matches"):      # before tracks: rated pool tracks can be seeds
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM tracks WHERE origin = 'pool'")
        for table in ("candidates", "generations"):
            conn.execute(f"DELETE FROM {table}")
