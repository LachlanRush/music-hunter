"""music-hunter command line."""

import argparse
import sys
from pathlib import Path

import numpy as np

from . import audio, db
from .config import CONFIG, path


def embed_references(force: bool) -> None:
    """Milestone 1: embed the original reference tracks in references/ (treated as rated 5)."""
    from .clap import Embedder

    folder = path("references")
    folder.mkdir(exist_ok=True)
    files = audio.audio_files(folder)
    if not files:
        sys.exit(f"No audio files in {folder}. Add the 5 original reference tracks first.")

    conn = db.connect()
    embedder = Embedder()
    sr = CONFIG["clap"]["sample_rate"]
    for file in files:
        rel = str(file.relative_to(path("references").parent))
        row = conn.execute(
            "SELECT t.id, e.model FROM tracks t LEFT JOIN track_embeddings e ON e.track_id = t.id"
            " WHERE t.file_path = ?", (rel,)).fetchone()
        if row and row["model"] == embedder.model_name and not force:
            print(f"  skip (already embedded)  {file.name}")
            continue

        artist, title = audio.artist_title(file)
        samples = audio.decode(file, sr)
        emb = embedder.embed_track(samples)
        with conn:
            if row:
                track_id = row["id"]
                conn.execute("UPDATE tracks SET artist=?, title=?, duration_s=? WHERE id=?",
                             (artist, title, len(samples) / sr, track_id))
            else:
                track_id = conn.execute(
                    "INSERT INTO tracks (artist, title, file_path, duration_s, origin, rating)"
                    " VALUES (?, ?, ?, ?, 'original_reference', 5)",
                    (artist, title, rel, len(samples) / sr)).lastrowid
            db.store_embedding(conn, track_id, embedder.model_name, emb)
        print(f"  embedded {len(emb.chunks):3d} chunks  {artist} – {title}")


def similarity(extra_files: list[Path]) -> None:
    """Milestone 1 check: cosine similarity matrix of references (+ optional test files)."""
    conn = db.connect()
    rows = conn.execute(
        "SELECT t.id, t.artist, t.title, e.model, e.embedding FROM tracks t"
        " JOIN track_embeddings e ON e.track_id = t.id"
        " WHERE t.origin = 'original_reference' ORDER BY t.id").fetchall()
    if not rows:
        sys.exit("No reference embeddings yet. Run: music-hunter embed-references")

    model = CONFIG["clap"]["checkpoint"]
    if any(r["model"] != model for r in rows):
        sys.exit(f"Some references were embedded with a different model than {model}; re-run with --force.")

    labels = [f"{r['artist']} – {r['title']}" for r in rows]
    vecs = [db.from_blob(r["embedding"]) for r in rows]
    consistency = [chunk_consistency(conn, r["id"]) for r in rows]
    n_refs = len(rows)

    if extra_files:
        from .clap import Embedder

        embedder = Embedder()
        sr = CONFIG["clap"]["sample_rate"]
        for file in extra_files:
            try:
                artist, title = audio.artist_title(file)
                labels.append(f"[test] {artist} – {title}")
            except ValueError:
                labels.append(f"[test] {file.stem}")
            emb = embedder.embed_track(audio.decode(file, sr))
            vecs.append(emb.track)
            consistency.append(mean_offdiag(emb.chunks @ emb.chunks.T))

    m = np.stack(vecs)
    sim = m @ m.T

    print("\nTracks (chunk consistency = mean similarity between a track's own chunks):")
    for i, (label, c) in enumerate(zip(labels, consistency), 1):
        print(f"  {i:2d}. {label}   [consistency {c:.3f}]")

    print("\nCosine similarity:")
    print("      " + "".join(f"{j:>7d}" for j in range(1, len(labels) + 1)))
    for i, row in enumerate(sim, 1):
        print(f"  {i:2d}  " + "".join(f"{v:7.3f}" for v in row))

    off = sim[:n_refs, :n_refs][~np.eye(n_refs, dtype=bool)]
    if off.size:
        print(f"\nReference-to-reference: min {off.min():.3f}  mean {off.mean():.3f}  max {off.max():.3f}")

    if extra_files:
        print("\nTest tracks vs references (top-3 mean ≈ audio_match before normalisation):")
        for i in range(n_refs, len(labels)):
            to_refs = np.sort(sim[i, :n_refs])[::-1]
            print(f"  {i + 1:2d}. {labels[i]}: max {to_refs[0]:.3f}  top-3 mean {to_refs[:3].mean():.3f}")


def pull(fresh: bool, no_lastfm: bool, no_fill: bool) -> None:
    """One generation: candidates from both sources (Milestone 2), then fill the pool (Milestone 3)."""
    from .pull import Pull, match_seeds, reset
    from .sources import LastFM, YouTubeMusic

    conn = db.connect()
    if fresh:
        reset(conn)
    yt = YouTubeMusic()
    lastfm = None if no_lastfm else LastFM()
    seed_list = match_seeds(conn, yt)
    generation = Pull(conn, yt, lastfm).run(seed_list)
    candidate_stats(conn, generation)
    if not no_fill:
        fill(generation)


def fill(generation: int | None) -> None:
    """Milestone 3: quotas, paced downloads, loudness, analysis. Re-run to resume."""
    from .pool import fill as fill_pool

    conn = db.connect()
    generation = generation or latest_generation(conn)
    fill_pool(conn, generation)
    show_pool()


def show_pool() -> None:
    """Milestone 3 check: analysed pool tracks, per-list counts, quotas and failures."""
    import json
    from collections import Counter

    conn = db.connect()
    rows = conn.execute(
        "SELECT t.*, e.n_chunks, e.model FROM tracks t LEFT JOIN track_embeddings e ON e.track_id = t.id"
        " WHERE t.origin = 'pool' AND t.rating IS NULL ORDER BY t.id").fetchall()
    seed_names = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM tracks")}
    print(f"\nPool — {len(rows)} unrated tracks:")
    per_list = Counter()
    for t in rows:
        prov = conn.execute("SELECT DISTINCT seed_track_id, source FROM suggestions WHERE candidate_id = ?",
                            (t["candidate_id"],)).fetchall()
        for p in prov:
            per_list[(seed_names[p["seed_track_id"]], p["source"])] += 1
        sources = ", ".join(f"{'LF' if p['source'] == 'lastfm' else 'YT'}:{seed_names[p['seed_track_id']]}"
                            for p in prov)
        analysed = f"{t['n_chunks']} chunks" if t["n_chunks"] else "NOT ANALYSED"
        print(f"  {t['id']:4d}  {t['artist']} – {t['title']}  [{t['duration_s'] / 60:.1f} min, {analysed},"
              f" {t['loudness_lufs']:.1f} LUFS → gain {t['gain_db']:+.1f} dB]\n          {sources}")

    gen = latest_generation(conn)
    stats = json.loads(conn.execute("SELECT stats FROM generations WHERE id = ?", (gen,)).fetchone()[0] or "{}")
    if "quotas" in stats:
        print(f"\nGeneration {gen} quotas (seed averages {sorted(set(stats['seed_averages'].values()))},"
              f" source averages {stats['source_averages']}):")
        for name, n in stats["quotas"].items():
            print(f"  {n:3d}  {name}")
    print("\nPool tracks suggested by each (seed, source) — a track can count for several:")
    for (seed, src), n in sorted(per_list.items()):
        print(f"  {n:3d}  {seed} / {src}")
    failed = conn.execute("SELECT artist, title, fail_reason FROM candidates WHERE status = 'download_failed'").fetchall()
    print(f"\nDownload/analysis failures: {len(failed)}")
    for f in failed:
        print(f"  {f['artist']} – {f['title']}: {f['fail_reason']}")
    analysed = sum(1 for t in rows if t["n_chunks"])
    print(f"\nAnalysed tracks in pool: {analysed} / target {CONFIG['pool']['target_size']}")


def scores(top: int) -> None:
    """Milestone 4: the pool ranked by current score, with normalised components."""
    from .scoring import COMPONENTS, current_weights, score_pool

    conn = db.connect()
    rows = score_pool(conn)
    _, weights = current_weights(conn)
    short = {"audio_match": "audio", "dislike_match": "dislike", "source_score": "source", "seed_score": "seed",
             "agreement": "agree", "personal_model": "model", "diversity_penalty": "divers"}
    print("Weights: " + ", ".join(f"{short[c]} {weights[c]:+.2f}" for c in COMPONENTS))
    print(f"\n rank  score  " + " ".join(f"{short[c]:>7}" for c in COMPONENTS) + "  track")
    for r in rows[:top]:
        print(f"  {r['rank']:3d}  {r['score']:5.3f}  " + " ".join(f"{r['norm'][c]:7.2f}" for c in COMPONENTS)
              + f"  {r['artist']} – {r['title']}")
    print(f"\n{len(rows)} unplayed tracks scored")


def averages() -> None:
    """Milestone 4: seed and source averages, reference sets, seeds."""
    from .pull import seeds
    from .ratings import learning_ratings, negative_set, positive_set, seed_averages, source_averages

    conn = db.connect()
    seed_list = seeds(conn)
    s_avg = seed_averages(conn, [s.track_id for s in seed_list])
    print("Seed averages:")
    for s in seed_list:
        print(f"  {s_avg[s.track_id]:.2f}  {s.label}")
    print("\nSource averages:")
    for src, a in source_averages(conn).items():
        print(f"  {a:.2f}  {src}")
    print(f"\nPositive reference set: {len(positive_set(conn)[0])} tracks;"
          f" negative set: {len(negative_set(conn)[0])}; learning ratings: {learning_ratings(conn)}")


def latest_generation(conn) -> int:
    row = conn.execute("SELECT max(id) FROM generations").fetchone()
    if row[0] is None:
        sys.exit("No pulls yet. Run: music-hunter pull")
    return row[0]


def candidates(generation: int | None) -> None:
    """Milestone 2 check: candidate list with sources, and filter-reason counts."""
    import json

    conn = db.connect()
    generation = generation or latest_generation(conn)
    seed_names = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM tracks")}

    print(f"\nCandidates — generation {generation}:")
    rows = conn.execute("SELECT * FROM candidates WHERE generation = ? ORDER BY id", (generation,)).fetchall()
    for c in rows:
        prov = conn.execute("SELECT seed_track_id, source, rank, via FROM suggestions WHERE candidate_id = ?"
                            " ORDER BY source, rank", (c["id"],)).fetchall()
        sources = ", ".join(
            f"{'LF' if p['source'] == 'lastfm' else 'YT'}:{seed_names[p['seed_track_id']]}#{p['rank']}"
            + (f" via {p['via']}" if p["via"] else "") for p in prov)
        dur = f"{c['duration_s'] // 60:.0f}:{c['duration_s'] % 60:02.0f}" if c["duration_s"] else "?"
        ext = " [ext]" if c["is_extended"] else ""
        print(f"  {c['id']:4d}  {c['artist']} – {c['title']} ({dur}){ext}\n          {sources}")
    candidate_stats(conn, generation)


def candidate_stats(conn, generation: int) -> None:
    import json

    stats = json.loads(conn.execute("SELECT stats FROM generations WHERE id = ?", (generation,)).fetchone()[0] or "{}")
    print("\nRaw suggestions per seed:")
    for seed, by_source in stats.get("raw_suggestions", {}).items():
        print(f"  {seed}: " + ", ".join(f"{s} {n}" for s, n in sorted(by_source.items())))
    print("\nMerged / dropped, by reason:")
    for reason, n in stats.get("filter_reasons", {}).items():
        print(f"  {n:4d}  {reason}")
    if stats.get("seed_match_failures"):
        print("\nSeeds not matched on YouTube Music: " + "; ".join(stats["seed_match_failures"]))
    print(f"\nDistinct candidates: {stats.get('candidates')}")


def chunk_consistency(conn, track_id: int) -> float:
    blobs = conn.execute("SELECT embedding FROM chunk_embeddings WHERE track_id=? ORDER BY chunk_index",
                         (track_id,)).fetchall()
    chunks = np.stack([db.from_blob(b["embedding"]) for b in blobs])
    return mean_offdiag(chunks @ chunks.T)


def mean_offdiag(sim: np.ndarray) -> float:
    n = len(sim)
    return float(sim[~np.eye(n, dtype=bool)].mean()) if n > 1 else 1.0


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-hunter")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("embed-references", help="Embed the original tracks in references/")
    p.add_argument("--force", action="store_true", help="Re-embed even if already stored")

    p = sub.add_parser("similarity", help="Show the reference similarity matrix")
    p.add_argument("files", nargs="*", type=Path,
                   help="Extra audio files to compare against the references (not stored)")

    p = sub.add_parser("pull", help="Fetch candidates from Last.fm and YouTube Music (one generation)")
    p.add_argument("--fresh", action="store_true", help="Forget earlier pulls and seed matches first")
    p.add_argument("--no-lastfm", action="store_true", help="YouTube Music only (e.g. before the API key is set)")
    p.add_argument("--no-fill", action="store_true", help="Candidates only; don't download or analyse")

    p = sub.add_parser("fill", help="Fill the pool from a generation's candidates (resumes)")
    p.add_argument("--generation", type=int, help="Defaults to the latest pull")

    sub.add_parser("pool", help="Show the analysed pool")

    p = sub.add_parser("scores", help="Rank the pool by current score")
    p.add_argument("--top", type=int, default=25)

    sub.add_parser("averages", help="Seed and source averages, reference sets")

    sub.add_parser("serve", help="Start the localhost player")

    sub.add_parser("report", help="Operational report: sessions, delays, pipeline timings, errors")

    p = sub.add_parser("candidates", help="List candidates with sources and filter-reason counts")
    p.add_argument("--generation", type=int, help="Defaults to the latest pull")

    args = parser.parse_args()
    db.setup_logging()
    if args.command == "embed-references":
        embed_references(args.force)
    elif args.command == "similarity":
        similarity(args.files)
    elif args.command == "pull":
        pull(args.fresh, args.no_lastfm, args.no_fill)
    elif args.command == "fill":
        fill(args.generation)
    elif args.command == "pool":
        show_pool()
    elif args.command == "scores":
        scores(args.top)
    elif args.command == "averages":
        averages()
    elif args.command == "serve":
        from .web import serve
        serve()
    elif args.command == "report":
        from .report import report
        report(db.connect())
    elif args.command == "candidates":
        candidates(args.generation)
