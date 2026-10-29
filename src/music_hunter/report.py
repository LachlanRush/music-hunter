"""Operational report: is everything working, and how long does it take? (`music-hunter report`)

Built from the plays, generations and events tables. Results of the experiment itself
are on the results page; this is about health, delays and timings.
"""

import json
import sqlite3
from collections import Counter
from datetime import datetime

from .config import CONFIG, path

GAP_MINUTES = CONFIG["report"]["session_gap_minutes"]


def ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def spread(values: list[float], unit: str, digits: int = 1) -> str:
    if not values:
        return "no data"
    f = lambda x: f"{x:.{digits}f}{unit}"
    return f"median {f(pct(values, 0.5))} · p90 {f(pct(values, 0.9))} · max {f(max(values))} (n={len(values)})"


def events(conn: sqlite3.Connection, kind: str) -> list[tuple[sqlite3.Row, dict]]:
    return [(r, json.loads(r["data"] or "{}")) for r in
            conn.execute("SELECT * FROM events WHERE kind = ? ORDER BY id", (kind,))]


def folder_size(name: str) -> tuple[int, float]:
    folder = path(name)
    files = [f for f in folder.rglob("*") if f.is_file()] if folder.exists() else []
    return len(files), sum(f.stat().st_size for f in files) / 1e6


def report(conn: sqlite3.Connection) -> None:
    plays = conn.execute("SELECT * FROM plays ORDER BY id").fetchall()

    print("SESSIONS")
    sessions, current = [], []
    for p in plays:
        if current and (ts(p["shown_at"]) - ts(current[-1]["shown_at"])).total_seconds() > GAP_MINUTES * 60:
            sessions.append(current)
            current = []
        current.append(p)
    if current:
        sessions.append(current)
    for s in sessions:
        end = max(ts(p["rated_at"] or p["skipped_at"] or p["shown_at"]) for p in s)
        minutes = (end - ts(s[0]["shown_at"])).total_seconds() / 60
        print(f"  {s[0]['shown_at'][:16]}  {len(s):3d} plays  {sum(p['rating'] is not None for p in s):3d} rated"
              f"  {minutes:5.0f} min  ({len(s) / max(minutes / 60, 1 / 60):.0f} plays/hour)")
    if not sessions:
        print("  no plays yet")

    print("\nLISTENING")
    rated = [p for p in plays if p["rating"] is not None]
    print(f"  plays {len(plays)} · rated {len(rated)} · skipped {sum(1 for p in plays if p['skipped_at'])}"
          f" · control {sum(p['is_control'] for p in plays)} · awaiting rating"
          f" {sum(1 for p in plays if p['rating'] is None and not p['skipped_at'])}")
    for label, group in (("ranked", [p for p in rated if not p["is_control"]]),
                         ("control", [p for p in rated if p["is_control"]])):
        dist = Counter(p["rating"] for p in group)
        print(f"  {label:8s} ratings 1–5: " + " ".join(f"{r}:{dist.get(r, 0)}" for r in range(1, 6)))
    print("  time from shown to rated: " + spread(
        [(ts(p["rated_at"]) - ts(p["shown_at"])).total_seconds() for p in rated], " s", 0))
    print("  listened before rating:   " + spread([p["played_seconds"] for p in rated if p["played_seconds"] is not None], " s", 0))
    changes = events(conn, "rating_changed")
    print(f"  rating changes: {len(changes)}" + (" (" + ", ".join(f"{d['old']}→{d['new']}" for _, d in changes[-10:]) + ")" if changes else ""))
    ended = events(conn, "player_ended")
    print(f"  tracks played to the end: {len(ended)}")

    print("\nPLAYER")
    print("  track start latency: " + spread([d["latency_ms"] for _, d in events(conn, "player_playing")], " ms", 0))
    stalls = [d["ms"] for _, d in events(conn, "player_stall")]
    print(f"  buffering stalls: {len(stalls)}" + (f", {spread(stalls, ' ms', 0)}" if stalls else ""))
    for kind in ("player_audio_error", "player_page_error"):
        errs = events(conn, kind)
        print(f"  {kind.removeprefix('player_').replace('_', ' ')}s: {len(errs)}")
        for msg, n in Counter(d.get("message") or str(d.get("code")) for _, d in errs).most_common(5):
            print(f"    {n:3d}× {msg}")
    print("  scoring the pool (per track shown): " + spread([d["score_ms"] for _, d in events(conn, "shown")], " ms"))

    print("\nRE-WEIGHTING")
    rw = events(conn, "reweighted")
    for _, d in rw:
        print(f"  at {d['at_ratings']:3d} ratings: {d['seconds'] * 1000:.0f} ms, {d['pairs']} pairs,"
              f" concordance {d['old_concordance'] or 0:.3f} → {d['new_concordance'] or 0:.3f}")
    if not rw:
        print("  none yet")

    print("\nPOOL PIPELINE (per generation)")
    ready = events(conn, "track_ready")
    failed = events(conn, "track_failed")
    for g in conn.execute("SELECT * FROM generations ORDER BY id"):
        st = json.loads(g["stats"] or "{}")
        api = st.get("api") or {}
        api_txt = " · ".join(f"{k} {v['calls']} calls/{v['errors']} errors/{v['seconds']:.0f}s"
                             for k, v in api.items() if v)
        r = [d for _, d in ready if d.get("generation") == g["id"]]
        f = [d for _, d in failed if d.get("generation") == g["id"]]
        print(f"  generation {g['id']} (started {g['started_at'][:16]}):")
        print(f"    candidates: {st.get('candidates', '?')} in {st.get('candidate_phase_seconds', '?')} s"
              + (f" · {api_txt}" if api_txt else ""))
        print(f"    fill: {len(r)} ready, {len(f)} failed in {st.get('fill_seconds', '?')} s · pool {st.get('pool_size', '?')}")
        if r:
            print("    download: " + spread([d["download_s"] for d in r], " s"))
            print("    analysis: " + spread([d["analysis_s"] for d in r], " s"))
            print(f"    audio: {sum(d['bytes'] for d in r) / 1e6:.0f} MB, {sum(d['duration_s'] for d in r) / 3600:.1f} h")
        for reason, n in Counter(d["reason"][:90] for d in f).most_common(5):
            print(f"    {n:3d}× {reason}")

    print("\nBACKGROUND JOBS")
    jobs = [(r, d, k) for k in ("job_start", "job_done", "job_error") for r, d in events(conn, k)]
    for r, d, k in sorted(jobs, key=lambda x: x[0]["id"])[-12:]:
        extra = f" {d['seconds']} s" if "seconds" in d else ""
        err = f" — {d['error']}" if k == "job_error" else ""
        print(f"  {r['at'][:19]}  {k.removeprefix('job_'):5s} {d['job']}{extra}{err}")
    if not jobs:
        print("  none yet")
    starts = events(conn, "server_start")
    print(f"  server starts: {len(starts)}" + (f", last {starts[-1][0]['at'][:16]}" if starts else ""))

    print("\nDISK")
    for name in ("cache", "references", "keepers", "data"):
        n, mb = folder_size(name)
        print(f"  {name:10s} {n:4d} files  {mb:7.0f} MB")
    print(f"\nFull log: {path('data') / 'music-hunter.log'}")
