"""Fill the pool from a generation's candidates — SPEC.md §4.1 steps 5–7 (scoring is Milestone 4).

Quotas give each seed a share by seed average (with a floor), split between sources by
source average. Candidates are taken from each (seed, source) list in the source's own
order, downloaded one at a time with a pause between downloads, loudness-measured and
analysed with CLAP. A failed download is replaced by the next candidate from the same
list; a list that runs dry passes its leftover slots to lists that still have candidates.
Buckets are visited round-robin, so the first tracks ready span all seeds and sources.
"""

import json
import re
import sqlite3
import subprocess
import time
from collections import deque
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

        self.conn, self.generation = conn, generation
        self.embedder = Embedder()
        self.last_download = 0.0
        self.ready = 0
        self.failed: list[str] = []

    def process(self, cid: int) -> bool:
        """Download, measure and analyse one candidate. True if it joined the pool."""
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
        t2 = time.monotonic()

        with self.conn:
            tid = self.conn.execute(
                "INSERT INTO tracks (artist, title, file_path, duration_s, origin, candidate_id, generation,"
                " video_id, loudness_lufs, true_peak_db, gain_db) VALUES (?, ?, ?, ?, 'pool', ?, ?, ?, ?, ?, ?)",
                (c["artist"], c["title"], relative(file), len(samples) / sr, cid,
                 self.generation, c["video_id"], lufs, peak, gain)).lastrowid
            db.store_embedding(self.conn, tid, self.embedder.model_name, emb)
            self.conn.execute("UPDATE candidates SET status = 'pool', track_id = ? WHERE id = ?", (tid, cid))
        self.ready += 1
        db.log_event(self.conn, "track_ready", generation=self.generation, track_id=tid, video_id=c["video_id"],
                     download_s=round(t1 - t0, 2), analysis_s=round(t2 - t1, 2), waited_s=round(max(wait, 0), 2),
                     bytes=file.stat().st_size, duration_s=round(len(samples) / sr, 1), chunks=len(emb.chunks))
        print(f"  [{self.ready:2d}] {c['artist']} – {c['title']}  "
              f"({len(samples) / sr / 60:.1f} min, {len(emb.chunks)} chunks, {lufs:.1f} LUFS, gain {gain:+.1f} dB)")
        return True

    def _fail(self, c, reason: str, **timings) -> bool:
        reason = reason.splitlines()[0][:300]
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


def fill(conn: sqlite3.Connection, generation: int) -> dict:
    """Fill the pool to target_size from this generation's candidates. Safe to re-run (resumes)."""
    slots = CFG["target_size"] - unrated_pool_size(conn)
    queues = {b: deque(cids) for b, cids in buckets(conn, generation).items()}
    seed_ids = sorted({seed for seed, _ in queues})
    s_avgs, src_avgs = seed_averages(conn, seed_ids), source_averages(conn)
    quota = quotas(max(slots, 0), s_avgs, src_avgs, {b: len(q) for b, q in queues.items()}) if seed_ids else {}
    print(f"\nFilling pool: {max(slots, 0)} slots across {len(quota)} (seed, source) lists")

    started = time.monotonic()
    db.log_event(conn, "fill_start", generation=generation, slots=max(slots, 0))
    remaining = {b: quota.get(b, 0) for b in queues}
    tried: set[int] = set()
    filler = Filler(conn, generation) if slots > 0 else None
    order = sorted(queues)  # each round takes one from every list
    while filler and sum(remaining.values()) > 0:
        spare = 0
        for b in order:
            if remaining[b] == 0:
                continue
            q = queues[b]
            while q and q[0] in tried:
                q.popleft()
            if not q:
                spare += remaining[b]
                remaining[b] = 0
                continue
            cid = q.popleft()
            tried.add(cid)
            if filler.process(cid):
                remaining[b] -= 1
        # Leftover slots from exhausted lists go to lists with candidates, biggest quota first.
        open_lists = sorted((b for b in order if any(c not in tried for c in queues[b])),
                            key=lambda b: quota.get(b, 0), reverse=True)
        if spare and not open_lists:
            print(f"  Out of candidates: {spare} slots unfilled")
            break
        for i in range(spare):
            remaining[open_lists[i % len(open_lists)]] += 1

    if unrated_pool_size(conn) >= CFG["target_size"]:
        with conn:
            conn.execute("UPDATE candidates SET status = 'not_selected' WHERE generation = ? AND status = 'candidate'",
                         (generation,))

    label = {r["id"]: f"{r['artist']} – {r['title']}" for r in conn.execute("SELECT id, artist, title FROM tracks")}
    stats = json.loads(conn.execute("SELECT stats FROM generations WHERE id = ?", (generation,)).fetchone()[0] or "{}")
    stats.update({
        "seed_averages": {label[s]: a for s, a in s_avgs.items()},
        "source_averages": src_avgs,
        "quotas": {f"{label[s]} / {src}": n for (s, src), n in sorted(quota.items())},
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
