"""Ratings and their effects — SPEC.md §16.4 (v3 buttons), §5 and §16.6 (controls).

Everything derived from ratings (averages, reference sets, seeds) is computed from the
`plays` table on demand, so changing the most recent rating only has to update that
play and move its audio file.

v3 buttons: 1 Off (negative set), 2 Close (mild positive, weight 0.5; not saved), 3 Add (positive,
weight 2; seed; saved) — weights in config [ratings]. Effects on the audio file:
  Add (learning or control) → collection/
  anything else → held in data/undo/ until the next rating (so the back button can still
  change it), then deleted. Learning only needs the stored embedding, not the file.
  Skipped tracks' audio is deleted once a later rating makes them unreachable.

v3 (SPEC.md §16.6): control ratings count for learning like any other — likes and dislikes, seed and source
credit, personal model — except that a control is never a seed (random picks don't steer the sources).
"""

import json
import random
import re
import shutil
import sqlite3
import time
from pathlib import Path

import numpy as np

from . import db
from .config import CONFIG, path, relative, resolve

SOURCES = ("lastfm", "ytmusic", "ytrelated", "deezer", "tidal")
AVG = CONFIG["averages"]
POSITIVE_WEIGHTS = {int(k): v for k, v in CONFIG["ratings"]["positive_weights"].items()}
NEGATIVE_WEIGHTS = {int(k): v for k, v in CONFIG["ratings"]["negative_weights"].items()}
LABELS = CONFIG["ratings"]["labels"]
ADD = CONFIG["ratings"]["add"]


def current_generation(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT coalesce(max(id), 0) FROM generations").fetchone()[0]


# --- averages (SPEC.md §5) -----------------------------------------------------------

def _averages(conn: sqlite3.Connection, column: str, upto_play: int | None = None,
              generation: int | None = None) -> dict:
    """Decayed average with prior, per seed or per source, over every rating (controls included, v3).

    Each seed (or source) that suggested a track gets full credit for its rating, once.
    upto_play / generation replay the averages as they were at an earlier play (results view).
    """
    gen = current_generation(conn) if generation is None else generation
    total, weight = {}, {}
    for r in conn.execute(
            f"SELECT DISTINCT p.id, s.{column} AS key, p.rating, p.generation FROM plays p"
            " JOIN tracks t ON t.id = p.track_id"
            " JOIN suggestions s ON s.candidate_id = t.candidate_id"
            " WHERE p.rating IS NOT NULL AND p.id <= ?", (upto_play or 1 << 62,)):
        w = AVG["decay"] ** (gen - r["generation"])
        total[r["key"]] = total.get(r["key"], 0.0) + w * r["rating"]
        weight[r["key"]] = weight.get(r["key"], 0.0) + w
    k, prior = AVG["prior_strength"], AVG["prior"]
    return {key: (prior * k + total[key]) / (k + weight[key]) for key in total}


def seed_averages(conn: sqlite3.Connection, seed_ids) -> dict[int, float]:
    avgs = _averages(conn, "seed_track_id")
    return {s: avgs.get(s, AVG["prior"]) for s in seed_ids}


def source_averages(conn: sqlite3.Connection) -> dict[str, float]:
    avgs = _averages(conn, "source")
    return {s: avgs.get(s, AVG["prior"]) for s in SOURCES}


# --- reference sets (SPEC.md §3) ---------------------------------------------------------

def _embedding_set(conn: sqlite3.Connection, query: str, weights: dict[int, float]):
    rows = conn.execute(
        "SELECT t.id, r.rating, e.embedding FROM (" + query + ") r"
        " JOIN tracks t ON t.id = r.id JOIN track_embeddings e ON e.track_id = t.id ORDER BY t.id").fetchall()
    ids = [r["id"] for r in rows]
    if rows:
        vecs = np.stack([db.from_blob(r["embedding"]) for r in rows])
    else:
        any_emb = conn.execute("SELECT embedding FROM track_embeddings LIMIT 1").fetchone()
        vecs = np.zeros((0, len(db.from_blob(any_emb[0])) if any_emb else 0), np.float32)
    return ids, vecs, np.array([weights[r["rating"]] for r in rows], dtype=np.float32)


def _in(ratings: dict) -> str:
    return "(" + ", ".join(str(int(r)) for r in ratings) + ")"


def positive_set(conn: sqlite3.Connection):
    """(track ids, embeddings, weights): seeds (as Add) and plays with a positive weight (Close, Add)."""
    return _embedding_set(conn,
        f"SELECT id, {ADD} AS rating FROM tracks WHERE origin = 'original_reference'"
        f" UNION SELECT track_id, rating FROM plays WHERE rating IN {_in(POSITIVE_WEIGHTS)}",
        POSITIVE_WEIGHTS)


def negative_set(conn: sqlite3.Connection):
    """(track ids, embeddings, weights): plays rated Off."""
    return _embedding_set(conn, "SELECT track_id AS id, rating FROM plays"
                          f" WHERE rating IN {_in(NEGATIVE_WEIGHTS)}", NEGATIVE_WEIGHTS)


def learning_ratings(conn: sqlite3.Connection) -> int:
    """Ratings the system learns from: every rating in v3, controls included (SPEC.md §16.6)."""
    return conn.execute("SELECT count(*) FROM plays WHERE rating IS NOT NULL").fetchone()[0]


def control_rate(conn: sqlite3.Connection, rng=random) -> float:
    """This run's secret control rate: drawn once (uniform in [controls] rate_min–rate_max), stored, never shown."""
    rate = db.get_setting(conn, "control_rate")
    if rate is None:
        rate = rng.uniform(CONFIG["controls"]["rate_min"], CONFIG["controls"]["rate_max"])
        db.set_setting(conn, "control_rate", rate, log=False)
    return rate


def next_is_control(conn: sqlite3.Connection, rng=random) -> bool:
    """Each play is a control independently, at this run's secret rate (SPEC.md §16.6)."""
    return rng.random() < control_rate(conn, rng)


def rated_controls(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM plays WHERE is_control = 1 AND rating IS NOT NULL").fetchone()[0]


def run_complete(conn: sqlite3.Connection) -> bool:
    """The run ends by itself once [controls] target controls are rated; results are sealed until then."""
    return rated_controls(conn) >= CONFIG["controls"]["target"]


def upcoming(conn: sqlite3.Connection, rng=random) -> int | None:
    """The track the next play would show if it started now, so the player can fetch its audio early.

    Whether the next play is a control (and, if so, which random pool track) is drawn here, ahead of time,
    and kept until that play starts: it never depends on ratings, so drawing it early changes nothing. A
    learning play's track is the current top score, which a rating in between can still change.
    """
    from .scoring import score_pool

    scored = score_pool(conn)
    if not scored:
        return None
    draw = db.get_setting(conn, "next_draw")
    if draw is None or (draw["control"] and draw["track_id"] not in {r["track_id"] for r in scored}):
        control = next_is_control(conn, rng)
        draw = {"control": control, "track_id": rng.choice(scored)["track_id"] if control else None}
        db.set_setting(conn, "next_draw", draw, log=False)
    return draw["track_id"] if draw["control"] else scored[0]["track_id"]


def show_next(conn: sqlite3.Connection, rng=random) -> int:
    """Start the next play, as drawn by `upcoming`. Returns the play ID."""
    upcoming(conn, rng)
    draw = db.get_setting(conn, "next_draw")
    db.set_setting(conn, "next_draw", None, log=False)
    return show(conn, track_id=draw["track_id"], control=True) if draw["control"] else show(conn)


# --- showing and rating --------------------------------------------------------------------

def show(conn: sqlite3.Connection, track_id: int | None = None, control: bool = False, rng=random) -> int:
    """Log a track as shown, with its score breakdown at this moment. Returns the play ID.

    Without track_id: a learning play shows the top-scored track, a control play a random
    unplayed pool track (SPEC.md §15.4).
    """
    from .scoring import score_pool

    t0 = time.monotonic()
    scored = score_pool(conn)
    score_ms = (time.monotonic() - t0) * 1000
    if not scored:
        raise RuntimeError("The pool has no unplayed tracks")
    if track_id:
        row = next(r for r in scored if r["track_id"] == track_id)
    else:
        row = rng.choice(scored) if control else scored[0]
    play_number = conn.execute("SELECT count(*) FROM plays").fetchone()[0] + 1
    components = {c: {"raw": row["raw"][c], "norm": row["norm"][c]} for c in row["raw"]}
    with conn:
        play_id = conn.execute(
            "INSERT INTO plays (track_id, generation, play_number, is_control, weight_set_id, components,"
            " score, rank, pool_size, provenance, target_bpm, scored_for_seed, shown_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%f', 'now'))",
            (row["track_id"], current_generation(conn), play_number, int(control), row["weight_set_id"],
             json.dumps(components), row["score"], row["rank"], len(scored),
             json.dumps(row["provenance"]), row.get("target_bpm"), row.get("scored_for_seed"))).lastrowid
    # Control status and rank stay out of the event log (and its text file): they're in the plays table, and
    # seeing them during the run would reveal which plays are controls (SPEC.md §16.6).
    db.log_event(conn, "shown", play_id, pool_size=len(scored), score_ms=round(score_ms, 1))
    return play_id


def rate(conn: sqlite3.Connection, play_id: int, rating: int, played_seconds: float | None = None,
         played_bpm: float | None = None, playback_rate: float | None = None) -> None:
    _check(rating)
    play = conn.execute("SELECT * FROM plays WHERE id = ?", (play_id,)).fetchone()
    if play is None or play["rating"] is not None:
        raise ValueError(f"Play {play_id} doesn't exist or is already rated (use change_last_rating)")
    if play["skipped_at"] and previous_play(conn)["id"] != play_id:
        raise ValueError("Only the most recent skipped track can still be rated")
    _purge_undo(conn)
    _purge_skipped(conn)
    with conn:
        _place(conn, play, rating)
        conn.execute("UPDATE plays SET rating = ?, rated_at = strftime('%Y-%m-%d %H:%M:%f', 'now'),"
                     " played_seconds = coalesce(?, played_seconds), played_bpm = ?, playback_rate = ?,"
                     " skipped_at = NULL WHERE id = ?",
                     (rating, played_seconds, played_bpm, playback_rate, play_id))
        conn.execute("UPDATE tracks SET rating = ? WHERE id = ?", (rating, play["track_id"]))


def skip(conn: sqlite3.Connection, play_id: int, played_seconds: float | None = None) -> None:
    """Skip without rating: the track counts as shown (never shown again) but teaches nothing."""
    with conn:
        conn.execute("UPDATE plays SET skipped_at = strftime('%Y-%m-%d %H:%M:%f', 'now'), played_seconds = ?"
                     " WHERE id = ? AND rating IS NULL AND skipped_at IS NULL", (played_seconds, play_id))


def current_play(conn: sqlite3.Connection):
    """The play waiting for a rating, if any (e.g. after reloading the page)."""
    return conn.execute("SELECT * FROM plays WHERE rating IS NULL AND skipped_at IS NULL"
                        " ORDER BY id DESC LIMIT 1").fetchone()


def previous_play(conn: sqlite3.Connection, exclude: int | None = None):
    """The most recent finished (rated or skipped) play, other than `exclude`.

    This is where the player's back button goes. When it is the most recent finished play
    overall, its rating can be changed (or, if it was skipped, given late).
    """
    return conn.execute("SELECT * FROM plays WHERE (rating IS NOT NULL OR skipped_at IS NOT NULL) AND id != ?"
                        " ORDER BY coalesce(rated_at, skipped_at) DESC, id DESC LIMIT 1",
                        (exclude or -1,)).fetchone()


def last_rated(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM plays WHERE rating IS NOT NULL ORDER BY rated_at DESC, id DESC LIMIT 1"
                        ).fetchone()


def change_last_rating(conn: sqlite3.Connection, rating: int) -> int:
    """Correct the most recent rating (mis-tap). Returns the play ID."""
    _check(rating)
    play = last_rated(conn)
    if play is None:
        raise ValueError("Nothing has been rated yet")
    db.log_event(conn, "rating_changed", play["id"], old=play["rating"], new=rating)
    with conn:
        _place(conn, play, rating)
        conn.execute("UPDATE plays SET rating = ? WHERE id = ?", (rating, play["id"]))
        conn.execute("UPDATE tracks SET rating = ? WHERE id = ?", (rating, play["track_id"]))
    return play["id"]


def _check(rating: int) -> None:
    if rating not in range(1, len(LABELS) + 1):
        raise ValueError(f"Rating must be 1–{len(LABELS)}, got {rating}")


def _place(conn: sqlite3.Connection, play, rating: int) -> None:
    """Move the play's audio to where its rating puts it (see module docstring)."""
    track = conn.execute("SELECT * FROM tracks WHERE id = ?", (play["track_id"],)).fetchone()
    play = conn.execute("SELECT * FROM plays WHERE id = ?", (play["id"],)).fetchone()  # fresh undo_path
    src = resolve(track["file_path"] or play["undo_path"])
    if rating == ADD:
        dest = _unique(path("collection") / f"{_safe(track['artist'])} - {_safe(track['title'])}{src.suffix}", src)
        file_path, undo_path = relative(dest), None
    else:
        dest = _unique(path("data") / "undo" / src.name, src)
        file_path, undo_path = None, relative(dest)
    if dest != src:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(src, dest)
    conn.execute("UPDATE tracks SET file_path = ? WHERE id = ?", (file_path, track["id"]))
    conn.execute("UPDATE plays SET undo_path = ? WHERE id = ?", (undo_path, play["id"]))


def _purge_skipped(conn: sqlite3.Connection) -> None:
    """After a new rating, earlier skips can no longer be reached with back: delete their audio."""
    with conn:
        for r in conn.execute("SELECT t.id, t.file_path FROM plays p JOIN tracks t ON t.id = p.track_id"
                              " WHERE p.skipped_at IS NOT NULL AND p.rating IS NULL AND t.file_path IS NOT NULL"
                              ).fetchall():
            resolve(r["file_path"]).unlink(missing_ok=True)
            conn.execute("UPDATE tracks SET file_path = NULL WHERE id = ?", (r["id"],))


def _purge_undo(conn: sqlite3.Connection) -> None:
    """A new rating makes earlier ones final: delete audio held for tracks not Added."""
    with conn:
        for r in conn.execute("SELECT id, undo_path FROM plays WHERE undo_path IS NOT NULL").fetchall():
            resolve(r["undo_path"]).unlink(missing_ok=True)
            conn.execute("UPDATE plays SET undo_path = NULL WHERE id = ?", (r["id"],))


def _safe(name: str) -> str:
    return re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", name).strip(" .")[:120] or "untitled"


def _unique(dest: Path, src: Path) -> Path:
    if dest == src or not dest.exists():
        return dest
    n = 2
    while (candidate := dest.with_name(f"{dest.stem} ({n}){dest.suffix}")).exists():
        n += 1
    return candidate
