"""Rekordbox's own beat grids, read back from the laptop (2026-11-14, feedback #54, my choice): our phrase marks go on
Rekordbox's grid rather than ours, since its bar 1 is right more often (checked against my hand-set drops: our phrase
lines passed through 12 of 14 on its bars, 9 on ours).

I export Rekordbox's collection (File > Export Collection in xml format) into a folder in my Songs share; Syncthing
brings it to the Pi, which reads each synced song's grid and cues from it and deletes the file. The PC then finds the
phrases on those bars (grid.phrases), and rekordbox.xml carries memory cues every [grid] cue_bars bars, counted from
my own hot cue A when I've set one on a downbeat (my drop), else from the drop the PC found. Rekordbox's grid and my
own cues are written back as they were, so importing rekordbox.xml doesn't change them.
"""

import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET
from xml.sax.saxutils import quoteattr

from . import db
from .config import CONFIG, path

OURS = re.compile(r"^([+-]\d+|0|Drop)$")      # names of the memory cues we write (and the hot cue A we once wrote)
_failed: set = set()
_seen: set = set()


def inbox() -> Path:
    return Path(CONFIG["sync"]["rekordbox_inbox"])


def _shift() -> float:
    return CONFIG.get("grid", {}).get("rekordbox_shift_s", 0.048)


def _synced() -> dict:
    """Every synced file, by its path on the laptop (lower case): (track id, the audio it was copied from)."""
    laptop = CONFIG["sync"]["laptop_folder"].replace("\\", "/").rstrip("/")
    out = {}
    for f in (path("data") / "sync").glob("*.json"):
        st = json.loads(f.read_text())
        for tid, d in st.get("tracks", {}).items():
            out[f"{laptop}/{st['folder']}/{d['file']}".lower()] = (int(tid), d["source"])
    return out


def _location(loc: str) -> str:
    loc = unquote(loc)
    for p in ("file://localhost/", "file:///"):
        if loc.startswith(p):
            loc = loc[len(p):]
    return loc.replace("\\", "/").lower()


def _is_ours(m: dict) -> bool:
    return bool(OURS.match(m.get("Name", ""))) and m.get("Num") in ("-1", "0")


def import_file(lib, f: Path) -> dict:
    """Read one Rekordbox collection export: each synced song's grid (TEMPO markers) and my own cues."""
    synced = _synced()
    found = changed = 0
    root = ET.parse(f).getroot()
    for t in root.iterfind("./COLLECTION/TRACK"):
        hit = synced.get(_location(t.get("Location", "")))
        if hit is None:
            continue
        tid, source = hit
        markers = [[float(m.get("Inizio")), float(m.get("Bpm")), int(m.get("Battito", "1"))] for m in t.iterfind("TEMPO")]
        if not markers:
            continue                                 # not analysed in Rekordbox yet
        cues = [dict(m.attrib) for m in t.iterfind("POSITION_MARK")]
        new = {"source": source, "duration": float(t.get("TotalTime") or 0), "markers": markers,
               "cues": [c for c in cues if not _is_ours(c)]}
        row = lib.execute("SELECT rb_grid FROM tracks WHERE id = ?", (tid,)).fetchone()
        if row is None:
            continue
        old = json.loads(row["rb_grid"]) if row["rb_grid"] else None
        found += 1
        with lib:
            if old is None or old["markers"] != markers or old["source"] != source:
                changed += 1                         # the PC finds the phrases on the new bars
                lib.execute("UPDATE tracks SET rb_grid = ?, rb_grid_at = datetime('now') WHERE id = ?",
                            (json.dumps(new), tid))
            elif old != new:
                lib.execute("UPDATE tracks SET rb_grid = ? WHERE id = ?", (json.dumps(new), tid))
    return {"songs": found, "new_grids": changed}


def valid(rb: dict | None, file_path: str | None) -> bool:
    """Rekordbox's grid still describes the audio we have (not if the song was swapped for another version since)."""
    return bool(rb and rb.get("markers") and rb.get("source") == file_path)


def downbeats(rb: dict, duration: float) -> list[float]:
    from .grid import rekordbox_downbeats

    return rekordbox_downbeats(rb["markers"], max(duration, rb.get("duration") or 0) + 1)


def key(rb: dict) -> str:
    """Which grid a phrase result belongs to."""
    return json.dumps(rb["markers"])


def marks(rb: dict, grid: dict | None, duration: float) -> str:
    """rekordbox.xml's part for one song: Rekordbox's grid and my cues as they were, plus memory cues every cue_bars
    bars from the anchor: my hot cue A on a downbeat, else the drop the PC found on these bars."""
    g = CONFIG.get("grid", {})
    bars = downbeats(rb, duration)
    anchor, name = None, "0"
    a = next((c for c in rb["cues"] if c.get("Num") == "0"), None)
    if a is not None and bars:
        t = float(a["Start"])
        i = min(range(len(bars)), key=lambda j: abs(bars[j] - t))
        if abs(bars[i] - t) < 0.1:
            anchor, name = i, "Drop"
    if anchor is None and grid and grid.get("rb") == key(rb) and grid.get("anchor_bar") is not None:
        anchor = int(grid["anchor_bar"])
    out = [f'      <TEMPO Inizio="{m[0]:.3f}" Bpm="{m[1]:.2f}" Metro="4/4" Battito="{m[2]}"/>' for m in rb["markers"]]
    out += ["      <POSITION_MARK " + " ".join(f"{k}={quoteattr(v)}" for k, v in c.items()) + "/>" for c in rb["cues"]]
    if anchor is not None:
        step = g.get("cue_bars", 16)
        bar = 240.0 / rb["markers"][0][1]
        n = 0
        for i in range(anchor % step, len(bars), step):
            if bars[i] >= duration - bar or n >= g.get("max_memory_cues", 20):
                break
            label = name if i == anchor else f"{(i - anchor):+d}"
            out.append(f'      <POSITION_MARK Name="{label}" Type="0" Start="{bars[i]:.3f}" Num="-1"/>')
            n += 1
    return "\n" + "\n".join(out) + "\n    "


def check() -> None:
    """Read any export waiting in the folder, then delete it. A file still being written (changed in the last 15 s)
    waits for the next look."""
    box = inbox()
    if not box.parent.exists():
        return                                       # (not on the Pi)
    if not box.exists():
        box.mkdir()
        db.log_event(db.connect_library(), "rekordbox_inbox_made", folder=str(box))
    for f in sorted(box.iterdir()):
        if f.name not in _seen:                      # what arrives (Syncthing's own temporary files included)
            _seen.add(f.name)
            db.log_event(db.connect_library(), "rekordbox_inbox_file", file=f.name, bytes=f.stat().st_size)
        if f.name.startswith(".") or f.suffix.lower() != ".xml" or not f.is_file():
            continue
        st = f.stat()
        if time.time() - st.st_mtime < 15 or (f.name, st.st_mtime, st.st_size) in _failed:
            continue
        lib = db.connect_library()
        try:
            res = import_file(lib, f)
        except Exception as e:  # noqa: BLE001 — a damaged file stays (and isn't tried again until it changes)
            _failed.add((f.name, st.st_mtime, st.st_size))
            db.log_event(lib, "rekordbox_import_failed", file=f.name, error=str(e)[:300])
            continue
        f.unlink(missing_ok=True)
        db.set_setting(lib, "rekordbox_last_import", {"file": f.stem, **res})   # named in rekordbox.xml (sync.py)
        db.log_event(lib, "rekordbox_import", file=f.name, **res)
        from . import sync
        sync.request()


def _watch() -> None:
    while True:
        try:
            check()
        except Exception as e:  # noqa: BLE001 — never stops
            db.log_event(db.connect_library(), "rekordbox_watch_failed", error=str(e)[:200])
        time.sleep(CONFIG["sync"].get("rekordbox_check_s", 30))


def start() -> None:
    box = inbox()
    db.log_event(db.connect_library(), "rekordbox_inbox", folder=str(box), share=box.parent.exists(), exists=box.exists(),
                 files=sorted(f.name for f in box.iterdir()) if box.exists() else None)
    threading.Thread(target=_watch, daemon=True, name="rekordbox-inbox").start()
