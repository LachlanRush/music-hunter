"""Background jobs — SPEC.md §18.4 (v5).

Jobs are queued in the database and run one at a time: for now by a runner thread inside the web server; from
milestone 3, heavy jobs by the PC worker. A job reports progress ({done, total, message}) as it goes; one that was
running when the server stopped is queued again on start (jobs are safe to re-run).
"""

import json
import sqlite3
import threading
import time
import traceback

from . import db


def enqueue(conn: sqlite3.Connection, kind: str, playlist_id: int | None = None, **payload) -> int:
    with conn:
        return conn.execute("INSERT INTO jobs (kind, playlist_id, payload) VALUES (?, ?, ?)",
                            (kind, playlist_id, json.dumps(payload))).lastrowid


def get(conn: sqlite3.Connection, job_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row(r) if r else None


def latest_for(conn: sqlite3.Connection, playlist_id: int, kind: str | None = None) -> dict | None:
    r = conn.execute("SELECT * FROM jobs WHERE playlist_id = ?" + (" AND kind = ?" if kind else "")
                     + " ORDER BY id DESC LIMIT 1", (playlist_id, kind) if kind else (playlist_id,)).fetchone()
    return _row(r) if r else None


def _row(r) -> dict:
    return {**dict(r), "payload": json.loads(r["payload"]), "progress": json.loads(r["progress"])}


def _set(conn: sqlite3.Connection, job_id: int, **fields) -> None:
    if "progress" in fields:
        fields["progress"] = json.dumps(fields["progress"])
    with conn:
        conn.execute(f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in fields)}, updated_at = datetime('now')"
                     " WHERE id = ?", (*fields.values(), job_id))


def requeue_interrupted(conn: sqlite3.Connection) -> int:
    with conn:
        return conn.execute("UPDATE jobs SET state = 'queued' WHERE state = 'running'").rowcount


def run_job(conn: sqlite3.Connection, job: dict) -> None:
    """Run one job to completion, recording progress and the outcome."""
    from . import library

    _set(conn, job["id"], state="running")
    progress = {"done": 0, "total": 0, "message": "Starting…"}

    def report(done, total, message):
        progress.update(done=done, total=total, message=message)
        _set(conn, job["id"], progress=progress)

    db.log_event(conn, "job_start", job=job["kind"], job_id=job["id"])
    t0 = time.monotonic()
    try:
        if job["kind"] == "import":
            result = library.import_spotify(conn, job["payload"]["link"], job["playlist_id"], report)
            progress.update(result)
        else:
            raise ValueError(f"Unknown job kind {job['kind']!r}")
        _set(conn, job["id"], state="done", progress=progress)
        db.log_event(conn, "job_done", job=job["kind"], job_id=job["id"], seconds=round(time.monotonic() - t0, 1))
    except Exception as e:
        _set(conn, job["id"], state="failed", error=f"{type(e).__name__}: {e}", progress=progress)
        db.log_event(conn, "job_error", job=job["kind"], job_id=job["id"], error=str(e)[:300],
                     traceback=traceback.format_exc())


class Runner:
    """Runs queued jobs one at a time in a background thread (started when there's work)."""

    def __init__(self):
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()

    def kick(self) -> None:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()

    def _loop(self) -> None:
        conn = db.connect()
        while True:
            r = conn.execute("SELECT * FROM jobs WHERE state = 'queued' ORDER BY id LIMIT 1").fetchone()
            if r is None:
                return
            run_job(conn, _row(r))
