"""Playlists on the laptop (SPEC.md §19): a Sync switch per playlist keeps <folder>/<playlist name>/<Artist - Title>.m4a
in my Songs share, which Syncthing keeps the same on the laptop, plus a rekordbox.xml with the playlists and stars.
A file deleted on the laptop comes back while it's still in the playlist; other songs in the share are never touched.

Only M4A files go in (Rekordbox can't read the Opus the app downloads at first), so a track appears once the PC has
fetched its M4A. To rate tracks aren't synced. A track that leaves the playlist leaves the folder, and only files this
code put there are ever deleted (each playlist's list is kept in data/sync/); switching Sync off leaves the files.
"""

import json
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import quoteattr

from . import db
from .config import CONFIG, path, resolve
from .ratings import _safe

CFG = CONFIG["sync"]
_lock = threading.Lock()
_thread: threading.Thread | None = None
_again = False


def setting(pid: int) -> str:
    return f"sync:{pid}"


def is_on(lib, pid: int) -> bool:
    return bool(db.get_setting(lib, setting(pid), False))


def set_on(lib, pid: int, on: bool) -> None:
    db.set_setting(lib, setting(pid), bool(on))
    request()


def _list_file(pid: int) -> Path:
    return path("data") / "sync" / f"{pid}.json"


def _load(pid: int) -> dict:
    f = _list_file(pid)
    return json.loads(f.read_text()) if f.exists() else {"folder": None, "tracks": {}}


def _save(pid: int, state: dict) -> None:
    f = _list_file(pid)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".part")
    tmp.write_text(json.dumps(state))
    tmp.replace(f)


def _tracks(lib, pid: int) -> list:
    """The playlist's tracks that belong on the laptop (reviewed, not To rate), best stars first."""
    return lib.execute(
        "SELECT t.id, t.artist, t.title, t.file_path, t.art_path, t.duration_s, t.bpm, t.quality, pt.stars"
        " FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id"
        " WHERE pt.playlist_id = ? AND pt.status != 'added' ORDER BY pt.stars IS NULL, pt.stars DESC, t.id",
        (pid,)).fetchall()


def state(lib, pid: int) -> dict:
    """For the Sync button: how many of the playlist's tracks are in the folder."""
    tracks, kept = _tracks(lib, pid), _load(pid)["tracks"]
    return {"on": is_on(lib, pid), "tracks": len(tracks), "ready": sum(str(t["id"]) in kept for t in tracks)}


def _temp(dest: Path) -> Path:
    """A file being written, named as Syncthing's own temporary files are, so it isn't copied half-made."""
    return dest.with_name(f".syncthing.{dest.name}.tmp")


TAGS = 2   # what's written into a copy; a copy made under an older version is written again (2: no album, 2026-11-12)


def _tag(src: Path, art: Path | None, dest: Path, title: str, artist: str) -> None:
    """A copy with its names and cover written in (same audio, no re-encoding). No album: we don't know the real one,
    and the playlist's name there made every song look like it came from an album of that name (2026-11-12)."""
    tmp = _temp(dest)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    if art is not None and art.exists():
        cmd += ["-i", str(art), "-map", "0:a", "-map", "1:v", "-disposition:v:0", "attached_pic"]
    else:
        cmd += ["-map", "0:a"]
    cmd += ["-c", "copy", "-map_metadata", "-1", "-metadata", f"title={title}", "-metadata", f"artist={artist}",
            "-f", "mp4", str(tmp)]
    subprocess.run(cmd, check=True, capture_output=True)
    tmp.replace(dest)


def sync_playlist(lib, pid: int, name: str, parent: str | None = None) -> dict:
    """Bring one playlist's folder up to date (inside its home page folder's, if it's in one: `parent`). Returns
    what's in it: {track id: details}."""
    root = Path(CFG["folder"])
    rel = f"{_safe(parent)}/{_safe(name)}" if parent else _safe(name)
    folder = root / rel
    st = _load(pid)
    if st["folder"] and st["folder"] != rel and (root / st["folder"]).exists() and not folder.exists():
        folder.parent.mkdir(parents=True, exist_ok=True)
        (root / st["folder"]).rename(folder)                     # the playlist was renamed or moved to another folder
        old_parent = (root / st["folder"]).parent
        if old_parent != root and not any(old_parent.iterdir()):
            old_parent.rmdir()                                   # (a folder it left empty)
    folder.mkdir(parents=True, exist_ok=True)
    kept = st["tracks"]
    wanted = {}
    for t in _tracks(lib, pid):
        tid, src = str(t["id"]), t["file_path"]
        if not src or Path(src).suffix.lower() != ".m4a":
            continue                                             # waiting for its M4A
        have = kept.get(tid)
        stem = _safe(f"{t['artist']} - {t['title']}")
        names = {d["file"] for k, d in kept.items() if k != tid} | {d["file"] for d in wanted.values()}
        file = f"{stem}.m4a"
        n = 2
        while file in names:
            file = f"{stem} ({n}).m4a"
            n += 1
        same = (have and have["source"] == src and have["file"] == file and have.get("tags") == TAGS
                and (folder / file).exists())
        if not same:
            _tag(resolve(src), t["art_path"] and resolve(t["art_path"]), folder / file, t["title"], t["artist"])
            if have and have["file"] != file:
                (folder / have["file"]).unlink(missing_ok=True)
        wanted[tid] = {"file": file, "source": src, "tags": TAGS, "stars": t["stars"], "artist": t["artist"], "title": t["title"],
                       "duration_s": t["duration_s"], "bpm": t["bpm"], "size": (folder / file).stat().st_size}
    for tid, d in kept.items():
        if tid not in wanted:
            (folder / d["file"]).unlink(missing_ok=True)         # it left the playlist
    _save(pid, {"folder": rel, "tracks": wanted})
    return wanted


def _rekordbox(playlists: list[tuple]) -> str:
    """rekordbox.xml: every synced track with its stars (0, 51 … 255) and the playlists, inside their home page
    folders (as Rekordbox folders), for File > Import."""
    laptop = CFG["laptop_folder"].replace("\\", "/").rstrip("/")
    seen, entries, nodes = {}, [], []
    groups: dict = {}
    for name, folder, tracks, *parent in playlists:
        keys = []
        for tid, d in tracks.items():
            if tid not in seen:
                seen[tid] = str(len(seen) + 1)
                where = quote(f"{laptop}/{folder}/{d['file']}", safe="/:")
                entries.append(
                    f'    <TRACK TrackID="{seen[tid]}" Name={quoteattr(d["title"])} Artist={quoteattr(d["artist"])}'
                    f' Kind="M4A File" Size="{d["size"]}" TotalTime="{round(d["duration_s"] or 0)}"'
                    f' AverageBpm="{d["bpm"] or 0:.2f}" Rating="{(d["stars"] or 0) * 51}"'
                    f' Location="file://localhost/{where}"/>')
            keys.append(f'      <TRACK Key="{seen[tid]}"/>')
        node = (f'    <NODE Name={quoteattr(name)} Type="1" KeyType="0" Entries="{len(keys)}">\n'
                + "\n".join(keys) + "\n    </NODE>")
        if parent and parent[0]:
            groups.setdefault(parent[0], []).append(node)
        else:
            nodes.append(node)
    for fname, inner in groups.items():
        nodes.insert(0, f'    <NODE Type="0" Name={quoteattr(fname)} Count="{len(inner)}">\n' + "\n".join(inner)
                     + "\n    </NODE>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<DJ_PLAYLISTS Version="1.0.0">\n'
            '  <PRODUCT Name="Music Hunter" Version="1" Company=""/>\n'
            f'  <COLLECTION Entries="{len(entries)}">\n' + "\n".join(entries) + "\n  </COLLECTION>\n"
            f'  <PLAYLISTS>\n  <NODE Type="0" Name="ROOT" Count="{len(nodes)}">\n' + "\n".join(nodes)
            + "\n  </NODE>\n  </PLAYLISTS>\n</DJ_PLAYLISTS>\n")


def run_once(lib) -> None:
    from . import library

    done = []
    folders = {f["id"]: f["name"] for f in library.list_folders(lib)}
    for p in library.list_playlists(lib):
        if is_on(lib, p["id"]):
            parent = folders.get(p["folder_id"])                 # the home page's folder, if it's in one
            tracks = sync_playlist(lib, p["id"], p["name"], parent)
            done.append((p["name"], _load(p["id"])["folder"], tracks, parent))
    if done:
        xml = Path(CFG["folder"]) / "rekordbox.xml"
        tmp = _temp(xml)
        tmp.write_text(_rekordbox(done), encoding="utf-8")
        tmp.replace(xml)


def request() -> None:
    """Update the folders in the background; a change while it runs gets one more pass."""
    global _thread, _again
    if shutil.which("ffmpeg") is None:
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            _again = True
            return
        _thread = threading.Thread(target=_work, daemon=True)
        _thread.start()


def _work() -> None:
    global _again, _thread
    while True:
        lib = db.connect_library()
        try:
            run_once(lib)
        except Exception as e:                                   # the next change or maintenance pass tries again
            db.log_event(lib, "sync_failed", error=str(e)[:300])
        with _lock:
            if not _again:
                _thread = None
                return
            _again = False


def wait(timeout: float = 30) -> None:
    """For tests."""
    t = _thread
    if t is not None:
        t.join(timeout)
