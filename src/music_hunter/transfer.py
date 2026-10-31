"""Moving data to the Pi and getting it back for analysis — SPEC.md §18.11 (v5).

- `bundle`: a tar of the app's data — consistent copies of the library database and every Find music session (SQLite's
  backup, safe while the app runs), plus the audio folders when moving house.
- On the Pi, an uploaded bundle is saved as /data/incoming.tar and the app restarts; the add-on's start script moves
  the old data aside (/data/previous/) and unpacks the bundle before the app starts again.
- From the PC: `migrate` (send everything to the Pi), `snapshot` (fetch the databases and log into runs/pi/<time>/
  to analyse with the usual commands), `pi log`, `pi restart`, `pi update` (via Home Assistant).
All of it goes through the same token as the PC worker (WORKER_TOKEN).
"""

import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path

import requests

from . import config
from .config import CONFIG, path

AUDIO = ("library", "cache")             # [paths] entries with audio and covers (cache/worker is the PC worker's own)


def _databases() -> list[Path]:
    files = [path("db")]
    sessions = path("data") / "sessions"
    if sessions.exists():
        files += sorted(sessions.glob("playlist-*.db"))
    return [f for f in files if f.exists()]


def bundle(dest: Path, audio: bool, log: bool = False) -> Path:
    """Write the tar to `dest`. Paths inside are relative to the data root, as stored in the databases."""
    root = config.ROOT
    with tempfile.TemporaryDirectory(dir=path("data")) as tmp, tarfile.open(dest, "w") as tar:
        for db_file in _databases():
            copy = Path(tmp) / db_file.name
            src, out = sqlite3.connect(db_file), sqlite3.connect(copy)
            with out:
                src.backup(out)
            src.close(), out.close()
            tar.add(copy, arcname=str(db_file.relative_to(root)))
        if log and (path("data") / "music-hunter.log").exists():
            tar.add(path("data") / "music-hunter.log", arcname="data/music-hunter.log")
        if audio:
            for name in AUDIO:
                folder = path(name)
                if folder.exists():
                    tar.add(folder, arcname=name, filter=lambda ti: None if "/worker" in ti.name else ti)
            undo = path("data") / "undo"
            if undo.exists():
                tar.add(undo, arcname="data/undo")
    return dest


def unpack_incoming(root: Path) -> bool:
    """Add-on start-up: if a bundle was uploaded, move the current data aside and unpack it. True if it did."""
    import shutil

    incoming = root / "incoming.tar"
    if not incoming.exists():
        return False
    previous = root / "previous"
    shutil.rmtree(previous, ignore_errors=True)
    previous.mkdir()
    for name in ("data", "library", "cache"):
        if (root / name).exists():
            shutil.move(root / name, previous / name)
    with tarfile.open(incoming) as tar:
        tar.extractall(root, filter="data")
    incoming.unlink()
    print(f"Unpacked the uploaded data (the old data is in {previous})", flush=True)
    return True


# --- the PC side --------------------------------------------------------------------------------------------------

def _app(server: str | None) -> tuple[str, dict]:
    from .analysis import worker_token

    token = worker_token()
    if not token:
        raise SystemExit("WORKER_TOKEN isn't set in .env")
    return (server or CONFIG["worker"]["server"]).rstrip("/"), {"Authorization": f"Bearer {token}"}


def migrate(server: str | None, force: bool = False) -> None:
    """Send this machine's playlists, sessions and audio to the Pi (which must be empty unless `force`)."""
    base, headers = _app(server)
    out = path("data") / "migrate.tar"
    print("Packing the databases and audio…", flush=True)
    bundle(out, audio=True)
    size = out.stat().st_size / 1e6
    print(f"Sending {size:.0f} MB to {base}…", flush=True)
    t0 = time.monotonic()
    with out.open("rb") as f:
        r = requests.post(f"{base}/api/admin/restore", params={"force": int(force)}, data=f, headers=headers,
                          timeout=3600)
    out.unlink()
    if r.status_code != 200:
        raise SystemExit(f"The Pi refused it: {r.status_code} {r.text}")
    print(f"Sent in {time.monotonic() - t0:.0f} s; the app on the Pi is restarting with this data.", flush=True)


def snapshot(server: str | None, out: Path | None = None) -> Path:
    """Fetch the Pi's databases and log into runs/pi/<time>/ (analyse with MUSIC_HUNTER_ROOT=<that folder>)."""
    base, headers = _app(server)
    out = out or config.PROJECT / "runs" / "pi" / time.strftime("%Y-%m-%d_%H%M")
    out.mkdir(parents=True, exist_ok=True)
    r = requests.get(f"{base}/api/admin/snapshot", headers=headers, timeout=600, stream=True)
    r.raise_for_status()
    tar_file = out / "snapshot.tar"
    with tar_file.open("wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
    with tarfile.open(tar_file) as tar:
        tar.extractall(out, filter="data")
    tar_file.unlink()
    print(out)
    return out


def pi(action: str, server: str | None, lines: int = 200) -> None:
    base, headers = _app(server)
    if action == "log":
        r = requests.get(f"{base}/api/admin/log", params={"lines": lines}, headers=headers, timeout=60)
        r.raise_for_status()
        print(r.text)
    elif action == "restart":
        requests.post(f"{base}/api/admin/restart", headers=headers, timeout=60).raise_for_status()
        print("Restarting.")
    elif action == "update":
        print(ha_update())


def ha_update() -> str:
    """Install the add-on's pending update through Home Assistant (needs HA_TOKEN in .env)."""
    import os

    from .sources import load_env

    load_env()
    token, url = os.environ.get("HA_TOKEN"), CONFIG["pi"]["home_assistant"].rstrip("/")
    if not token:
        raise SystemExit("HA_TOKEN isn't set in .env (a long-lived access token from your Home Assistant profile)")
    headers = {"Authorization": f"Bearer {token}"}
    states = requests.get(f"{url}/api/states", headers=headers, timeout=30)
    states.raise_for_status()
    hits = [s for s in states.json() if s["entity_id"].startswith("update.") and "music_hunter" in s["entity_id"]]
    if not hits:
        return "No Music Hunter update entity found in Home Assistant"
    u = hits[0]
    a = u.get("attributes", {})
    if u["state"] != "on":
        return f"Up to date ({a.get('installed_version')})"
    requests.post(f"{url}/api/services/update/install", json={"entity_id": u["entity_id"]}, headers=headers,
                  timeout=900).raise_for_status()
    return f"Updated {a.get('installed_version')} → {a.get('latest_version')}"
