"""Download and analysis of one track — the heavy work (SPEC.md §18.4).

`backend().analyse(video_id, bpm_prior)` downloads a track's full audio and measures it: loudness, MAEST fingerprint,
BPM and cover art. Where that happens depends on [worker] mode:
- local: in this process (the PC on its own; also what the PC worker itself runs);
- remote: queued in the library's `work` table for the PC worker (the Pi). If no worker has asked for work lately,
  the PC is woken through Home Assistant; the worker downloads and analyses, then sends the audio, fingerprint and
  measurements back (web.py, /api/worker/…), and the caller carries on as if it had done the work itself.
Pulls, YouTube matching, the veto and scoring stay where the app runs: they're light (web requests and arithmetic).
"""

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import requests

from . import db
from .config import CONFIG, path, resolve

CFG = CONFIG["worker"]


class DownloadFailed(Exception):
    def __init__(self, message: str, **timings):
        super().__init__(message)
        self.timings = timings


class AnalysisFailed(DownloadFailed):
    pass


@dataclass
class Result:
    file: Path
    duration_s: float
    lufs: float
    peak: float
    gain: float
    embedding: object                 # embedding.TrackEmbedding (or anything with .track, .chunks, .spans)
    model: str
    bpm: float | None = None
    art: Path | None = None
    published: str | None = None
    timings: dict = field(default_factory=dict)
    notes: dict = field(default_factory=dict)     # {'bpm_failed': reason, 'art_failed': reason}


@dataclass
class Embedding:
    track: np.ndarray
    chunks: np.ndarray
    spans: list


class Local:
    """Does the work here. One track at a time per process (GPU, and paced downloads: no parallel bursts)."""

    lock = threading.Lock()
    last_download = 0.0

    def __init__(self, folder: Path | None = None):
        self.embedder = None
        self.yt = None
        self.folder = folder        # the PC worker's own folder: its files are deleted once sent, so never cache/

    def analyse(self, video_id: str, bpm_prior: float | None = None, status=None) -> Result:
        from . import artwork, audio
        from .bpm import estimate
        from .pool import download, loudness

        with Local.lock:
            if self.embedder is None:
                from .embedding import Embedder
                from .sources import YouTubeMusic
                self.embedder, self.yt = Embedder(), YouTubeMusic()
            wait = CONFIG["pool"]["download_delay_seconds"] - (time.monotonic() - Local.last_download)
            if wait > 0:
                time.sleep(wait)
            t0 = time.monotonic()
            try:
                file = download(video_id, self.folder)
            except Exception as e:  # yt-dlp raises DownloadError and friends
                raise DownloadFailed(str(e), download_s=time.monotonic() - t0) from e
            finally:
                Local.last_download = time.monotonic()
            t1 = time.monotonic()
            try:
                lufs, peak, gain = loudness(file)
                sr = self.embedder.sample_rate
                samples = audio.decode(file, sr)
                emb = self.embedder.embed_track(samples)
            except Exception as e:
                raise AnalysisFailed(str(e), download_s=t1 - t0, analysis_s=time.monotonic() - t1) from e
            notes, tempo = {}, None
            try:
                bpm_sr = CONFIG["bpm"]["analysis_sample_rate"]
                tempo = estimate(audio.decode(file, bpm_sr), bpm_sr, bpm_prior)
            except Exception as e:
                notes["bpm_failed"] = str(e)[:200]
            vocals = Vocals.measure(file)                  # §18.34: logged on the Pi for the replay test
            if vocals:
                notes["vocals"] = vocals
            t2 = time.monotonic()
            art, published = None, None
            try:
                art, published = artwork.fetch(self.yt, video_id, self.folder and self.folder / "art")
            except Exception as e:
                notes["art_failed"] = str(e)[:200]
            return Result(file, len(samples) / sr, lufs, peak, gain, emb, self.embedder.model_name, tempo, art,
                          published, {"download_s": t1 - t0, "analysis_s": t2 - t1, "waited_s": max(wait, 0)}, notes)


class Quality:
    """The PC side of playlist tracks' M4A files (SPEC.md §19). Only tracks in a playlist come here."""

    @staticmethod
    def premium_ready() -> bool:
        """My sign-in and the token tool are both there."""
        q = CONFIG["quality"]
        if not Path(q["cookies_file"]).expanduser().exists():
            return False
        try:
            return requests.get(q["token_provider"].rstrip("/") + "/ping", timeout=3).ok
        except requests.RequestException:
            return False

    @staticmethod
    def fetch(video_id: str, folder: Path) -> tuple[Path, int, str | None]:
        """(file, quality, why not the good one). A refusal by YouTube is raised, to pause as other downloads do."""
        from .pool import download

        q, why = CONFIG["quality"], "the sign-in file or token tool isn't there"
        with Local.lock:
            wait = CONFIG["pool"]["download_delay_seconds"] - (time.monotonic() - Local.last_download)
            if wait > 0:
                time.sleep(wait)
            try:
                if Quality.premium_ready():
                    try:
                        return download(video_id, folder, {
                            "format": q["format"], "cookiefile": str(Path(q["cookies_file"]).expanduser()),
                            "extractor_args": {"youtube": {"player_client": ["web_music"]}},
                        }), 2, None
                    except Exception as e:
                        if refusal(str(e)) == "blocked":
                            raise
                        why = str(e)[:200]
                return download(video_id, folder, {"format": q["free_format"]}), 1, why
            finally:
                Local.last_download = time.monotonic()


class Vocals:
    """How much of a track is vocals, and how happy, bright and approachable it sounds (SPEC.md §18.34).

    Measured by a helper in the taste-test environment (tastetest/vocals_server.py: Demucs and Essentia, which the
    app's environment doesn't carry), started once and kept running. Only where that environment exists (the PC);
    anywhere else, or if it fails, tracks are simply not measured."""

    proc = None
    broken = False

    @classmethod
    def measure(cls, file: Path) -> dict | None:
        import json
        import select
        import subprocess

        if not CONFIG.get("vocals", {}).get("enabled", True) or cls.broken:
            return None
        from .config import PROJECT

        python = PROJECT / "tastetest" / ".venv" / "bin" / "python"
        if not python.exists():
            return None
        try:
            if cls.proc is None or cls.proc.poll() is not None:
                cls.proc = subprocess.Popen([str(python), "vocals_server.py"], cwd=PROJECT / "tastetest", text=True,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                if cls._read(cls.proc, 180).get("ready") is not True:
                    raise RuntimeError("the vocals helper didn't start")
            cls.proc.stdin.write(f"{file}\n")
            cls.proc.stdin.flush()
            out = cls._read(cls.proc, CONFIG.get("vocals", {}).get("timeout_s", 120))
            return out.get("features")
        except Exception as e:
            print(f"Vocals not measured ({e}); the helper is off until the worker restarts", flush=True)
            cls.broken = True
            if cls.proc:
                cls.proc.kill()
            return None

    @staticmethod
    def _read(proc, timeout: float) -> dict:
        """The helper's next JSON answer (Essentia prints its own lines on the same output; those are skipped)."""
        import json
        import select

        end = time.monotonic() + timeout
        while time.monotonic() < end:
            ready, _, _ = select.select([proc.stdout], [], [], max(0.0, end - time.monotonic()))
            if not ready:
                break
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("the vocals helper stopped")
            if line.startswith("{"):
                return json.loads(line)
        raise TimeoutError("the vocals helper took too long")


class Remote:
    """Queues the work for the PC worker and waits for it, waking the PC when no worker is about."""

    def analyse(self, video_id: str, bpm_prior: float | None = None, status=None) -> Result:
        say = status or (lambda text: None)
        lib = db.connect_library()
        with lib:
            wid = lib.execute("INSERT INTO work (video_id, bpm_prior) VALUES (?, ?)", (video_id, bpm_prior)).lastrowid
        t0, woken = time.monotonic(), None
        try:
            while True:
                w = lib.execute("SELECT * FROM work WHERE id = ?", (wid,)).fetchone()
                if w["state"] == "done":
                    return self._collect(lib, w, time.monotonic() - t0)
                if w["state"] == "failed":
                    with lib:
                        lib.execute("UPDATE work SET state = 'collected' WHERE id = ?", (wid,))
                    err = json.loads(w["result"] or "{}")
                    cls = DownloadFailed if err.get("stage") == "download" else AnalysisFailed
                    raise cls(w["error"] or "failed on the PC", **err.get("timings", {}))
                if not worker_connected(lib):
                    if woken is None or time.monotonic() - woken > CFG["rewake_minutes"] * 60:
                        problem = wake()
                        woken = time.monotonic()
                        WAKE["problem"] = problem
                        db.log_event(lib, "pc_wake", work_id=wid, entity=WAKE.get("entity"), problem=problem)
                    say("waiting for the PC to wake up")
                elif w["state"] == "queued":
                    paused = db.get_setting(lib, "worker_paused_until") or 0
                    say(f"YouTube is refusing downloads for now; trying again in {max(1, round((paused - time.time()) / 60))} min"
                        if paused > time.time() else "waiting for the PC")
                elif w["state"] == "claimed":
                    say("downloading and analysing on the PC")      # (so a slow track shows it's being worked on)
                time.sleep(CFG["wait_poll_seconds"])
        except BaseException:
            # Not collected (stopped, or failed above): the worker's upload is refused, the item dropped.
            w = lib.execute("SELECT * FROM work WHERE id = ?", (wid,)).fetchone()
            if w["state"] in ("queued", "claimed", "done"):
                _discard(lib, w)
            raise
        finally:
            lib.close()

    @staticmethod
    def _collect(lib, w, waited: float) -> Result:
        meta = json.loads(w["result"])
        emb_file = resolve(w["embedding_path"])
        with np.load(emb_file) as z:
            emb = Embedding(z["track"], z["chunks"], [tuple(s) for s in z["spans"]])
        emb_file.unlink(missing_ok=True)
        with lib:
            lib.execute("UPDATE work SET state = 'collected' WHERE id = ?", (w["id"],))
        timings = {**meta.get("timings", {}), "remote_s": waited}
        return Result(resolve(w["file_path"]), meta["duration_s"], meta["lufs"], meta["peak"], meta["gain"], emb,
                      meta["model"], meta.get("bpm"), w["art_path"] and resolve(w["art_path"]), meta.get("published"),
                      timings, meta.get("notes", {}))


_local = Local()


def backend():
    return Remote() if CFG["mode"] == "remote" else _local


def worker_connected(lib) -> bool:
    """A worker asked for work lately, or is pausing on purpose (YouTube blocking downloads): no need to wake the PC."""
    seen = db.get_setting(lib, "worker_seen")
    paused = db.get_setting(lib, "worker_paused_until") or 0
    return (seen is not None and time.time() - seen < CFG["connected_seconds"]) or time.time() < paused


def youtube_paused(pause_minutes: float | None = None) -> bool:
    """Whether downloads are paused because YouTube refused this home connection (set by the PC worker's hand-backs
    or by the app's own downloads); with pause_minutes, start such a pause now."""
    lib = db.connect_library()
    if pause_minutes:
        db.set_setting(lib, "worker_paused_until", time.time() + pause_minutes * 60, log=False)
        db.log_event(lib, "youtube_paused", minutes=pause_minutes)
    return time.time() < (db.get_setting(lib, "worker_paused_until") or 0)


BLOCKED = ("confirm you’re not a bot", "confirm you're not a bot", "Sign in to confirm")
TEMPORARY = ("HTTP Error 403", "HTTP Error 429")


def refusal(error: str) -> str | None:
    """'blocked' (YouTube refusing every download for now), 'temporary' (one refused download), or None."""
    if any(k in error for k in BLOCKED):
        return "blocked"
    if any(k in error for k in TEMPORARY):
        return "temporary"
    return None


def wake() -> str | None:
    """Ask Home Assistant to wake the PC: turn on its Wake-on-LAN switch ([worker.wake]). Returns what went wrong,
    or None. With no entity set, the switch is looked for among Home Assistant's switches (name or ID mentioning
    wake / WoL / LAN); the one found is logged (`pc_wake`), so it can be set explicitly if the guess is wrong."""
    from .sources import load_env

    load_env()
    w = CFG["wake"]
    supervisor = os.environ.get("SUPERVISOR_TOKEN")          # inside a Home Assistant add-on
    token = os.environ.get("HA_TOKEN") or supervisor
    url = (w["url"] or ("http://supervisor/core" if supervisor else "")).rstrip("/")
    if not (url and token):
        return "Home Assistant not set up ([worker.wake] url and HA_TOKEN)"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        entity = w.get("entity_id") or find_wake_switch(url, headers)
        if isinstance(entity, list):
            return f"which switch wakes the PC? candidates: {', '.join(entity) or 'none found'}"
        domain = entity.split(".")[0]
        r = requests.post(f"{url}/api/services/{domain}/turn_on" if domain != "button" else f"{url}/api/services/button/press",
                          json={"entity_id": entity}, timeout=15, headers=headers)
        r.raise_for_status()
    except requests.RequestException as e:
        return str(e)[:200]
    WAKE["entity"] = entity
    return None


WAKE: dict = {}


def find_wake_switch(url: str, headers: dict) -> str | list[str]:
    """The Wake-on-LAN switch (or button) among Home Assistant's entities: the entity ID, or the candidates if
    there isn't exactly one."""
    r = requests.get(f"{url}/api/states", headers=headers, timeout=15)
    r.raise_for_status()
    hits = []
    for s in r.json():
        eid = s["entity_id"]
        if eid.split(".")[0] not in ("switch", "button"):
            continue
        text = f"{eid} {s.get('attributes', {}).get('friendly_name', '')}".lower()
        if any(k in text for k in ("wake", "wol", "_lan", " lan", "magic")):
            hits.append(eid)
    return hits[0] if len(hits) == 1 else hits


def worker_token() -> str:
    from .sources import load_env

    load_env()
    return os.environ.get("WORKER_TOKEN", "")


# --- the PC worker ----------------------------------------------------------------------------------------------

class Inhibitor:
    """Keeps the PC from sleeping while there's work (a systemd-logind inhibitor lock); released when idle, so the
    PC's own power settings take over again."""

    def __init__(self):
        self.proc, self.unavailable = None, False

    def hold(self) -> None:
        import subprocess

        if self.unavailable or (self.proc is not None and self.proc.poll() is None):
            return
        try:
            self.proc = subprocess.Popen(["systemd-inhibit", "--what=sleep:idle", "--who=music-hunter",
                                          "--why=Downloading and analysing tracks", "--mode=block",
                                          "sleep", "infinity"], stderr=subprocess.PIPE)
            time.sleep(0.2)
            if self.proc.poll() is not None:       # e.g. not allowed outside a desktop session (polkit)
                raise OSError(self.proc.stderr.read().decode(errors="replace").strip()[:200])
        except OSError as e:
            print(f"Can't keep the PC awake while working ({e}); carrying on", flush=True)
            self.proc, self.unavailable = None, True

    def release(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            self.proc.wait()
            self.proc = None


def run_worker(server: str, once: bool = False, http=None, local=None) -> int:
    """The PC side: ask the app for work, do it, send the results back. Never exits on its own unless `once` (then:
    when the queue has stayed empty for once_grace_seconds — jobs queue one track at a time). Returns the number of tracks done. `http` (a requests.Session-like object) and
    `local` are for tests."""
    token = worker_token()
    if not token:
        raise SystemExit("WORKER_TOKEN isn't set (in .env, the same on the PC and the Pi)")
    http = http or requests.Session()
    http.headers["Authorization"] = f"Bearer {token}"
    local = local or Local(path("cache") / "worker")
    base = server.rstrip("/") + "/api/worker"
    inhibitor, done, last_work, announced = Inhibitor(), 0, time.monotonic(), False
    blocked_streak, attempts, quality_next = 0, {}, 0.0
    while True:
        try:
            r = http.post(f"{base}/claim", timeout=60)
        except requests.RequestException as e:
            print(f"Can't reach {server}: {e}", flush=True)
            time.sleep(CFG["idle_poll_seconds"])
            continue
        if r.status_code == 204:
            if time.monotonic() >= quality_next:
                pause = _quality_one(http, base, local)       # nothing to analyse: playlist tracks' M4A files
                if pause == 0:
                    last_work = time.monotonic()
                    continue
                quality_next = time.monotonic() + pause
            idle = time.monotonic() - last_work
            if idle > CFG["idle_minutes"] * 60 and inhibitor.proc is not None:
                inhibitor.release()
                print("Queue empty: the PC may sleep again", flush=True)
            if once and idle > CFG["once_grace_seconds"]:
                return done
            if not announced:
                print(f"Connected to {server}; waiting for work", flush=True)
                announced = True
            time.sleep(CFG["poll_seconds"] if idle < CFG["idle_minutes"] * 60 else CFG["idle_poll_seconds"])
            continue
        r.raise_for_status()
        item = r.json()
        inhibitor.hold()
        wid = item["id"]
        try:
            res = local.analyse(item["video_id"], item.get("bpm_prior"))
        except DownloadFailed as e:
            stage = "analysis" if isinstance(e, AnalysisFailed) else "download"
            kind = None if stage == "analysis" else refusal(str(e))
            attempts[wid] = attempts.get(wid, 0) + 1
            if kind == "blocked" or (kind == "temporary" and attempts[wid] <= CFG["retry_attempts"]):
                pauses = CFG["blocked_pause_minutes"]
                pause = pauses[min(blocked_streak, len(pauses) - 1)] * 60 if kind == "blocked" else 30
                if _release(http, base, wid, pause):           # back in the queue; nothing fails
                    blocked_streak += kind == "blocked"
                    print(f"  {item['video_id']}: YouTube refused ({kind}); handed back, pausing {pause // 60 or 0.5} min",
                          flush=True)
                    last_work = time.monotonic()
                    time.sleep(pause)
                    continue
            print(f"  {item['video_id']}: {stage} failed: {str(e)[:120]}", flush=True)
            _post(http, f"{base}/{wid}/failed", json={"stage": stage, "error": str(e)[:500], "timings": e.timings})
            last_work = time.monotonic()
            continue
        blocked_streak = 0
        try:
            _send(http, base, wid, res)
            done += 1
            print(f"  [{done}] {item['video_id']} sent ({res.duration_s / 60:.1f} min)", flush=True)
        except requests.RequestException as e:
            print(f"  {item['video_id']}: couldn't send ({e}); the app will ask again", flush=True)
        finally:
            for f in (res.file, res.art):
                if f is not None:
                    Path(f).unlink(missing_ok=True)       # the app keeps the copies
        last_work = time.monotonic()


def _quality_one(http, base: str, local) -> float:
    """Fetch one playlist track's M4A and send it. Returns seconds to wait before asking again (0: ask straight away)."""
    from .pool import loudness

    if not CONFIG["quality"]["enabled"]:
        return 3600
    try:
        r = http.post(f"{base}/quality/claim", timeout=60)
    except requests.RequestException:
        return 60
    if r.status_code == 404:
        return 600                                       # an app that doesn't do this yet
    if r.status_code == 204:
        return 60
    r.raise_for_status()
    item = r.json()
    file = None
    try:
        file, quality, why = Quality.fetch(item["video_id"], local.folder or path("cache"))
        lufs, peak, gain = loudness(file)
        with open(file, "rb") as f:
            _post(http, f"{base}/quality/{item['track_id']}", data=f, params={
                "ext": file.suffix, "quality": quality, "lufs": lufs, "peak": peak, "gain": gain, "why": why or ""})
        print(f"  {item['video_id']} {'~256' if quality == 2 else '~130'} kbps M4A sent"
              + (f" ({why[:100]})" if why else ""), flush=True)
        return 0
    except Exception as e:
        print(f"  {item['video_id']}: no M4A: {str(e)[:160]}", flush=True)
        return CONFIG["worker"]["blocked_pause_minutes"][0] * 60 if refusal(str(e)) == "blocked" else 0
    finally:
        if file is not None:
            Path(file).unlink(missing_ok=True)


def _release(http, base: str, wid: int, pause_s: int) -> bool:
    """Hand an item back to the queue and tell the app the worker is pausing. False if the app can't (older app)."""
    try:
        r = http.post(f"{base}/{wid}/release", params={"pause_s": pause_s}, timeout=60)
    except requests.RequestException:
        return False
    return r.status_code == 200


def _post(http, url, **kw):
    r = http.post(url, timeout=300, **kw)
    if r.status_code == 409:
        return r                                         # the app no longer wants it (stopped meanwhile)
    r.raise_for_status()
    return r


def _send(http, base: str, wid: int, res: Result) -> None:
    import io

    buf = io.BytesIO()
    np.savez(buf, track=np.asarray(res.embedding.track, dtype=np.float32),
             chunks=np.asarray(res.embedding.chunks, dtype=np.float32), spans=np.asarray(res.embedding.spans))
    with open(res.file, "rb") as f:
        if _post(http, f"{base}/{wid}/file/audio", params={"ext": Path(res.file).suffix}, data=f).status_code == 409:
            return
    if res.art is not None:
        with open(res.art, "rb") as f:
            _post(http, f"{base}/{wid}/file/art", data=f)
    _post(http, f"{base}/{wid}/file/embedding", data=buf.getvalue())
    meta = {"duration_s": res.duration_s, "lufs": res.lufs, "peak": res.peak, "gain": res.gain, "bpm": res.bpm,
            "model": res.model, "published": res.published, "timings": res.timings, "notes": res.notes}
    _post(http, f"{base}/{wid}/done", json=meta)


# --- the app side of the worker API (called from web.py) -------------------------------------------------------

def claim(lib) -> dict | None:
    """The next queued item for the worker (and note that the worker is about). Items claimed too long ago without
    a result go back to the queue first."""
    db.set_setting(lib, "worker_seen", time.time(), log=False)
    with lib:
        lib.execute("UPDATE work SET state = 'queued' WHERE state = 'claimed'"
                    " AND claimed_at < datetime('now', ?)", (f"-{CFG['item_timeout_minutes']} minutes",))
        w = lib.execute("SELECT * FROM work WHERE state = 'queued' ORDER BY id LIMIT 1").fetchone()
        if w is None:
            return None
        lib.execute("UPDATE work SET state = 'claimed', claimed_at = datetime('now'), updated_at = datetime('now')"
                    " WHERE id = ?", (w["id"],))
    return {"id": w["id"], "video_id": w["video_id"], "bpm_prior": w["bpm_prior"]}


def quality_claim(lib) -> dict | None:
    """The next track in a playlist still without its good M4A (not tried lately)."""
    db.set_setting(lib, "worker_seen", time.time(), log=False)
    with lib:
        w = lib.execute(
            "SELECT t.id, t.video_id FROM tracks t WHERE t.video_id IS NOT NULL AND t.file_path IS NOT NULL"
            " AND t.id IN (SELECT track_id FROM playlist_tracks) AND coalesce(t.quality, 0) < 2"
            " AND (t.quality_tried_at IS NULL OR t.quality_tried_at < datetime('now', ?))"
            " ORDER BY t.id LIMIT 1", (f"-{CONFIG['quality']['retry_hours']} hours",)).fetchone()
        if w is None:
            return None
        lib.execute("UPDATE tracks SET quality_tried_at = datetime('now') WHERE id = ?", (w["id"],))
    return {"track_id": w["id"], "video_id": w["video_id"]}


def quality_receive(lib, track_id: int, body: bytes, ext: str, quality: int, lufs: float, peak: float, gain: float,
                    why: str = "") -> bool:
    """A playlist track's M4A has come back: it replaces the track's audio (the fingerprint stays: same recording)."""
    from .config import relative

    t = lib.execute("SELECT file_path, quality FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if t is None or not t["file_path"] or (t["quality"] or 0) >= quality:
        return False
    old = t["file_path"]
    ext = ext if ext.startswith(".") and ext[1:].isalnum() and len(ext) <= 6 else ".m4a"
    dest = resolve(old).with_suffix(ext)
    if dest == resolve(old):
        dest = dest.with_name(f"{dest.stem}.q{quality}{ext}")
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(body)
    tmp.replace(dest)
    with lib:
        lib.execute("UPDATE tracks SET file_path = ?, quality = ?, loudness_lufs = ?, true_peak_db = ?, gain_db = ?"
                    " WHERE id = ?", (relative(dest), quality, lufs, peak, gain, track_id))
    if relative(dest) != old:
        db.release_audio(lib, old)
    db.log_event(lib, "track_quality", track_id=track_id, quality=quality, bytes=len(body), why=why[:200] or None)
    return True


def quality_waiting(lib) -> bool:
    """Whether a playlist track is due for its M4A (the app wakes the PC for it)."""
    return lib.execute(
        "SELECT 1 FROM tracks t WHERE t.video_id IS NOT NULL AND t.file_path IS NOT NULL"
        " AND t.id IN (SELECT track_id FROM playlist_tracks) AND coalesce(t.quality, 0) < 2"
        " AND (t.quality_tried_at IS NULL OR t.quality_tried_at < datetime('now', ?)) LIMIT 1",
        (f"-{CONFIG['quality']['retry_hours']} hours",)).fetchone() is not None


def release(lib, wid: int, pause_s: float) -> bool:
    """The worker hands a claimed item back (YouTube refused it for now) and pauses; nothing is failed."""
    with lib:
        n = lib.execute("UPDATE work SET state = 'queued', claimed_at = NULL, updated_at = datetime('now')"
                        " WHERE id = ? AND state = 'claimed'", (wid,)).rowcount
    if n:
        db.set_setting(lib, "worker_paused_until", time.time() + max(0.0, pause_s), log=False)
        db.log_event(lib, "work_released", work_id=wid, pause_s=pause_s)
    return bool(n)


def receive_file(lib, wid: int, kind: str, body: bytes, ext: str = "") -> bool:
    """Store an uploaded file for a claimed item. False if the item isn't wanted any more."""
    w = lib.execute("SELECT * FROM work WHERE id = ? AND state = 'claimed'", (wid,)).fetchone()
    if w is None:
        return False
    from .config import relative

    if kind == "audio":
        ext = ext if ext.startswith(".") and ext[1:].isalnum() and len(ext) <= 6 else ".audio"
        dest, col = path("cache") / f"{w['video_id']}{ext}", "file_path"
    elif kind == "art":
        dest, col = path("cache") / "art" / f"{w['video_id']}.jpg", "art_path"
    else:
        dest, col = path("data") / "work" / f"{wid}.npz", "embedding_path"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    with lib:
        lib.execute(f"UPDATE work SET {col} = ?, updated_at = datetime('now') WHERE id = ?", (relative(dest), wid))
    return True


def finish(lib, wid: int, meta: dict | None = None, error: str | None = None) -> bool:
    """Mark a claimed item done (all files in) or failed. False if it isn't wanted any more."""
    w = lib.execute("SELECT * FROM work WHERE id = ? AND state = 'claimed'", (wid,)).fetchone()
    if w is None:
        return False
    if error is None and not (w["file_path"] and w["embedding_path"]):
        raise ValueError("audio and fingerprint must be sent before done")
    with lib:
        lib.execute("UPDATE work SET state = ?, result = ?, error = ?, updated_at = datetime('now') WHERE id = ?",
                    ("failed" if error is not None else "done", json.dumps(meta or {}), error, wid))
    return True


def _discard(lib, w) -> None:
    """Cancel an item and delete whatever was uploaded for it."""
    for col in ("file_path", "art_path", "embedding_path"):
        if w[col]:
            resolve(w[col]).unlink(missing_ok=True)
    with lib:
        lib.execute("UPDATE work SET state = 'cancelled', updated_at = datetime('now') WHERE id = ?", (w["id"],))


def drop_unfinished(lib) -> int:
    """At start-up: nobody is waiting for items queued before a restart (their jobs start again and queue anew)."""
    rows = lib.execute("SELECT * FROM work WHERE state IN ('queued', 'claimed', 'done', 'failed')").fetchall()
    for w in rows:
        _discard(lib, w)
    return len(rows)


def status(lib) -> dict:
    counts = dict(lib.execute("SELECT state, count(*) FROM work WHERE state IN ('queued', 'claimed')"
                              " GROUP BY state").fetchall())
    return {"mode": CFG["mode"], "connected": worker_connected(lib), "queued": counts.get("queued", 0),
            "working": counts.get("claimed", 0), "wake_entity": WAKE.get("entity"),
            "wake_problem": WAKE.get("problem")}
