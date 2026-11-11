"""v5 PC worker (SPEC.md §18.4): the app queues a track, the worker downloads/analyses it and sends it back."""

import threading
import time

import numpy as np
import pytest

import music_hunter.config as config
from music_hunter import analysis, db
from music_hunter.config import CONFIG


class Http:
    """requests-style calls on the app's TestClient (no network)."""

    def __init__(self, client):
        self.client, self.headers = client, client.headers

    def post(self, url, timeout=None, data=None, **kw):
        if data is not None:
            kw["content"] = data.read() if hasattr(data, "read") else data
        return self.client.post(url, **kw)


class FakePC:
    """Stands in for analysis.Local on the PC: writes a small 'download' outside the app's folders."""

    def __init__(self, folder, fail=None):
        self.folder, self.fail, self.calls = folder, fail, []
        folder.mkdir(exist_ok=True)

    def analyse(self, video_id, bpm_prior=None, status=None, lane=None):
        self.calls.append((video_id, bpm_prior))
        self.lanes = getattr(self, "lanes", []) + [(lane or analysis.HOME)["name"]]
        if self.fail:
            raise self.fail
        audio, art = self.folder / f"{video_id}.webm", self.folder / f"{video_id}.jpg"
        audio.write_bytes(b"audio " * 100)
        art.write_bytes(b"jpeg")
        chunks = np.eye(3, 8, dtype=np.float32)
        emb = analysis.Embedding(chunks.mean(0) / np.linalg.norm(chunks.mean(0)), chunks,
                                 [(0.0, 30.0), (30.0, 60.0), (60.0, 75.0)])
        return analysis.Result(audio, 75.0, -9.5, -0.8, -4.5, emb, "test-model", 141.0, art, "2026-01-02",
                               {"download_s": 1.0, "analysis_s": 0.5}, {"art_failed": "just a note"})


@pytest.fixture
def remote(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from music_hunter import web

    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setitem(CONFIG["worker"], "mode", "remote")
    monkeypatch.setitem(CONFIG["worker"], "wait_poll_seconds", 0.02)
    monkeypatch.setitem(CONFIG["worker"], "once_grace_seconds", -1)
    monkeypatch.setitem(CONFIG["worker"], "lanes", ["home"])
    monkeypatch.setattr(analysis.Quality, "announced", True)            # no sign-in check against YouTube
    monkeypatch.setenv("WORKER_TOKEN", "secret")
    wakes = []
    monkeypatch.setattr(analysis, "wake", lambda: wakes.append(time.time()))
    db.connect().close()
    return Http(TestClient(web.app)), wakes, tmp_path


def queue(video_id, bpm_prior=None):
    """Ask the backend for a track in a thread (it blocks until the worker answers); wait until it's queued."""
    box = {}

    def run():
        try:
            box["result"] = analysis.backend().analyse(video_id, bpm_prior)
        except Exception as e:
            box["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    lib = db.connect_library()
    for _ in range(200):
        if lib.execute("SELECT count(*) FROM work WHERE video_id = ?", (video_id,)).fetchone()[0]:
            break
        time.sleep(0.01)
    return t, box


def test_round_trip(remote):
    http, wakes, root = remote
    assert isinstance(analysis.backend(), analysis.Remote)
    t, box = queue("vidA", 140.0)
    time.sleep(0.1)
    assert wakes, "no worker connected: the PC is woken"
    pc = FakePC(root / "pc")
    assert analysis.run_worker("http://testserver", once=True, http=http, local=pc) == 1
    t.join(5)
    res = box["result"]
    assert pc.calls == [("vidA", 140.0)]
    assert res.file == root / "cache" / "vidA.webm" and res.file.read_bytes() == b"audio " * 100
    assert res.art.read_bytes() == b"jpeg" and res.bpm == 141.0 and res.published == "2026-01-02"
    assert res.embedding.chunks.shape == (3, 8) and res.embedding.spans[2] == (60.0, 75.0)
    assert res.notes == {"art_failed": "just a note"} and res.timings["download_s"] == 1.0
    assert not (root / "pc" / "vidA.webm").exists()                 # the PC's copy is removed once sent
    lib = db.connect_library()
    assert lib.execute("SELECT state FROM work").fetchone()[0] == "collected"
    assert list((root / "data" / "work").iterdir()) == []            # fingerprint file read and removed
    assert analysis.worker_connected(lib) and len(wakes) == 1


def test_failure_comes_back(remote):
    http, _, root = remote
    t, box = queue("vidB")
    pc = FakePC(root / "pc", fail=analysis.DownloadFailed("Video unavailable", download_s=2.0))
    analysis.run_worker("http://testserver", once=True, http=http, local=pc)
    t.join(5)
    e = box["error"]
    assert type(e) is analysis.DownloadFailed and "unavailable" in str(e) and e.timings == {"download_s": 2.0}


def test_token_and_stale_items(remote):
    http, _, root = remote
    assert http.client.post("/api/worker/claim").status_code == 401
    assert http.client.post("/api/worker/claim", headers={"Authorization": "Bearer nope"}).status_code == 401
    auth = {"Authorization": "Bearer secret"}
    assert http.client.post("/api/worker/claim", headers=auth).status_code == 204
    lib = db.connect_library()
    with lib:
        wid = lib.execute("INSERT INTO work (video_id, state) VALUES ('old', 'claimed')").lastrowid
    assert http.client.post(f"/api/worker/{wid}/done", headers=auth, json={
        "duration_s": 1, "lufs": 1, "peak": 1, "gain": 1, "model": "m"}).status_code == 400   # files first
    analysis.drop_unfinished(lib)                                     # a restart: nobody waits for it now
    r = http.client.post(f"/api/worker/{wid}/file/audio", headers=auth, content=b"x")
    assert r.status_code == 409 and not (root / "cache" / "old").exists()
    assert http.client.get("/api/worker").json()["mode"] == "remote"


def test_wake_finds_the_switch(monkeypatch):
    calls = []

    class R:
        def __init__(self, body=None):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body
    states = [{"entity_id": "switch.kitchen_light", "attributes": {"friendly_name": "Kitchen"}},
              {"entity_id": "switch.lachlandesktop", "attributes": {"friendly_name": "Desktop Wake on LAN"}},
              {"entity_id": "light.wake_up_light", "attributes": {}}]
    monkeypatch.setattr(analysis.requests, "get", lambda url, **kw: R(states))
    monkeypatch.setattr(analysis.requests, "post", lambda url, **kw: calls.append((url, kw["json"])) or R())
    monkeypatch.setenv("HA_TOKEN", "t")
    monkeypatch.setitem(CONFIG["worker"]["wake"], "url", "http://ha:8123")
    monkeypatch.setitem(CONFIG["worker"]["wake"], "entity_id", "")
    assert analysis.wake() is None
    assert calls == [("http://ha:8123/api/services/switch/turn_on", {"entity_id": "switch.lachlandesktop"})]
    states.append({"entity_id": "switch.wol_laptop", "attributes": {}})             # two candidates: ask
    assert "candidates: switch.lachlandesktop, switch.wol_laptop" in analysis.wake()
    monkeypatch.setitem(CONFIG["worker"]["wake"], "entity_id", "switch.wol_laptop")  # set explicitly
    assert analysis.wake() is None and calls[-1][1] == {"entity_id": "switch.wol_laptop"}


def test_youtube_refusal_hands_back_and_pauses(remote, monkeypatch):
    """YouTube refusing downloads ("not a bot"): the worker hands the track back and pauses instead of failing it;
    the app doesn't wake the PC meanwhile, and the track is done once downloads work again."""
    http, wakes, root = remote
    monkeypatch.setitem(analysis.CFG, "blocked_pause_minutes", [0])
    pc = FakePC(root / "pc")
    real, n = pc.analyse, {"calls": 0}

    def flaky(video_id, bpm_prior=None, status=None):
        n["calls"] += 1
        if n["calls"] <= 2:
            raise analysis.DownloadFailed("ERROR: [youtube] x: Sign in to confirm you\u2019re not a bot.")
        return real(video_id, bpm_prior, status)
    pc.analyse = flaky
    t, box = queue("vidC")
    assert analysis.run_worker("http://testserver", once=True, http=http, local=pc) == 1
    t.join(5)
    assert "result" in box and n["calls"] == 3                       # refused twice, then done; never failed
    lib = db.connect_library()
    assert lib.execute("SELECT count(*) FROM events WHERE kind = 'work_released'").fetchone()[0] == 2
    db.set_setting(lib, "worker_paused_until", time.time() + 60, log=False)
    assert analysis.worker_connected(lib)                            # pausing on purpose: no need to wake
    assert analysis.refusal("HTTP Error 403: Forbidden") == "temporary" and analysis.refusal("Video unavailable") is None


def test_app_downloads_pause_when_youtube_refuses(remote, monkeypatch):
    """The Pi's own downloads (audio brought back for a track that rose above the gate) share the home address with
    the PC: a "not a bot" refusal pauses them, and the PC's pause stops them too."""
    from music_hunter import pool

    http, _, root = remote
    lib = db.connect_library()
    with lib:
        tid = lib.execute("INSERT INTO tracks (artist, title, duration_s, origin, video_id) VALUES ('a', 't', 200, 'pool',"
                          " 'vidZ')").lastrowid
    calls = []

    def refuse(video_id, folder=None):
        calls.append(video_id)
        raise RuntimeError("ERROR: [youtube] vidZ: Sign in to confirm you’re not a bot.")
    monkeypatch.setattr(pool, "download", refuse)
    assert not pool.restore_audio(lib, tid) and analysis.youtube_paused()
    assert not pool.restore_audio(lib, tid) and calls == ["vidZ"]              # paused: not tried again


def _library_track(lib, tmp_path, vid="abc"):
    (tmp_path / "library").mkdir(exist_ok=True)
    (tmp_path / "library" / "A - T.webm").write_bytes(b"opus")
    with lib:
        pid = lib.execute("INSERT INTO playlists (name) VALUES ('p')").lastrowid
        tid = lib.execute("INSERT INTO tracks (artist, title, origin, video_id, file_path, gain_db) VALUES"
                          " ('A', 'T', 'library', ?, 'library/A - T.webm', 1.0)", (vid,)).lastrowid
        lib.execute("INSERT INTO playlist_tracks (playlist_id, track_id) VALUES (?, ?)", (pid, tid))
    return tid


def test_playlist_tracks_get_their_m4a_only_once_and_pool_tracks_never(remote, tmp_path):
    from music_hunter import analysis, db

    lib = db.connect_library()
    tid = _library_track(lib, tmp_path)
    with lib:
        lib.execute("INSERT INTO tracks (artist, title, origin, video_id, file_path) VALUES"
                    " ('A', 'Pool', 'pool', 'pooltrack', 'cache/pooltrack.webm')")
    assert analysis.quality_waiting(lib)
    item = analysis.quality_claim(lib)
    assert item == {"track_id": tid, "video_id": "abc"}
    assert analysis.quality_claim(lib) is None                        # tried just now; the pool track isn't offered
    assert not analysis.quality_waiting(lib)
    assert analysis.quality_receive(lib, tid, b"free", ".m4a", 1, -9.0, -1.0, 2.0)
    assert (tmp_path / "library" / "A - T.m4a").read_bytes() == b"free"
    assert not (tmp_path / "library" / "A - T.webm").exists()         # the old audio goes
    t = lib.execute("SELECT file_path, quality, gain_db FROM tracks WHERE id = ?", (tid,)).fetchone()
    assert (t["file_path"], t["quality"], t["gain_db"]) == ("library/A - T.m4a", 1, 2.0)
    with lib:                                                         # a day later the good one is tried again
        lib.execute("UPDATE tracks SET quality_tried_at = datetime('now', '-25 hours') WHERE id = ?", (tid,))
    assert analysis.quality_claim(lib)["track_id"] == tid
    assert analysis.quality_receive(lib, tid, b"good", ".m4a", 2, -9.0, -1.0, 2.0)
    assert lib.execute("SELECT quality FROM tracks WHERE id = ?", (tid,)).fetchone()[0] == 2
    assert not analysis.quality_receive(lib, tid, b"free", ".m4a", 1, -9.0, -1.0, 2.0)   # never downgraded
    with lib:
        lib.execute("UPDATE tracks SET quality_tried_at = datetime('now', '-25 hours') WHERE id = ?", (tid,))
    assert analysis.quality_claim(lib) is None                        # has the good one: done


def test_worker_fetches_a_playlist_track_when_idle(remote, tmp_path, monkeypatch):
    from music_hunter import analysis, db, pool

    lib = db.connect_library()
    tid = _library_track(lib, tmp_path)
    f = tmp_path / "abc.m4a"
    f.write_bytes(b"good")
    monkeypatch.setattr(analysis.Quality, "fetch", lambda vid, folder: (f, 2, None))
    monkeypatch.setattr(pool, "loudness", lambda file: (-9.0, -1.0, 2.0))
    http = remote[0]
    http.headers["Authorization"] = "Bearer secret"
    assert analysis._quality_one(http, "/api/worker", analysis.Local(tmp_path)) == 0
    assert lib.execute("SELECT quality FROM tracks WHERE id = ?", (tid,)).fetchone()[0] == 2
    assert analysis._quality_one(http, "/api/worker", analysis.Local(tmp_path)) == 60           # nothing left
    assert not f.exists()                                             # the worker keeps no copy


def test_two_addresses_share_the_queue(remote, monkeypatch):
    """With PIA's proxy login in .env the worker runs a second loop through it; both take tracks from the queue."""
    http, _, root = remote
    monkeypatch.setitem(CONFIG["worker"], "lanes", ["home", "pia"])
    monkeypatch.setenv("PIA_SOCKS_USER", "x1")
    monkeypatch.setenv("PIA_SOCKS_PASS", "pw")
    assert [x["name"] for x in analysis.lanes()] == ["home", "pia"]
    assert analysis.lanes()[1]["proxy"] == "socks5://x1:pw@proxy-nl.privateinternetaccess.com:1080"
    pc = FakePC(root / "pc")
    real = pc.analyse

    def slow(*a, **kw):                                               # long enough for the other loop to claim one
        time.sleep(0.3)
        return real(*a, **kw)
    pc.analyse = slow
    jobs = [queue(v) for v in ("vidL1", "vidL2")]
    assert analysis.run_worker("http://testserver", once=True, http=http, local=pc) == 2
    for t, box in jobs:
        t.join(5)
        assert "result" in box
    assert sorted(pc.lanes) == ["home", "pia"]
    monkeypatch.delenv("PIA_SOCKS_USER")
    assert [x["name"] for x in analysis.lanes()] == ["home"]           # no login: home only
