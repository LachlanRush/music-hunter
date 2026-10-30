"""Step 1: the dataset — every rated play from v1 and v2, its facts, CLAP features and audio.

    uv run python build.py            # dataset.json + features/clap.npz, then fetch missing audio

Audio deleted after rating (v1 1–2s, v2 non-Adds) is re-downloaded from the stored
YouTube IDs into runs/taste-test/audio/, paced. Seeds and kept files are reused in place.
"""

import json
import sqlite3
import time

import numpy as np

from common import AUDIO, CFG, OUT, RUNS, save_features


def percentile_ranks(values: list[float]) -> list[float]:
    """Rank within the run, scaled 0–1, ties sharing their average rank (puts v1 1–5 and v2 1–4 on one scale)."""
    order = np.argsort(values, kind="stable")
    ranks = np.empty(len(values))
    ranks[order] = np.arange(len(values))
    vals = np.array(values)
    for v in set(values):
        ranks[vals == v] = ranks[vals == v].mean()
    return list(ranks / max(1, len(values) - 1))


def build() -> list[dict]:
    from music_hunter.names import split_artists

    records, clap_track, clap_chunks = [], [], []
    for run, db_path, root, liked_at in RUNS:
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        cols = {x[1] for x in c.execute("PRAGMA table_info(tracks)")}
        opt = lambda col: f"t.{col}" if col in cols else f"NULL AS {col}"   # v1 predates these columns
        rows = c.execute(
            "SELECT p.id AS play_id, p.rating, p.is_control, p.components, p.play_number, t.id AS track_id,"
            f" t.artist, t.title, t.video_id, t.file_path, {opt('release_date')}, {opt('bpm')}, t.candidate_id, e.embedding"
            " FROM plays p JOIN tracks t ON t.id = p.track_id JOIN track_embeddings e ON e.track_id = t.id"
            " WHERE p.rating IS NOT NULL ORDER BY p.play_number").fetchall()
        ranks = percentile_ranks([r["rating"] for r in rows])
        for r, rank in zip(rows, ranks):
            arts = sorted(split_artists(r["artist"]))
            sources = {s[0] for s in c.execute("SELECT DISTINCT source FROM suggestions WHERE candidate_id = ?",
                                               (r["candidate_id"],))}
            comps = json.loads(r["components"])
            file = root / r["file_path"] if r["file_path"] else None
            records.append({
                "key": f"{run}:{r['play_id']}", "run": run, "play_number": r["play_number"],
                "track_id": r["track_id"], "video_id": r["video_id"], "artist": r["artist"], "title": r["title"],
                "artist_group": arts[0] if arts else r["artist"].lower(),
                "rating": r["rating"], "rank": rank, "liked": int(r["rating"] >= liked_at),
                "is_control": r["is_control"], "exact_sources": len(sources), "sources": sorted(sources),
                "release_year": int(r["release_date"][:4]) if r["release_date"] and r["release_date"][:4].isdigit() else None,
                "bpm": r["bpm"], "agreement_logged": comps.get("agreement", {}).get("raw"),
                "audio": str(file) if file and file.exists() else None,
            })
            emb = np.frombuffer(r["embedding"], dtype=np.float32)
            chunks = np.stack([np.frombuffer(x[0], dtype=np.float32) for x in c.execute(
                "SELECT embedding FROM chunk_embeddings WHERE track_id = ? ORDER BY chunk_index", (r["track_id"],))])
            clap_track.append(emb)
            clap_chunks.append(chunks)
    OUT.mkdir(parents=True, exist_ok=True)
    save_features("clap", [r["key"] for r in records], np.stack(clap_track), clap_chunks,
                  meta={"model": "laion/larger_clap_music", "chunk_seconds": 10})
    (OUT / "dataset.json").write_text(json.dumps(records, indent=1))
    return records


def fetch_audio(records: list[dict]) -> None:
    import yt_dlp

    AUDIO.mkdir(parents=True, exist_ok=True)
    missing = [r for r in records if not r["audio"]]
    opts = {"format": "bestaudio", "outtmpl": str(AUDIO / "%(id)s.%(ext)s"), "js_runtimes": {"node": {}},
            "noplaylist": True, "quiet": True, "no_warnings": True, "noprogress": True}
    print(f"{len(missing)} tracks to fetch")
    for i, r in enumerate(missing, 1):
        existing = list(AUDIO.glob(f"{r['video_id']}.*"))
        if existing:
            r["audio"] = str(existing[0])
            continue
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://music.youtube.com/watch?v={r['video_id']}", download=True)
            r["audio"] = info["requested_downloads"][0]["filepath"]
            print(f"  [{i}/{len(missing)}] {r['artist']} – {r['title']}")
        except Exception as e:
            print(f"  [{i}/{len(missing)}] FAILED {r['artist']} – {r['title']}: {str(e).splitlines()[0][:120]}")
        (OUT / "dataset.json").write_text(json.dumps(records, indent=1))
        time.sleep(CFG["download_delay_seconds"])
    have = sum(1 for r in records if r["audio"])
    print(f"audio available for {have}/{len(records)} rated tracks")


if __name__ == "__main__":
    recs = build()
    print(f"dataset: {len(recs)} rated plays ({sum(r['run'] == 'v1' for r in recs)} v1, {sum(r['run'] == 'v2' for r in recs)} v2),"
          f" {sum(r['liked'] for r in recs)} liked, {len({r['artist_group'] for r in recs})} artists")
    fetch_audio(recs)
