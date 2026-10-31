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

    Recency: a rating [averages] decay_half_life_ratings back counts half (v3); with 0, × decay per generation.

    Each seed (or source) that suggested a track gets full credit for its rating, once.
    upto_play / generation replay the averages as they were at an earlier play (results view).
    """
    gen = current_generation(conn) if generation is None else generation
    upto = upto_play or 1 << 62
    half_life = AVG["decay_half_life_ratings"]
    order = [r[0] for r in conn.execute("SELECT id FROM plays WHERE rating IS NOT NULL AND id <= ?"
                                        " ORDER BY rated_at, id", (upto,))]
    age = {pid: len(order) - 1 - i for i, pid in enumerate(order)}      # 0 = the most recent rating
    total, weight = {}, {}
    for r in conn.execute(
            f"SELECT DISTINCT p.id, s.{column} AS key, p.rating, p.generation FROM plays p"
            " JOIN tracks t ON t.id = p.track_id"
            " JOIN suggestions s ON s.candidate_id = t.candidate_id"
            " WHERE p.rating IS NOT NULL AND p.id <= ?", (upto,)):
        w = 0.5 ** (age[r["id"]] / half_life) if half_life else AVG["decay"] ** (gen - r["generation"])
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


def positive_set(conn: sqlite3.Connection, include_seeds: bool = True):
    """(track ids, embeddings, weights): the original seeds (as Add, unless include_seeds is off) and plays with a
    positive weight (Close, Add)."""
    seeds_part = f"SELECT id, {ADD} AS rating FROM tracks WHERE origin = 'original_reference' UNION " \
        if include_seeds else ""
    return _embedding_set(conn,
        f"{seeds_part}SELECT track_id AS id, rating FROM plays WHERE rating IN {_in(POSITIVE_WEIGHTS)}",
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
    """The run is over — ended by hand (`music-hunter end-run`, v4), or once [controls] target controls are rated
    (0 = no automatic end). Results are sealed until then."""
    target = CONFIG["controls"]["target"]
    return bool(db.get_setting(conn, "run_ended", False)) or (target > 0 and rated_controls(conn) >= target)


def _ids(conn: sqlite3.Connection, where: str) -> list[int]:
    return [r[0] for r in conn.execute(f"SELECT t.id FROM tracks t WHERE {where} ORDER BY t.id")]


def playable_ids(conn: sqlite3.Connection) -> list[int]:
    """Playable tracks ready to play now (audio present) — v4 picks come from these, at random."""
    from .veto import PLAYABLE

    return _ids(conn, f"{PLAYABLE} AND t.file_path IS NOT NULL AND t.id IN (SELECT track_id FROM track_embeddings)")


def vetoed_ids(conn: sqlite3.Connection) -> list[int]:
    """Unplayed vetoed tracks — injected controls come from these (SPEC.md §17.5)."""
    return _ids(conn, "t.origin = 'pool' AND t.veto_state = 'vetoed' AND t.id NOT IN (SELECT track_id FROM plays)")


def upcoming(conn: sqlite3.Connection, rng=random) -> int | None:
    """The track the next play will show, drawn ahead so the player can fetch its audio early.

    v4 (SPEC.md §17.5): at the run's secret rate the next play is a control — a random *vetoed* track slipped in
    to test the veto — otherwise a random playable track. The control decision never depends on ratings, so it
    is drawn once and kept until that play starts; only the track is redrawn if it no longer qualifies (e.g. a
    rating in between vetoed it). With nothing vetoed yet, a control can't be injected and the play is a normal pick.
    """
    playable, vetoed = set(playable_ids(conn)), set(vetoed_ids(conn))
    if not playable and not vetoed:
        return None
    draw = db.get_setting(conn, "next_draw")
    if draw is None or draw.get("track_id") not in (vetoed if draw.get("control") else playable):
        control = draw["control"] if draw is not None else next_is_control(conn, rng)
        if control and not vetoed:
            control = False
        choices = sorted(vetoed if control else playable)
        if not choices:
            return None
        draw = {"control": control, "track_id": rng.choice(choices)}
        db.set_setting(conn, "next_draw", draw, log=False)
    return draw["track_id"]


def show_next(conn: sqlite3.Connection, rng=random) -> int:
    """Start the next play, as drawn by `upcoming`. Returns the play ID."""
    if upcoming(conn, rng) is None:
        raise RuntimeError("The pool has no playable tracks")
    draw = db.get_setting(conn, "next_draw")
    db.set_setting(conn, "next_draw", None, log=False)
    return show(conn, track_id=draw["track_id"], control=draw["control"])


# --- showing and rating --------------------------------------------------------------------

def show(conn: sqlite3.Connection, track_id: int | None = None, control: bool = False, rng=random) -> int:
    """Log a track as shown, with every score component and its veto details at this moment. Returns the play ID.

    Without track_id: a random playable track (a control: a random vetoed track, or a playable one if none is
    vetoed). The logged score and rank are kept as data only; v4 doesn't pick by them.
    """
    from .scoring import score_pool
    from .veto import settings_version

    t0 = time.monotonic()
    scored = score_pool(conn)
    score_ms = (time.monotonic() - t0) * 1000
    if not scored:
        raise RuntimeError("The pool has no unplayed tracks")
    if track_id is None:
        choices = (vetoed_ids(conn) if control else []) or playable_ids(conn) or [r["track_id"] for r in scored]
        track_id = rng.choice(choices)
    row = next(r for r in scored if r["track_id"] == track_id)
    play_number = conn.execute("SELECT count(*) FROM plays").fetchone()[0] + 1
    components = {c: {"raw": row["raw"][c], "norm": row["norm"][c]} for c in row["raw"]}
    veto_json = conn.execute("SELECT veto FROM tracks WHERE id = ?", (track_id,)).fetchone()[0]
    if veto_json:                                  # the pool's split at this moment, for the "sources alone" baseline
        v = json.loads(veto_json)
        v["pool"] = {"playable": len(playable_ids(conn)), "vetoed": len(vetoed_ids(conn))}
        veto_json = json.dumps(v)
    with conn:
        play_id = conn.execute(
            "INSERT INTO plays (track_id, generation, play_number, is_control, weight_set_id, components,"
            " score, rank, pool_size, provenance, target_bpm, scored_for_seed, veto, settings_version, shown_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%f', 'now'))",
            (row["track_id"], current_generation(conn), play_number, int(control), row["weight_set_id"],
             json.dumps(components), row["score"], row["rank"], len(scored),
             json.dumps(row["provenance"]), row.get("target_bpm"), row.get("scored_for_seed"), veto_json,
             settings_version(conn))).lastrowid
    # Control status and rank stay out of the event log (and its text file): they're in the plays table, and
    # seeing them during the run would reveal which plays are controls (SPEC.md §16.6).
    db.log_event(conn, "shown", play_id, pool_size=len(scored), score_ms=round(score_ms, 1))
    return play_id


def rate(conn: sqlite3.Connection, play_id: int, rating: int, played_seconds: float | None = None,
         played_bpm: float | None = None, playback_rate: float | None = None, auto: bool = False) -> None:
    """Record a rating. auto: given by the player for a skipped or unrated track ([ratings] unrated_counts_as)."""
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
                     " skipped_at = NULL, auto_rated = ? WHERE id = ?",
                     (rating, played_seconds, played_bpm, playback_rate, int(auto), play_id))
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
    keep = [t for t in json.loads(play["tags"] or "[]") if t in tag_options(rating)]   # tags that still fit
    with conn:
        _place(conn, play, rating)
        conn.execute("UPDATE plays SET rating = ?, auto_rated = 0, tags = ? WHERE id = ?",
                     (rating, json.dumps(keep) if keep else None, play["id"]))
        conn.execute("UPDATE tracks SET rating = ? WHERE id = ?", (rating, play["track_id"]))
    return play["id"]


def tag_options(rating: int | None) -> list[str]:
    """The reason tags offered for a rating ([ratings.tags])."""
    return list(CONFIG["ratings"].get("tags", {}).get(str(rating), [])) if rating else []


def set_tags(conn: sqlite3.Connection, play_id: int, tags: list[str]) -> list[str]:
    """Optional reason tags for the most recent rating (SPEC.md §17.7). Unknown tags are refused. Returns them."""
    play = last_rated(conn)
    if play is None or play["id"] != play_id:
        raise ValueError("Tags can only be set on the most recent rating")
    allowed = tag_options(play["rating"])
    bad = [t for t in tags if t not in allowed]
    if bad:
        raise ValueError(f"Not a tag for this rating: {', '.join(bad)}")
    clean = [t for t in allowed if t in set(tags)]               # config order, no repeats
    with conn:
        conn.execute("UPDATE plays SET tags = ? WHERE id = ?", (json.dumps(clean) if clean else None, play_id))
    return clean


def _check(rating: int) -> None:
    if rating not in range(1, len(LABELS) + 1):
        raise ValueError(f"Rating must be 1–{len(LABELS)}, got {rating}")


def _place(conn: sqlite3.Connection, play, rating: int) -> None:
    """Move the play's audio to where its rating puts it (see module docstring)."""
    track = conn.execute("SELECT * FROM tracks WHERE id = ?", (play["track_id"],)).fetchone()
    play = conn.execute("SELECT * FROM plays WHERE id = ?", (play["id"],)).fetchone()  # fresh undo_path
    if not (track["file_path"] or play["undo_path"]):          # no audio on disk (e.g. a fetch failed): nothing to move
        db.log_event(conn, "no_audio_to_place", play["id"], rating=rating)
        return
    src = resolve(track["file_path"] or play["undo_path"])
    if rating == ADD:                               # v5: saved to the library (it joins the playlist via find.sync_add)
        dest = _unique(path("library") / f"{_safe(track['artist'])} - {_safe(track['title'])}{src.suffix}", src)
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
