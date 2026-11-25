"""Heavy background work in its own processes (2026-11-24, my note: listening comes first).

The app is one Python process: threads in it can only run Python code one at a time, so re-scoring a playlist's
Find music (the veto and the lanes, ~1 s on the Pi, for every playlist after each rating) took time away from serving
songs — on 21 Nov songs took 8–20 s to start while stars were being caught up with. veto.update now runs in one of
a few separate processes ([ui] background_processes) at a lower CPU priority (nice), so the web side only serves the
player, and the Pi's other cores do the rest. A call made inside a database transaction (its writes not visible to
another process yet), on a database that isn't a playlist's Find music, or with [ui] background_process off, runs
where it's called, as before.
"""

import os
import re
import sqlite3
import threading

from .config import CONFIG

_pool = None
_lock = threading.Lock()
IN_CHILD = False


def _init() -> None:
    global IN_CHILD
    IN_CHILD = True
    # Leave with the app (2026-11-25): a worker holds its own queue's write end, so when the app is killed (or exits
    # with os._exit to restart) the worker never sees the end of its queue and waits for ever, with its resource
    # tracker. Every restart left one behind (~60 MB); on the Pi they filled the memory, the kernel killed the app,
    # which left more behind: an hour of restarts on 25 Nov, and again that night.
    parent = os.getppid()

    def watch():
        import time
        while os.getppid() == parent:
            time.sleep(2)
        os._exit(0)
    threading.Thread(target=watch, daemon=True, name="parent-watch").start()
    try:
        os.nice(CONFIG["ui"].get("background_nice", 10))
    except OSError:
        pass
    from . import db
    db.setup_logging()


def _executor():
    global _pool
    with _lock:
        if _pool is None:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor
            _pool = ProcessPoolExecutor(max_workers=CONFIG["ui"].get("background_processes", 2),
                                        mp_context=multiprocessing.get_context("spawn"), initializer=_init)
        return _pool


def stop() -> None:
    """Stop the background processes now (before the app exits to restart)."""
    global _pool
    with _lock:
        pool, _pool = _pool, None
    if pool is None:
        return
    for proc in list((getattr(pool, "_processes", None) or {}).values()):
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception:  # noqa: BLE001
        pass


def reap_orphans() -> list[int]:
    """At start, in the add-on only: kill background processes a previous run of the app left behind (orphans, now
    children of the container's init) — Python's multiprocessing workers and resource trackers."""
    if not os.environ.get("MUSIC_HUNTER_RELEASE"):
        return []
    import signal
    killed = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == os.getpid():
            continue
        try:
            ppid = int(open(f"/proc/{d}/stat").read().rsplit(")", 1)[1].split()[1])
            cmd = open(f"/proc/{d}/cmdline", "rb").read().replace(b"\0", b" ")
        except (OSError, IndexError, ValueError):
            continue
        if ppid == 1 and b"python" in cmd and b"multiprocessing" in cmd:
            try:
                os.kill(int(d), signal.SIGKILL)
                killed.append(int(d))
            except OSError:
                pass
    return killed


def _session_of(conn: sqlite3.Connection) -> int | None:
    """The playlist whose Find music database this is, or None."""
    file = conn.execute("PRAGMA database_list").fetchone()[2] or ""
    m = re.search(r"playlist-(\d+)\.db$", file)
    return int(m.group(1)) if m else None


def _veto_update(pid: int) -> dict:
    from . import db, veto
    db.SESSION.set(pid)
    conn = db.connect(pid)
    try:
        return veto.update_here(conn)
    finally:
        conn.close()


def veto_update(conn: sqlite3.Connection) -> dict:
    """veto.update, in a background process when it can be."""
    from . import veto
    pid = None if IN_CHILD or conn.in_transaction or not CONFIG["ui"].get("background_process", True) \
        else _session_of(conn)
    if pid is None:
        return veto.update_here(conn)
    global _pool
    try:
        return _executor().submit(_veto_update, pid).result()
    except Exception as e:  # noqa: BLE001 — a broken process pool: start a new one next time, do this one here
        from concurrent.futures.process import BrokenProcessPool
        if isinstance(e, BrokenProcessPool):
            with _lock:
                _pool = None
            return veto.update_here(conn)
        raise


def _groups_update(pid: int) -> dict:
    from . import db, groups
    db.SESSION.set(pid)
    conn = db.connect(pid)
    try:
        return groups.update(conn)
    finally:
        conn.close()


def groups_update(conn: sqlite3.Connection) -> dict:
    """groups.update (§20.9), in a background process when it can be, as veto_update."""
    from . import groups
    pid = None if IN_CHILD or conn.in_transaction or not CONFIG["ui"].get("background_process", True) \
        else _session_of(conn)
    if pid is None:
        return groups.update(conn)
    global _pool
    try:
        return _executor().submit(_groups_update, pid).result()
    except Exception as e:  # noqa: BLE001
        from concurrent.futures.process import BrokenProcessPool
        if isinstance(e, BrokenProcessPool):
            with _lock:
                _pool = None
            return groups.update(conn)
        raise
