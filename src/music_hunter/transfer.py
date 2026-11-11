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
import sys
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


# --- pushed code (`pi push`): the PC's working tree running on the Pi without a release ---------------------------
# Kept in /data/code (src/music_hunter and config.toml, as in the project), tied to the release it was pushed onto:
# once a different add-on version is installed, the release wins and the pushed code is moved aside.

def prepare_code(root: Path, release: Path) -> Path:
    """Add-on start-up: unpack pushed code (or drop it on `pi revert`); the folder to run the app from."""
    import shutil

    code, incoming, revert = root / "code", root / "code-incoming.tar", root / "code-revert"
    version = (release / "VERSION").read_text().strip() if (release / "VERSION").exists() else "?"
    if revert.exists():
        shutil.rmtree(code, ignore_errors=True)
        shutil.rmtree(root / "code.failed", ignore_errors=True)
        revert.unlink()
        print("Back on the released code", file=sys.stderr, flush=True)
    if incoming.exists():
        shutil.rmtree(code, ignore_errors=True)
        shutil.rmtree(root / "code.failed", ignore_errors=True)
        code.mkdir()
        with tarfile.open(incoming) as tar:
            tar.extractall(code, filter="data")
        (code / "BASE").write_text(version)
        incoming.unlink()
        print(f"Unpacked pushed code onto {version}", file=sys.stderr, flush=True)
    if code.exists():
        base = (code / "BASE").read_text().strip() if (code / "BASE").exists() else ""
        if base == version and (code / "src" / "music_hunter").is_dir():
            return code / "src"
        shutil.rmtree(root / "code.superseded", ignore_errors=True)
        shutil.move(code, root / "code.superseded")
        print(f"Pushed code was for {base or '?'}, not {version}: running the release", file=sys.stderr, flush=True)
    return release / "src"


def code_failed(root: Path) -> None:
    """The pushed code didn't start: set it aside (code.failed) so the release runs instead."""
    import shutil

    shutil.rmtree(root / "code.failed", ignore_errors=True)
    if (root / "code").exists():
        shutil.move(root / "code", root / "code.failed")
    print("Pushed code failed to start; running the release (it's in code.failed)", file=sys.stderr, flush=True)


def code_bundle(dest: Path) -> dict:
    """The working tree's app code and config.toml as a tar.gz, with a label saying what it is."""
    import io
    import json
    import py_compile
    import subprocess

    src = config.PROJECT / "src" / "music_hunter"
    files = [p for p in src.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    for p in files:
        if p.suffix == ".py":
            py_compile.compile(str(p), doraise=True, cfile=str(Path(tempfile.gettempdir()) / "mh-check.pyc"))
    git = subprocess.run(["git", "describe", "--always", "--dirty", "--tags"], cwd=config.PROJECT,
                         capture_output=True, text=True).stdout.strip()
    label = {"git": git or "?", "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with tarfile.open(dest, "w:gz") as tar:
        for p in files:
            tar.add(p, arcname=str(p.relative_to(config.PROJECT)))
        tar.add(config.PROJECT / "config.toml", arcname="config.toml")
        data = json.dumps(label).encode()
        info = tarfile.TarInfo("PUSHED.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return label


def _wait_for_code(base: str, headers: dict, want, timeout: float = 120) -> dict:
    """After a restart: the Pi's code status once `want(status)` holds."""
    t0 = time.monotonic()
    time.sleep(2)
    while time.monotonic() - t0 < timeout:
        try:
            r = requests.get(f"{base}/api/admin/code", headers=headers, timeout=5)
            if r.status_code == 200 and want(r.json()):
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(1)
    raise SystemExit("The Pi didn't come back as expected; see `music-hunter pi log`")


def _describe_code(s: dict) -> str:
    if s.get("failed") and not s.get("pushed"):
        return (f"Pushed code {s['failed']['git']} failed to start, so the Pi runs release {s['release']};"
                " see `music-hunter pi log`")
    if s.get("pushed"):
        return f"Pi runs pushed code {s['pushed']['git']} ({s['pushed']['at']}) on release {s['release']}"
    return f"Pi runs release {s['release']}"


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
    elif action == "app":
        print(publish_app(base, headers))
    elif action == "push":
        out = path("data") / "code.tar.gz"
        label = code_bundle(out)
        with out.open("rb") as f:
            r = requests.post(f"{base}/api/admin/code", data=f, headers=headers, timeout=120)
        out.unlink()
        if r.status_code != 200:
            raise SystemExit(f"The Pi refused it: {r.status_code} {r.text}")
        print(f"Sent {label['git']}; the app on the Pi is restarting…", flush=True)
        print(_describe_code(_wait_for_code(base, headers, lambda s: s.get("pushed") == label
                                            or s.get("failed") == label)))
    elif action == "revert":
        requests.delete(f"{base}/api/admin/code", headers=headers, timeout=60).raise_for_status()
        print("Restarting on the release…", flush=True)
        print(_describe_code(_wait_for_code(base, headers, lambda s: not s.get("pushed"))))
    elif action == "code":
        r = requests.get(f"{base}/api/admin/code", headers=headers, timeout=60)
        r.raise_for_status()
        print(_describe_code(r.json()))
    elif action == "feedback":
        r = requests.get(f"{base}/api/admin/feedback", headers=headers, timeout=60)
        r.raise_for_status()
        for f in r.json():
            where = " ".join(f"{k}={v}" for k, v in f["context"].items() if v is not None)
            print(f"#{f['id']} {f['at']} [{f['status']}] {f['page'] or ''} {where}\n  {f['text']}\n")


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


def publish_app(base: str, headers: dict) -> str:
    """Send the latest signed build of the Android app to the Pi, where the app on the phone finds it (§19)."""
    import json

    out = config.PROJECT / "android" / "app" / "build" / "outputs" / "apk" / "release"
    meta = json.loads((out / "output-metadata.json").read_text())["elements"][0]
    apk = out / meta["outputFile"]
    r = requests.post(f"{base}/api/admin/app", params={"version_code": meta["versionCode"],
                      "version_name": meta["versionName"]}, data=apk.read_bytes(), headers=headers, timeout=300)
    if r.status_code != 200:
        raise SystemExit(f"The Pi refused it: {r.status_code} {r.text}")
    return f"Version {meta['versionName']} ({meta['versionCode']}) is on the Pi; the app offers it at its next launch."
