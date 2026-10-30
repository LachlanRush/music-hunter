"""Fill the pool from a generation's candidates — SPEC.md §4.1 steps 5–7, §16.4.

v3: every seed has its own share of the pool — proportional to its seed average, with a floor — so
a new or obscure seed that few sources know is as present as a well-known one. Within a seed's share,
candidates suggested by two or more source families go in first (most families first; with [pool]
multi_source_always_added they join even beyond the share), then the rest of the share is split
between that seed's lists by source average. Candidates are taken from each (seed, source) list in
the source's own order, matched on YouTube Music (pull.Matcher), downloaded one at a time with a
pause between downloads, loudness-measured and analysed with CLAP. A failed match or download is
replaced by the next candidate from the same list; a list that runs dry passes its leftover slots to
the same seed's other lists, and only a seed with no candidates left passes them to other seeds.
Lists are visited round-robin, so the first tracks ready span all seeds and sources.
"""

import json
import re
import sqlite3
import subprocess
import time
from collections import Counter, deque
from pathlib import Path

from . import audio, db
from .config import CONFIG, path, relative
from .ratings import SOURCES, seed_averages, source_averages
from .scoring import current_weights

CFG = CONFIG["pool"]


# --- quotas ----------------------------------------------------------------------------

def seed_shares(averages: dict[int, float]) -> dict[int, float]:
    """Shares proportional to average, each at least min_seed_share of an equal share."""
    floor = CFG["min_seed_share"] / len(averages)
    fixed: dict[int, float] = {}
    while True:
        free = {s: a for s, a in averages.items() if s not in fixed}
        left = 1.0 - sum(fixed.values())
        total = sum(free.values())
        shares = {s: left * a / total for s, a in free.items()}
        low = {s for s, v in shares.items() if v < floor}
        if not low:
            return {**fixed, **shares}
        fixed.update({s: floor for s in low})


def round_quotas(weights: dict, total: int) -> dict:
    """Largest-remainder rounding of total * weight (weights sum to 1)."""
    raw = {k: total * w for k, w in weights.items()}
    out = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - out[k], reverse=True)[:total - sum(out.values())]:
        out[k] += 1
    return out


def quotas(slots: int, seed_avgs: dict[int, float], source_avgs: dict[str, float],
           available: dict[tuple[int, str], int]) -> dict[tuple[int, str], int]:
    """Slots per (seed, source). A source with no candidates for a seed gets no share of it."""
    weights = {}
    for seed, share in seed_shares(seed_avgs).items():
        srcs = [s for s in SOURCES if available.get((seed, s))] or list(SOURCES)
        total = sum(source_avgs[s] for s in srcs)
        for s in srcs:
            weights[(seed, s)] = share * source_avgs[s] / total
    return round_quotas(weights, slots)


def buckets(conn: sqlite3.Connection, generation: int) -> dict[tuple[int, str], list[int]]:
    """Unprocessed candidates suggested by each (seed, source), in the source's order."""
    out: dict[tuple[int, str], list[int]] = {}
    for r in conn.execute(
            "SELECT s.seed_track_id, s.source, s.candidate_id FROM suggestions s"
            " JOIN candidates c ON c.id = s.candidate_id"
            " WHERE s.generation = ? AND c.generation = ? AND c.status = 'candidate'"
            " ORDER BY s.seed_track_id, s.source, s.rank", (generation, generation)):
        lst = out.setdefault((r[0], r[1]), [])
        if r[2] not in lst:
            lst.append(r[2])
    return out


# --- download, loudness, analysis --------------------------------------------------------

def download(video_id: str) -> Path:
    """Full audio with yt-dlp into cache/, original container (no re-encode)."""
    import yt_dlp

    cache = path("cache")
    cache.mkdir(exist_ok=True)
    opts = {
        "format": CFG["download_format"],
        "outtmpl": str(cache / "%(id)s.%(ext)s"),
        "js_runtimes": {"node": {}},
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://music.youtube.com/watch?v={video_id}", download=True)
    return Path(info["requested_downloads"][0]["filepath"])


def loudness(file: Path) -> tuple[float, float, float]:
    """(integrated LUFS, true peak dBTP, playback gain dB) via ffmpeg's EBU R128 filter."""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(file),
                          "-af", "ebur128=peak=true", "-f", "null", "-"],
                         capture_output=True, text=True, check=True).stderr
    summary = out[out.rfind("Summary:"):]
    lufs = float(re.search(r"I:\s+(-?[\d.]+|-inf) LUFS", summary).group(1))
    peak = float(re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", summary).group(1))
    cfg = CONFIG["loudness"]
    gain = min(cfg["target_lufs"] - lufs, cfg["max_true_peak_db"] - peak)
    return lufs, peak, gain


class Filler:
    def __init__(self, conn: sqlite3.Connection, generation: int):
        from .clap import Embedder

        from .pull import Matcher, optional
        from .sources import Deezer, LastFM, YouTubeMusic

        self.conn, self.generation = conn, generation
        self.embedder = Embedder()
        self.yt = YouTubeMusic()
        self.matcher = Matcher(conn, self.yt, optional(LastFM), Deezer())
        self.unusable: list[str] = []
        self.last_download = 0.0
        self.ready = 0
        self.failed: list[str] = []

    def process(self, cid: int, seed: int | None = None) -> bool:
        """Match, download, measure and analyse one candidate for `seed`'s share. True if it joined the pool."""
        if self.conn.execute("SELECT status FROM candidates WHERE id = ?", (cid,)).fetchone()[0] != "candidate":
            return False                      # already handled, e.g. merged into or as a duplicate
        try:
            why = self.matcher.resolve(cid)
        except Exception as e:  # a source API failing: treat like a failed download, try the next
            why = self.matcher._set(cid, "match_failed", f"error: {str(e)[:200]}")
        if why:
            c = self.conn.execute("SELECT artist, title FROM candidates WHERE id = ?", (cid,)).fetchone()
            self.unusable.append(f"{c['artist']} – {c['title']}: {why}")
            db.log_event(self.conn, "candidate_unusable", generation=self.generation, candidate_id=cid, reason=why)
            print(f"  skipped {c['artist']} – {c['title']}: {why}")
            # Same song as a candidate not yet tried (now with this one's provenance): it takes the slot.
            return bool(self.matcher.merged_into) and self.process(self.matcher.merged_into, seed)
        c = self.conn.execute("SELECT * FROM candidates WHERE id = ?", (cid,)).fetchone()
        wait = CFG["download_delay_seconds"] - (time.monotonic() - self.last_download)
        if wait > 0:
            time.sleep(wait)
        t0 = time.monotonic()
        try:
            file = download(c["video_id"])
        except Exception as e:  # yt-dlp raises DownloadError and friends
            return self._fail(c, f"download: {e}", download_s=time.monotonic() - t0)
        finally:
            self.last_download = time.monotonic()
        t1 = time.monotonic()
        try:
            lufs, peak, gain = loudness(file)
            sr = CONFIG["clap"]["sample_rate"]
            samples = audio.decode(file, sr)
            emb = self.embedder.embed_track(samples)
        except Exception as e:
            return self._fail(c, f"analysis: {e}", download_s=t1 - t0, analysis_s=time.monotonic() - t1)
        try:
            from .bpm import estimate, seeds_median
            if not hasattr(self, "bpm_prior"):
                self.bpm_prior = seeds_median(self.conn)      # 3:2 tie-break: prefer tempos near the seeds'
            tempo = estimate(samples, sr, self.bpm_prior)
        except Exception as e:
            tempo = None
            db.log_event(self.conn, "bpm_failed", generation=self.generation, video_id=c["video_id"], reason=str(e)[:200])
        t2 = time.monotonic()
        art, published = self._artwork(c["video_id"])

        with self.conn:
            tid = self.conn.execute(
                "INSERT INTO tracks (artist, title, file_path, duration_s, origin, candidate_id, generation,"
                " video_id, loudness_lufs, true_peak_db, gain_db, art_path, release_date, bpm)"
                " VALUES (?, ?, ?, ?, 'pool', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (c["artist"], c["title"], relative(file), len(samples) / sr, cid,
                 self.generation, c["video_id"], lufs, peak, gain, art and relative(art),
                 c["release_date"] or published, tempo)).lastrowid
            db.store_embedding(self.conn, tid, self.embedder.model_name, emb)
            self.conn.execute("UPDATE candidates SET status = 'pool', track_id = ?, pool_seed = ? WHERE id = ?",
                              (tid, seed, cid))
        self.ready += 1
        db.log_event(self.conn, "track_ready", generation=self.generation, track_id=tid, video_id=c["video_id"],
                     download_s=round(t1 - t0, 2), analysis_s=round(t2 - t1, 2), waited_s=round(max(wait, 0), 2),
                     bytes=file.stat().st_size, duration_s=round(len(samples) / sr, 1), chunks=len(emb.chunks),
                     bpm=tempo)
        print(f"  [{self.ready:2d}] {c['artist']} – {c['title']}  "
              f"({len(samples) / sr / 60:.1f} min, {len(emb.chunks)} chunks, {lufs:.1f} LUFS, gain {gain:+.1f} dB)")
        return True

    def _artwork(self, video_id: str):
        from . import artwork

        try:
            return artwork.fetch(self.yt, video_id)
        except Exception as e:
            db.log_event(self.conn, "art_failed", generation=self.generation, video_id=video_id, reason=str(e)[:200])
            return None, None

    def _fail(self, c, reason: str, **timings) -> bool:
        reason = re.sub(r"\x1b\[[0-9;]*m", "", reason).splitlines()[0][:300]   # drop yt-dlp's colour codes
        db.log_event(self.conn, "track_failed", generation=self.generation, video_id=c["video_id"], reason=reason,
                     **{k: round(v, 2) for k, v in timings.items()})
        with self.conn:
            self.conn.execute("UPDATE candidates SET status = 'download_failed', fail_reason = ? WHERE id = ?",
                              (reason, c["id"]))
        self.failed.append(f"{c['artist']} – {c['title']}: {reason}")
        print(f"  FAILED {c['artist']} – {c['title']}: {reason}")
        return False


def unrated_pool_size(conn: sqlite3.Connection) -> int:
    """Pool tracks not yet shown (rated or skipped tracks have left the pool)."""
    return conn.execute("SELECT count(*) FROM tracks WHERE origin = 'pool' AND rating IS NULL"
                        " AND id NOT IN (SELECT track_id FROM plays)").fetchone()[0]


def multi_source_order(conn: sqlite3.Connection, cids) -> list[int]:
    """Candidates suggested by 2+ source families: most families first, then most (seed, source)
    suggestions, then best position in any list."""
    from .pull import families

    fams = families(conn, cids)
    multi = [c for c, f in fams.items() if len(f) >= 2]
    if not multi:
        return []
    marks = ",".join("?" * len(multi))
    detail = {r[0]: (r[1], r[2]) for r in conn.execute(
        f"SELECT candidate_id, count(DISTINCT seed_track_id || source), min(rank) FROM suggestions"
        f" WHERE candidate_id IN ({marks}) GROUP BY candidate_id", multi)}
    return sorted(multi, key=lambda c: (-len(fams[c]), -detail[c][0], detail[c][1], c))


def multi_source_unplayed(conn: sqlite3.Connection) -> int:
    """Unplayed pool tracks suggested by 2+ source families (v3 refill trigger)."""
    from .pull import families

    cids = [r[0] for r in conn.execute("SELECT candidate_id FROM tracks WHERE origin = 'pool' AND rating IS NULL"
                                       " AND id NOT IN (SELECT track_id FROM plays)")]
    return sum(1 for f in families(conn, cids).values() if len(f) >= 2)


def seed_targets(conn: sqlite3.Connection, seed_ids: list[int]) -> dict[int, int]:
    """Each seed's share of target_size (by seed average, with the min_seed_share floor)."""
    return round_quotas(seed_shares(seed_averages(conn, seed_ids)), CFG["target_size"])


def seed_pool_counts(conn: sqlite3.Connection) -> dict[int, int]:
    """Unplayed pool tracks per seed share (tracks from before shares existed count for their first seed)."""
    out: dict[int, int] = {}
    for (seed,) in conn.execute(
            "SELECT coalesce(c.pool_seed, (SELECT min(seed_track_id) FROM suggestions s WHERE s.candidate_id = c.id))"
            " FROM tracks t JOIN candidates c ON c.id = t.candidate_id WHERE t.origin = 'pool' AND t.rating IS NULL"
            " AND t.id NOT IN (SELECT track_id FROM plays)"):
        out[seed] = out.get(seed, 0) + 1
    return out


def fill(conn: sqlite3.Connection, generation: int) -> dict:
    """Fill every seed's share of the pool from this generation's candidates. Safe to re-run (resumes)."""
    from .pull import seeds

    started = time.monotonic()
    queues = {b: deque(cids) for b, cids in buckets(conn, generation).items()}
    seed_ids = [s.track_id for s in seeds(conn)]
    target = seed_targets(conn, seed_ids) if seed_ids else {}
    have = seed_pool_counts(conn)
    tried: set[int] = set()
    filler = None
    get_filler = lambda: filler or Filler(conn, generation)

    # 1. Multi-source candidates, each counted for the suggesting seed whose share is least full.
    suggesters: dict[int, set[int]] = {}
    for (seed, _), q in queues.items():
        for cid in q:
            suggesters.setdefault(cid, set()).add(seed)
    multi = multi_source_order(conn, list(suggesters)) if CFG["multi_source_first"] else []
    multi_added: dict[int, int] = {}
    if multi:
        print(f"\nMulti-source candidates first: {len(multi)}")
        db.log_event(conn, "fill_multi_start", generation=generation, candidates=len(multi))
    for cid in multi:
        options = [s for s in suggesters[cid] if s in target]
        if not CFG["multi_source_always_added"]:
            options = [s for s in options if have.get(s, 0) < target[s]]
        if not options:
            continue
        seed = min(options, key=lambda s: (have.get(s, 0) / max(target[s], 1), s))
        filler = get_filler()
        tried.add(cid)
        if filler.process(cid, seed):
            have[seed] = have.get(seed, 0) + 1
            multi_added[seed] = multi_added.get(seed, 0) + 1

    # 2. The rest of each seed's share, split between its lists by source average.
    src_avgs = source_averages(conn)
    quota: dict[tuple[int, str], int] = {}
    for seed in target:
        need = max(target[seed] - have.get(seed, 0), 0)
        lists = [b for b in queues if b[0] == seed and any(c not in tried for c in queues[b])]
        if need and lists:
            total = sum(src_avgs[src] for _, src in lists)
            quota.update(round_quotas({b: src_avgs[b[1]] / total for b in lists}, need))
        elif need:
            quota[(seed, None)] = need                    # no candidates left: passed to other seeds below
    slots = sum(quota.values())
    print(f"\nFilling pool: {slots} slots across {len(target)} seed shares")
    db.log_event(conn, "fill_start", generation=generation, slots=slots, multi_source_added=sum(multi_added.values()))
    remaining = {b: n for b, n in quota.items() if b[1] is not None}
    orphan = sum(n for b, n in quota.items() if b[1] is None)
    order = sorted(remaining)  # each round takes one from every list
    while slots and (sum(remaining.values()) or orphan):
        spare: dict[int | None, int] = {None: orphan} if orphan else {}
        orphan = 0
        for b in order:
            if remaining[b] == 0:
                continue
            q = queues[b]
            while q and q[0] in tried:
                q.popleft()
            if not q:
                spare[b[0]] = spare.get(b[0], 0) + remaining[b]
                remaining[b] = 0
                continue
            cid = q.popleft()
            tried.add(cid)
            filler = get_filler()
            if filler.process(cid, b[0]):
                remaining[b] -= 1
        # Leftover slots go to the same seed's lists that still have candidates (biggest quota first);
        # only a seed with nothing left passes them to other seeds' lists.
        open_lists = [b for b in queues if any(c not in tried for c in queues[b])]
        lost = 0
        for seed, n in spare.items():
            own = sorted((b for b in open_lists if b[0] == seed), key=lambda b: quota.get(b, 0), reverse=True)
            pool_ = own or sorted(open_lists, key=lambda b: quota.get(b, 0), reverse=True)
            if not pool_:
                lost += n
                continue
            for i in range(n):
                b = pool_[i % len(pool_)]
                remaining[b] = remaining.get(b, 0) + 1
                if b not in order:
                    order.append(b)
        if lost:
            print(f"  Out of candidates: {lost} slots unfilled")
        if not open_lists:
            break

    if unrated_pool_size(conn) >= CFG["target_size"]:
        with conn:
            conn.execute("UPDATE candidates SET status = 'not_selected' WHERE generation = ? AND status = 'candidate'",
                         (generation,))

    label = {r["id"]: f"{r['artist']} – {r['title']}" for r in conn.execute("SELECT id, artist, title FROM tracks")}
    stats = json.loads(conn.execute("SELECT stats FROM generations WHERE id = ?", (generation,)).fetchone()[0] or "{}")
    unusable = Counter()
    for r in conn.execute("SELECT status, fail_reason FROM candidates WHERE generation = ?"
                          " AND status IN ('match_failed', 'merged', 'known')", (generation,)):
        unusable[f"{r[0]}: {r[1].split(' (into')[0]}"] += 1
    stats.update({
        "multi_source_candidates": len(multi),
        "multi_source_added": stats.get("multi_source_added", 0) + sum(multi_added.values()),
        "seed_pools": {label[s]: {"target": target[s], "unplayed": seed_pool_counts(conn).get(s, 0),
                                  "multi_source_added": multi_added.get(s, 0)} for s in target},
        "unusable_at_selection": dict(unusable.most_common()),
        "seed_averages": {label[s]: a for s, a in seed_averages(conn, list(target)).items()},
        "source_averages": src_avgs,
        "quotas": {f"{label[s]} / {src or 'no candidates left'}": n for (s, src), n in quota.items()},
        "download_failures": stats.get("download_failures", []) + (filler.failed if filler else []),
        "pool_size": unrated_pool_size(conn),
        "weights": current_weights(conn)[1],
        "fill_seconds": round(stats.get("fill_seconds", 0) + time.monotonic() - started, 1),
    })
    with conn:
        conn.execute("UPDATE generations SET stats = ? WHERE id = ?", (json.dumps(stats), generation))
    db.log_event(conn, "fill_done", generation=generation, ready=filler.ready if filler else 0,
                 failed=len(filler.failed) if filler else 0, seconds=round(time.monotonic() - started, 1),
                 pool_size=stats["pool_size"])
    return stats
