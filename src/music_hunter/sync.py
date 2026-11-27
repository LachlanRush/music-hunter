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
    _save(pid, {**st, "folder": rel, "tracks": wanted})           # (its Set to BPM folder's list kept)
    return wanted


TEMPO_TAGS = 1   # what's written into a BPM folder copy (as TAGS)


def _bpm_plan(lib, pid: int, tracks: dict) -> tuple[str | None, dict]:
    """The playlist's Set to BPM folder (#148, 2026-11-27): its name ("142 BPM") while Set to BPM is ticked, else
    None; and each synced track's rate there and the tempo it then plays at — the player's own rule (the playlist's
    target, or the track's own "Play at … BPM")."""
    from . import tempo

    s = tempo.playlist_settings(lib, pid)
    if not s["on"] or not s.get("target"):
        return None, {}
    core = tempo.playlist_tempo(lib, pid)
    overrides = dict(lib.execute("SELECT track_id, bpm_override FROM playlist_tracks WHERE playlist_id = ?"
                                 " AND bpm_override IS NOT NULL", (pid,)).fetchall())
    around = core["center"] if core else s["target"]
    plan = {}
    for tid, d in tracks.items():
        bpm, own = d.get("bpm"), overrides.get(int(tid))
        rate = (own and tempo.override_rate(bpm, own, around)) or tempo.rate_for(bpm, s)[0]
        plays_at = (own or s["target"]) if rate != 1.0 else bpm and tempo.nearest(bpm, s["target"])
        plan[tid] = {"rate": round(rate, 3), "bpm": round(plays_at, 2) if plays_at else None}
    return f"{s['target']:g} BPM", plan


def _render(src: Path, art: Path | None, dest: Path, rate: float, title: str, artist: str) -> None:
    """A copy stretched to `rate`, pitch kept (Rubber Band, as the player's copies), as 256 kbps AAC for Rekordbox."""
    tmp = _temp(dest)
    cmd = ["nice", "-n", "15", "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    if art is not None and art.exists():
        cmd += ["-i", str(art), "-map", "0:a", "-map", "1:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    else:
        cmd += ["-map", "0:a"]
    cmd += ["-af", f"rubberband=tempo={rate}:pitchq=quality", "-c:a", "aac", "-b:a", "256k", "-map_metadata", "-1",
            "-metadata", f"title={title}", "-metadata", f"artist={artist}", "-f", "mp4", str(tmp)]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def sync_bpm_folder(lib, pid: int, stop=lambda: False) -> dict | None:
    """Bring the playlist's Set to BPM folder up to date: <playlist folder>/<target> BPM/, every synced track at the
    tempo the player plays it at. Gone (only the files put there) when Set to BPM is unticked or the target changes.
    Rendering takes a while on the Pi, so `stop()` is asked between tracks (a change waiting: the plain folders go
    first) and what's done is kept for the next pass. Returns {"name", "tracks"} or None."""
    st = _load(pid)
    if not st["folder"]:
        return None
    folder = Path(CFG["folder"]) / st["folder"]
    name, plan = _bpm_plan(lib, pid, st["tracks"])
    old = st.get("bpm") or {"name": None, "tracks": {}}
    if old["name"] and old["name"] != name:                       # unticked, or another target: its files go
        sub = folder / old["name"]
        for d in old["tracks"].values():
            (sub / d["file"]).unlink(missing_ok=True)
        if sub.exists() and not any(sub.iterdir()):
            sub.rmdir()
        old = {"name": None, "tracks": {}}
        st["bpm"] = None
        _save(pid, st)
    if name is None:
        return None
    sub = folder / name
    sub.mkdir(parents=True, exist_ok=True)
    kept, done = old["tracks"], {}
    rows = {str(r["id"]): r for r in lib.execute(
        f"SELECT id, art_path FROM tracks WHERE id IN ({','.join('?' * len(plan)) or 'NULL'})", [int(t) for t in plan])}
    for tid, d in st["tracks"].items():
        p = plan[tid]
        have = kept.get(tid)
        if (have and have["source"] == d["source"] and have["rate"] == p["rate"] and have["file"] == d["file"]
                and have.get("tags") == TEMPO_TAGS and (sub / d["file"]).exists()):
            done[tid] = {**have, "bpm": p["bpm"], "stars": d["stars"]}
            continue
        if stop():
            break
        art = rows.get(tid) and rows[tid]["art_path"]
        art = resolve(art) if art else None
        try:
            if p["rate"] == 1.0:
                _tag(resolve(d["source"]), art, sub / d["file"], d["title"], d["artist"])
            else:
                _render(resolve(d["source"]), art, sub / d["file"], p["rate"], d["title"], d["artist"])
        except subprocess.CalledProcessError as e:
            db.log_event(lib, "sync_bpm_failed", playlist_id=pid, track_id=int(tid),
                         error=(e.stderr or b"").decode(errors="replace")[-300:])
            continue
        if have and have["file"] != d["file"]:
            (sub / have["file"]).unlink(missing_ok=True)
        done[tid] = {"file": d["file"], "source": d["source"], "rate": p["rate"], "tags": TEMPO_TAGS, "bpm": p["bpm"],
                     "stars": d["stars"], "artist": d["artist"], "title": d["title"], "duration_s": d["duration_s"],
                     "size": (sub / d["file"]).stat().st_size}
        st["bpm"] = {"name": name, "tracks": {**{k: v for k, v in kept.items() if k not in done}, **done}}
        _save(pid, st)                                           # (kept track by track: a restart carries on)
    else:
        for tid, d in kept.items():
            if tid not in done:
                (sub / d["file"]).unlink(missing_ok=True)        # it left the playlist (or its folder)
        st["bpm"] = {"name": name, "tracks": done}
        _save(pid, st)
    return {"name": name, "tracks": {k: v for k, v in (st.get("bpm") or {}).get("tracks", {}).items() if k in st["tracks"]}}


def _marks(grid: dict | None, duration: float, rb: dict | None = None, source: str | None = None) -> str:
    """A track's beat grid and phrase marks for Rekordbox (feedback #54). On Rekordbox's own grid when it has sent it
    (rekordbox.py; [grid] only_on_rekordbox: only then); else our grid (TEMPO: the first bar's first beat and the
    tempo), a hot cue A on the drop, and memory cues every [grid] cue_bars bars counted from the drop, named by their
    distance from it ("-16", "Drop", "+16" …), at most [grid] max_memory_cues."""
    from . import rekordbox

    g = CONFIG.get("grid", {})
    if not g.get("write_xml", True):
        return ""
    if rekordbox.valid(rb, source):
        return rekordbox.marks(rb, grid, duration)
    if not grid or grid.get("rb") or g.get("only_on_rekordbox", False):
        return ""
    # Rekordbox counts time from the start of the M4A's audio including the encoder's lead-in (2112 samples, 48 ms),
    # which our decoder skips: measured on 32 songs, its beats sat 48 ms after ours (2026-11-14).
    shift = g.get("rekordbox_shift_s", 0.048)
    grid = {**grid, "first_beat": grid["first_beat"] + shift, "drop": grid["drop"] + shift}
    bar = 60.0 / grid["bpm"] * 4
    step = bar * g.get("cue_bars", 16)
    first = grid["drop"] - step * int(grid["drop"] // step)
    cues = []
    t = first
    while t < duration - bar and len(cues) < g.get("max_memory_cues", 10):
        n = round((t - grid["drop"]) / bar)
        cues.append(f'      <POSITION_MARK Name="{"Drop" if n == 0 else f"{n:+d}"}" Type="0" Start="{t:.3f}" Num="-1"/>')
        t += step
    return (f'\n      <TEMPO Inizio="{grid["first_beat"]:.3f}" Bpm="{grid["bpm"]:.2f}" Metro="4/4" Battito="1"/>'
            f'\n      <POSITION_MARK Name="Drop" Type="0" Start="{grid["drop"]:.3f}" Num="0"/>\n' + "\n".join(cues) + "\n    ")


def _rekordbox(playlists: list[tuple], grids: dict | None = None, last: dict | None = None) -> str:
    """rekordbox.xml: every synced track with its stars (0, 51 … 255) and the playlists, inside their home page
    folders (as Rekordbox folders), for File > Import. `last`: the latest Rekordbox export read back (rekordbox.py),
    named at the top of the list with how many of its songs have their marks yet, so I can tell it has come back."""
    laptop = CFG["laptop_folder"].replace("\\", "/").rstrip("/")
    seen, entries, nodes = {}, [], []
    groups: dict = {}
    marked = 0
    for name, folder, tracks, *parent in playlists:
        keys = []
        for tid, d in tracks.items():
            if tid not in seen:
                seen[tid] = str(len(seen) + 1)
                if d.get("rate"):                                 # (a Set to BPM copy: its own length and tempo)
                    d = {**d, "duration_s": (d["duration_s"] or 0) / d["rate"], "bpm": d["bpm"]}
                where = quote(f"{laptop}/{folder}/{d['file']}", safe="/:")
                entries.append(
                    f'    <TRACK TrackID="{seen[tid]}" Name={quoteattr(d["title"])} Artist={quoteattr(d["artist"])}'
                    f' Kind="M4A File" Size="{d["size"]}" TotalTime="{round(d["duration_s"] or 0)}"'
                    f' AverageBpm="{d["bpm"] or 0:.2f}" Rating="{(d["stars"] or 0) * 51}"'
                    f' Location="file://localhost/{where}"'
                    + (f'>{marks}</TRACK>' if not d.get("rate") and (marks := _marks(
                        (gr := (grids or {}).get(tid, (None, None)))[0], d["duration_s"] or 0, gr[1], d["source"]))
                       else (marks := "") or '/>'))
                marked += 'Num="-1"' in marks
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
    if last and CONFIG.get("grid", {}).get("write_xml", True):   # (an empty playlist, just its name)
        nodes.insert(0, f'    <NODE Name={quoteattr(f"Marks from {last['file']}: {marked} of {last['songs']} songs")}'
                        ' Type="1" KeyType="0" Entries="0">\n    </NODE>')
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
            done.append((p["id"], p["name"], _load(p["id"])["folder"], tracks, parent))
    if not done:
        return
    _write_xml(lib, done, _bpm_entries(done, {p: _load(p).get("bpm") for p, *_ in done}))
    # Then the Set to BPM folders (#148): slow on the Pi, so a change waiting stops them between tracks and the plain
    # folders go first; the next pass carries on.
    bpm = {}
    for p, *_ in done:
        bpm[p] = sync_bpm_folder(lib, p, stop=lambda: _again)
        if _again:
            return
    _write_xml(lib, done, _bpm_entries(done, bpm))


def _bpm_entries(done: list, bpm: dict) -> dict:
    """{playlist id: (name, folder, tracks, parent)} of each Set to BPM folder, for rekordbox.xml."""
    out = {}
    for p, name, folder, tracks, parent in done:
        b = bpm.get(p)
        if b and b.get("name"):
            out[p] = (f"{name} ({b['name']})", f"{folder}/{b['name']}",
                      {f"{k}@bpm": v for k, v in b["tracks"].items() if k in tracks}, parent)
    return out


def _write_xml(lib, done: list, bpm: dict) -> None:
    xml = Path(CFG["folder"]) / "rekordbox.xml"
    tmp = _temp(xml)
    grids = {str(r["id"]): (json.loads(r["grid"]) if r["grid"] else None, json.loads(r["rb_grid"]) if r["rb_grid"] else None)
             for r in lib.execute("SELECT id, grid, rb_grid FROM tracks WHERE grid IS NOT NULL OR rb_grid IS NOT NULL")}
    lists = []
    for p, *rest in done:
        lists.append(tuple(rest))
        if p in bpm:
            lists.append(bpm[p])
    tmp.write_text(_rekordbox(lists, grids, db.get_setting(lib, "rekordbox_last_import")), encoding="utf-8")
    tmp.replace(xml)


def request() -> None:
    """Update the folders in the background; a change while it runs gets one more pass."""
    global _thread, _again
    if shutil.which("ffmpeg") is None:
        return
    with _lock:
        if _thread is not None:                      # running, or just started (it clears this when it ends)
            _again = True
            return
        _thread = t = threading.Thread(target=_work, daemon=True)
    t.start()                                        # (outside the lock: the pass takes it when it ends)


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
