"""music-hunter command line."""

import argparse
import sys
from pathlib import Path

import numpy as np

from . import audio, db
from .config import CONFIG, path

SOURCE_CODES = {"lastfm": "LF", "ytmusic": "YT", "ytrelated": "YR", "deezer": "DZ", "tidal": "TD"}


def embed_seeds(force: bool) -> None:
    """Embed audio files placed in seeds/ by hand (treated as rated top)."""
    from .seeds import add_local_files

    add_local_files(db.connect(), force)


def add_seeds(links: list[str], file: Path | None) -> None:
    """v2: seeds from Spotify track or playlist links (SPEC.md §15.1)."""
    from .seeds import add_from_spotify

    if file:
        links = links + [l.strip() for l in file.read_text().splitlines() if l.strip() and not l.startswith("#")]
    if not links:
        sys.exit("Give Spotify links, or --file with one link per line.")
    add_from_spotify(db.connect(), links)


def analyse_bpm(seeds: bool) -> None:
    """Measure BPM for tracks that have audio but no BPM yet; --seeds re-reads the seeds (two passes)."""
    from .bpm import estimate, seeds_median
    from .config import resolve

    conn = db.connect()
    if seeds:
        from .seeds import refresh_seed_bpms
        refresh_seed_bpms(conn)
        return
    around = seeds_median(conn)
    sr = CONFIG["clap"]["sample_rate"]
    rows = conn.execute("SELECT id, artist, title, file_path FROM tracks WHERE bpm IS NULL AND file_path IS NOT NULL").fetchall()
    for r in rows:
        tempo = estimate(audio.decode(resolve(r["file_path"]), sr), sr, around)
        with conn:
            conn.execute("UPDATE tracks SET bpm = ? WHERE id = ?", (tempo, r["id"]))
        print(f"  {tempo or '–':>7} BPM  {r['artist']} – {r['title']}")
    print(f"{len(rows)} tracks measured")


def similarity(extra_files: list[Path]) -> None:
    """Milestone 1 check: cosine similarity matrix of references (+ optional test files)."""
    conn = db.connect()
    rows = conn.execute(
        "SELECT t.id, t.artist, t.title, e.model, e.embedding FROM tracks t"
        " JOIN track_embeddings e ON e.track_id = t.id"
        " WHERE t.origin = 'original_reference' ORDER BY t.id").fetchall()
    if not rows:
        sys.exit("No seed embeddings yet. Run: music-hunter add-seeds <Spotify links>")

    model = CONFIG["clap"]["checkpoint"]
    if any(r["model"] != model for r in rows):
        sys.exit(f"Some seeds were embedded with a different model than {model}; re-run embed-seeds --force.")

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


def pull(fresh: bool, no_lastfm: bool, no_fill: bool, no_deezer: bool = False, no_tidal: bool = False) -> None:
    """One generation: candidates from every source, then fill the pool."""
    from .pull import Pull, match_seeds, optional, reset
    from .sources import Deezer, LastFM, Tidal, YouTubeMusic

    conn = db.connect()
    if fresh:
        reset(conn)
    yt = YouTubeMusic()
    lastfm = None if no_lastfm else LastFM()
    seed_list = match_seeds(conn, yt)
    generation = Pull(conn, yt, lastfm, None if no_deezer else Deezer(),
                      None if no_tidal else optional(Tidal)).run(seed_list)
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

    from .pool import multi_source_unplayed

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
        sources = ", ".join(f"{SOURCE_CODES[p['source']]}:{seed_names[p['seed_track_id']]}"
                            for p in prov)
        analysed = f"{t['n_chunks']} chunks" if t["n_chunks"] else "NOT ANALYSED"
        tempo = f"{t['bpm']:.1f} BPM" if t["bpm"] else "BPM ?"
        year = (t["release_date"] or "?")[:4]
        print(f"  {t['id']:4d}  {t['artist']} – {t['title']}  [{t['duration_s'] / 60:.1f} min, {tempo}, {year},"
              f" {analysed}, {t['loudness_lufs']:.1f} LUFS → gain {t['gain_db']:+.1f} dB]\n          {sources}")

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
    if stats.get("seed_pools"):
        print("\nEach seed's share of the pool (target / unplayed now / multi-source added this pull):")
        for seed, p in stats["seed_pools"].items():
            print(f"  {p['target']:4d} / {p['unplayed']:4d} / {p['multi_source_added']:3d}  {seed}")
    if stats.get("multi_source_candidates") is not None:
        print(f"\nMulti-source candidates (2+ source families): {stats['multi_source_candidates']},"
              f" added first: {stats['multi_source_added']}; unplayed multi-source in pool: {multi_source_unplayed(conn)}")
    if stats.get("unusable_at_selection"):
        print("Not usable when selected (YouTube match failed, or a duplicate found by ISRC / YouTube ID):")
        for reason, n in stats["unusable_at_selection"].items():
            print(f"  {n:4d}  {reason}")
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
             "agreement": "agree", "personal_model": "model", "diversity_penalty": "divers",
             "bpm_match": "bpm", "year_match": "year"}
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

    print(f"\nCandidates — generation {generation} (most source families first):")
    rows = conn.execute("SELECT * FROM candidates WHERE generation = ? ORDER BY id", (generation,)).fetchall()
    from .pull import families
    fams = families(conn, [c["id"] for c in rows])
    rows.sort(key=lambda c: -len(fams[c["id"]]))
    for c in rows:
        prov = conn.execute("SELECT seed_track_id, source, rank, via FROM suggestions WHERE candidate_id = ?"
                            " ORDER BY source, rank", (c["id"],)).fetchall()
        sources = ", ".join(
            f"{SOURCE_CODES[p['source']]}:{seed_names[p['seed_track_id']]}#{p['rank']}"
            + (f" via {p['via']}" if p["via"] else "") for p in prov)
        dur = f"{c['duration_s'] // 60:.0f}:{c['duration_s'] % 60:02.0f}" if c["duration_s"] else "?"
        ext = " [ext]" if c["is_extended"] else ""
        yt = "" if c["matched"] else " [YouTube match when selected]"
        print(f"  {c['id']:4d}  {c['artist']} – {c['title']} ({dur}){ext}{yt}  · {len(fams[c['id']])} families"
              f"\n          {sources}")
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
    if stats.get("seed_lookups"):
        print("\nSeeds on Deezer / TIDAL:")
        for seed, look in stats["seed_lookups"].items():
            print(f"  {seed}: " + ", ".join(f"{k} {v}" for k, v in look.items()))
    if stats.get("source_combinations"):
        print("\nCandidates by which sources suggested them (agreement):")
        for combo, n in stats["source_combinations"].items():
            print(f"  {n:4d}  {combo}")
        f = stats.get("candidates_by_family_count", {})
        if f:
            print("Candidates suggested by 1 / 2 / 3 / 4 source families (YouTube radio + related = one family): "
                  + " / ".join(str(f.get(str(k), 0)) for k in (1, 2, 3, 4)))
        a = stats.get("artists_by_source_count", {})
        print("Artists suggested by 1 / 2 / 3 / 4 / 5 sources: "
              + " / ".join(str(a.get(str(k), 0)) for k in (1, 2, 3, 4, 5)))
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

    p = sub.add_parser("add-seeds", help="Add seeds from Spotify track or playlist links")
    p.add_argument("links", nargs="*", help="Spotify track or playlist links")
    p.add_argument("--file", type=Path, help="Text file with one link per line")

    p = sub.add_parser("embed-seeds", help="Embed audio files placed in seeds/ by hand")
    p.add_argument("--force", action="store_true", help="Re-embed even if already stored")

    p = sub.add_parser("similarity", help="Show the reference similarity matrix")
    p.add_argument("files", nargs="*", type=Path,
                   help="Extra audio files to compare against the references (not stored)")

    p = sub.add_parser("pull", help="Fetch candidates from every source, then fill the pool (one generation)")
    p.add_argument("--fresh", action="store_true", help="Forget earlier pulls and seed matches first")
    p.add_argument("--no-lastfm", action="store_true", help="YouTube Music only (e.g. before the API key is set)")
    p.add_argument("--no-fill", action="store_true", help="Candidates only; don't download or analyse")
    p.add_argument("--no-deezer", action="store_true", help="Skip the Deezer source")
    p.add_argument("--no-tidal", action="store_true", help="Skip the TIDAL source")

    p = sub.add_parser("fill", help="Fill the pool from a generation's candidates (resumes)")
    p.add_argument("--generation", type=int, help="Defaults to the latest pull")

    sub.add_parser("pool", help="Show the analysed pool")

    p = sub.add_parser("scores", help="Rank the pool by current score")
    p.add_argument("--top", type=int, default=25)

    sub.add_parser("averages", help="Seed and source averages, reference sets")

    p = sub.add_parser("analyse-bpm", help="Measure BPM for tracks that don't have it yet")
    p.add_argument("--seeds", action="store_true", help="Re-read the seeds' BPM (two passes, 3:2 tie-break)")

    sub.add_parser("serve", help="Start the localhost player")

    sub.add_parser("report", help="Operational report: sessions, delays, pipeline timings, errors")

    p = sub.add_parser("candidates", help="List candidates with sources and filter-reason counts")
    p.add_argument("--generation", type=int, help="Defaults to the latest pull")

    args = parser.parse_args()
    db.setup_logging()
    if args.command == "add-seeds":
        add_seeds(args.links, args.file)
    elif args.command == "embed-seeds":
        embed_seeds(args.force)
    elif args.command == "similarity":
        similarity(args.files)
    elif args.command == "pull":
        pull(args.fresh, args.no_lastfm, args.no_fill, args.no_deezer, args.no_tidal)
    elif args.command == "fill":
        fill(args.generation)
    elif args.command == "pool":
        show_pool()
    elif args.command == "scores":
        scores(args.top)
    elif args.command == "averages":
        averages()
    elif args.command == "analyse-bpm":
        analyse_bpm(args.seeds)
    elif args.command == "serve":
        from .web import serve
        serve()
    elif args.command == "report":
        from .report import report
        report(db.connect())
    elif args.command == "candidates":
        candidates(args.generation)
