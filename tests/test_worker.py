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

    def analyse(self, video_id, bpm_prior=None, status=None):
        self.calls.append((video_id, bpm_prior))
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
