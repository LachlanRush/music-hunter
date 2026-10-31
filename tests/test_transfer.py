"""v5 moving data to the Pi and snapshots back (SPEC.md §18.11)."""

import tarfile

import music_hunter.config as config
from music_hunter import db, library, transfer


def test_bundle_and_unpack(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    lib = db.connect()
    pid = library.create_playlist(lib, "pump")
    (tmp_path / "library").mkdir()
    (tmp_path / "library" / "A - B.webm").write_bytes(b"audio")
    (tmp_path / "cache" / "worker").mkdir(parents=True)
    (tmp_path / "cache" / "worker" / "tmp.webm").write_bytes(b"x")
    sess = db.connect(pid)
    sess.execute("SELECT 1")
    out = transfer.bundle(tmp_path / "b.tar", audio=True)
    names = tarfile.open(out).getnames()
    assert "data/music_hunter.db" in names and f"data/sessions/playlist-{pid}.db" in names
    assert "library/A - B.webm" in names and not any("worker" in n for n in names)
    snap = tarfile.open(transfer.bundle(tmp_path / "s.tar", audio=False)).getnames()
    assert not any(n.startswith("library") for n in snap)

    pi = tmp_path / "pi"                                   # the add-on's /data, already used once
    (pi / "data").mkdir(parents=True)
    (pi / "data" / "old.txt").write_text("old")
    out.rename(pi / "incoming.tar")
    assert transfer.unpack_incoming(pi) and not (pi / "incoming.tar").exists()
    assert (pi / "library" / "A - B.webm").read_bytes() == b"audio" and (pi / "previous" / "data" / "old.txt").exists()
    monkeypatch.setattr(config, "ROOT", pi)
    assert [p["name"] for p in library.list_playlists(db.connect())] == ["pump"]
    assert not transfer.unpack_incoming(pi)


def test_admin_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from music_hunter import web

    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setenv("WORKER_TOKEN", "secret")
    monkeypatch.setattr(web, "_restart_soon", lambda: None)
    library.create_playlist(db.connect(), "mine")
    client, auth = TestClient(web.app), {"Authorization": "Bearer secret"}
    assert client.get("/api/admin/snapshot").status_code == 401
    r = client.get("/api/admin/snapshot", headers=auth)
    assert r.status_code == 200 and r.content[:100].strip(b"\0")
    assert client.post("/api/admin/restore", headers=auth, content=b"tar").status_code == 409   # has playlists
    r = client.post("/api/admin/restore", params={"force": 1}, headers=auth, content=b"tar")
    assert r.status_code == 200 and (tmp_path / "incoming.tar").read_bytes() == b"tar"
