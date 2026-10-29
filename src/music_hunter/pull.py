"""One pull ("generation") of candidates — SPEC.md §4.1 steps 1–4.

Seeds are matched to YouTube Music, both sources are queried for every seed, and each
raw suggestion is filtered and either becomes a candidate, is merged into one
(keeping its provenance), or is dropped with a reason.
"""

import json
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass

from . import db, names
from .config import CONFIG
from .ratings import ADD
from .sources import Deezer, Item, LastFM, YouTubeMusic

TOLERANCE = CONFIG["sources"]["duration_tolerance_seconds"]


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


@dataclass
class Candidate:
    id: int
    song: names.Song


class Pull:
    def __init__(self, conn: sqlite3.Connection, yt: YouTubeMusic, lastfm: LastFM | None,
                 deezer: Deezer | None = None):
        self.conn, self.yt, self.lastfm, self.deezer = conn, yt, lastfm, deezer
        with conn:
            self.generation = conn.execute("INSERT INTO generations DEFAULT VALUES").lastrowid
        # Already rated or shown: originals, rated tracks and anything played.
        self.known = [names.song(r["artist"], r["title"]) for r in conn.execute(
            "SELECT artist, title FROM tracks WHERE origin = 'original_reference' OR rating IS NOT NULL"
            " OR id IN (SELECT track_id FROM plays)")]
        # Already in the pool (downloaded, unrated) from earlier generations.
        self.pool = [self._candidate(r) for r in conn.execute(
            "SELECT c.id, c.song_title, c.song_artists, c.is_extended FROM candidates c"
            " JOIN tracks t ON t.id = c.track_id WHERE c.status = 'pool' AND t.rating IS NULL"
            " AND t.id NOT IN (SELECT track_id FROM plays)")]
        # Same by ISRC (exact): known/shown tracks and the pool, where their ISRC is known.
        self.known_isrc = {r[0] for r in conn.execute(
            "SELECT c.isrc FROM candidates c JOIN tracks t ON t.id = c.track_id WHERE c.isrc IS NOT NULL"
            " AND (t.rating IS NOT NULL OR t.id IN (SELECT track_id FROM plays))")}
        self.pool_isrc = {r[0]: r[1] for r in conn.execute(
            "SELECT c.isrc, c.id FROM candidates c JOIN tracks t ON t.id = c.track_id WHERE c.isrc IS NOT NULL"
            " AND c.status = 'pool' AND t.rating IS NULL AND t.id NOT IN (SELECT track_id FROM plays)")}
        self.current: list[Candidate] = []
        self.by_video: dict[str, int] = {}
        self.by_isrc: dict[str, int] = {}

    @staticmethod
    def _candidate(r) -> Candidate:
        return Candidate(r["id"], names.Song(r["song_title"], frozenset(json.loads(r["song_artists"])),
                                             bool(r["is_extended"])))

    def run(self, seed_list: list[Seed]) -> int:
        started = time.monotonic()
        db.log_event(self.conn, "pull_start", generation=self.generation, seeds=len(seed_list))
        for seed in seed_list:
            if self.lastfm:
                try:
                    items = self.lastfm.similar_by_artist(seed.artist)
                except Exception as e:  # logged per seed, the pull carries on
                    items = []
                    print(f"  Last.fm failed for {seed.label}: {e}")
                print(f"  Last.fm  {len(items):3d} via similar artists of {seed.label}")
                for rank, item in enumerate(items, 1):
                    self.process(seed, "lastfm", rank, item)
            if seed.video_id:
                items = self.yt.radio(seed.video_id)
                print(f"  YT radio {len(items):3d} from {seed.label}")
                for rank, item in enumerate(items, 1):
                    self.process(seed, "ytmusic", rank, item)
            else:
                print(f"  YT radio skipped for {seed.label}: seed not matched on YouTube Music")
            if self.deezer:
                try:
                    found = self.deezer.find(seed.artist, seed.title, seed.duration_s)
                    items = self.deezer.artist_radio(found) if found else []
                except Exception as e:
                    found, items = True, []
                    print(f"  Deezer failed for {seed.label}: {e}")
                print(f"  Deezer   {len(items):3d} from artist radio of {seed.label}"
                      + ("" if found else " (seed not found on Deezer)"))
                for rank, item in enumerate(items, 1):
                    self.process(seed, "deezer", rank, item)

        stats = summary(self.conn, self.generation)
        stats["candidate_phase_seconds"] = round(time.monotonic() - started, 1)
        stats["api"] = {"ytmusic": self.yt.stats.as_dict(), "lastfm": self.lastfm.stats.as_dict() if self.lastfm else None,
                        "deezer": self.deezer.stats.as_dict() if self.deezer else None}
        with self.conn:
            self.conn.execute("UPDATE generations SET finished_at = datetime('now'), stats = ? WHERE id = ?",
                              (json.dumps(stats), self.generation))
        db.log_event(self.conn, "pull_done", generation=self.generation, candidates=stats["candidates"],
                     seconds=stats["candidate_phase_seconds"], api=stats["api"])
        return self.generation

    def process(self, seed: Seed, source: str, rank: int, item: Item) -> None:
        """Filter one raw suggestion. `item` is recorded as the source gave it."""
        song = names.song(item.artist, item.title)
        rec = lambda outcome, reason, cid=None, chosen=item: self._record(
            seed, source, rank, item, chosen.video_id, outcome, reason, cid)

        if any(song.same_song(k) for k in self.known):
            return rec("dropped", "already_rated_or_shown")
        for c in self.pool:
            if song.same_song(c.song):
                return rec("merged", "already_in_pool", c.id)

        if source == "deezer":
            self.deezer.details(item)              # ISRC and release date
            if item.isrc in self.known_isrc:
                return rec("dropped", "already_rated_or_shown_isrc")
            if item.isrc in self.pool_isrc:
                return rec("merged", "already_in_pool_isrc", self.pool_isrc[item.isrc])

        chosen, match_note = item, None
        existing = (self._find(song) or (item.video_id and self.by_video.get(item.video_id))
                    or (item.isrc and self.by_isrc.get(item.isrc)))
        if not existing and source in ("lastfm", "deezer"):
            matched, reason = self._match_youtube(item, song, source)
            if not matched:
                return rec("dropped", reason)
            matched.isrc, matched.release_date = item.isrc, item.release_date
            chosen, match_note = matched, reason
            song = names.song(chosen.artist, chosen.title)
            existing = self.by_video.get(chosen.video_id) or self._find(song)
        if existing:
            by_isrc = bool(item.isrc) and self.by_isrc.get(item.isrc) == existing
            self._add_ids(existing, item)
            return rec("merged", "duplicate_isrc" if by_isrc else self._merge(chosen, song, existing),
                       existing, chosen)

        with self.conn:
            cid = self.conn.execute(
                "INSERT INTO candidates (generation, video_id, artist, title, duration_s,"
                " song_title, song_artists, is_extended, isrc, release_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.generation, chosen.video_id, chosen.artist, chosen.title, chosen.duration_s,
                 song.title, json.dumps(sorted(song.artists)), int(song.is_extended),
                 item.isrc, item.release_date)).lastrowid
        self.current.append(Candidate(cid, song))
        self.by_video[chosen.video_id] = cid
        if item.isrc:
            self.by_isrc[item.isrc] = cid
        rec("new", match_note, cid, chosen)

    def _add_ids(self, cid: int, item: Item) -> None:
        """Keep an ISRC or release date learnt from a later source for an existing candidate."""
        if item.isrc or item.release_date:
            with self.conn:
                self.conn.execute("UPDATE candidates SET isrc = coalesce(isrc, ?), release_date ="
                                  " coalesce(release_date, ?) WHERE id = ?", (item.isrc, item.release_date, cid))
            if item.isrc:
                self.by_isrc.setdefault(item.isrc, cid)

    def _find(self, song: names.Song) -> int | None:
        return next((c.id for c in self.current if song.same_song(c.song)), None)

    def _merge(self, item: Item, song: names.Song, cid: int) -> str:
        """Duplicate of a candidate in this pull: prefer the Extended version. Returns the reason."""
        cand = next(c for c in self.current if c.id == cid)
        row = self.conn.execute("SELECT video_id FROM candidates WHERE id = ?", (cid,)).fetchone()
        if not item.video_id or item.video_id == row["video_id"]:
            return "duplicate"
        if song.is_extended and not cand.song.is_extended:
            with self.conn:
                self.conn.execute(
                    "UPDATE candidates SET video_id=?, artist=?, title=?, duration_s=?, is_extended=1"
                    " WHERE id=?", (item.video_id, item.artist, item.title, item.duration_s, cid))
            cand.song = names.Song(cand.song.title, cand.song.artists | song.artists, True)
            self.by_video[item.video_id] = cid
        return "other_version"

    def _match_youtube(self, item: Item, song: names.Song, source: str) -> tuple[Item | None, str | None]:
        """Find a Last.fm or Deezer suggestion on YouTube Music (SPEC.md §4.1 step 4).

        Same normalised song required; duration within tolerance of the source's (Deezer gives
        one; Last.fm's comes from track.getInfo) when known, otherwise the name match alone is
        accepted. Prefers an Extended version of the same song if the search already returned one.
        Returns (match, note) where note is a drop reason, or 'name_only_match' on success.
        """
        results = self.yt.search_songs(f"{item.artist} {item.title}")
        if not results:
            return None, "yt_match_failed:no_results"
        same = [r for r in results if r.duration_s and names.song(r.artist, r.title).same_song(song)]
        if not same:
            return None, "yt_match_failed:no_name_match"
        if source == "lastfm":
            item.duration_s = self.lastfm.duration(item.artist, item.title)
        extended = [r for r in same if names.song(r.artist, r.title).is_extended]
        if item.duration_s is None:
            return (extended or same)[0], "name_only_match"
        close = [r for r in same if abs(r.duration_s - item.duration_s) <= TOLERANCE]
        if not close:
            return None, "yt_match_failed:duration_mismatch"
        return (extended or sorted(close, key=lambda r: abs(r.duration_s - item.duration_s)))[0], None

    def _record(self, seed, source, rank, item: Item, video_id, outcome, reason, cid=None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO suggestions (generation, seed_track_id, source, rank, artist, title, via,"
                " duration_s, video_id, outcome, reason, candidate_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.generation, seed.track_id, source, rank, item.artist, item.title, item.via,
                 item.duration_s, video_id, outcome, reason, cid))


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
    artist_sources: dict[str, set] = {}
    for r in conn.execute("SELECT artist, source FROM suggestions WHERE generation = ?", (generation,)):
        for a in names.split_artists(r["artist"]):
            artist_sources.setdefault(a, set()).add(r["source"])
    artist_overlap = Counter(len(v) for v in artist_sources.values())
    return {"raw_suggestions": raw, "filter_reasons": dict(sorted(reasons.items())),
            "seed_match_failures": seed_failures, "candidates": n,
            "source_combinations": dict(sorted(combos.items(), key=lambda kv: -kv[1])),
            "artists_by_source_count": {str(k): v for k, v in sorted(artist_overlap.items())}}


def reset(conn: sqlite3.Connection) -> None:
    """Development helper: forget all pulls, seed matches and pool tracks (cache files stay)."""
    with conn:
        pool = [r[0] for r in conn.execute("SELECT id FROM tracks WHERE origin = 'pool'")]
        for table in ("chunk_embeddings", "track_embeddings"):
            conn.executemany(f"DELETE FROM {table} WHERE track_id = ?", [(t,) for t in pool])
        conn.execute("UPDATE candidates SET track_id = NULL")
        conn.execute("DELETE FROM tracks WHERE origin = 'pool'")
        for table in ("suggestions", "candidates", "generations", "seed_matches"):
            conn.execute(f"DELETE FROM {table}")
