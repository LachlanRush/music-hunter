"""v5 playlists and library (SPEC.md §18.1), on an empty temporary project — no network."""

import pytest

import music_hunter.config as config
from music_hunter import db, library


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    (tmp_path / "library").mkdir()
    conn = db.connect()

    def track(title, artist="Someone"):
        rel = f"library/{artist} - {title}.webm"
        (tmp_path / rel).write_bytes(b"audio")
        with conn:
            return conn.execute("INSERT INTO tracks (artist, title, file_path, duration_s, origin) VALUES (?, ?, ?, 200,"
                                " 'library')", (artist, title, rel)).lastrowid
    return conn, track, tmp_path


def test_order_by_stars_with_added_on_top(lib):
    conn, track, _ = lib
    pid = library.create_playlist(conn, "pump")
    a, b, c, d, e = (track(t) for t in "ABCDE")
    for t in (a, b, c, d):
        library.add_to_playlist(conn, pid, t)
    library.add_to_playlist(conn, pid, e, status="added")          # Added in Find music, waiting for stars
    library.set_stars(conn, pid, b, 5)
    library.set_stars(conn, pid, d, 2)
    order = [t["id"] for t in library.playlist_tracks(conn, pid)]
    assert order == [e, b, d, c, a]                                  # Added first, then 5 → 2, unstarred last
    import time
    time.sleep(0.01)
    library.set_stars(conn, pid, a, 2)                               # same stars as d, starred later: above it
    assert [t["id"] for t in library.playlist_tracks(conn, pid)][:4] == [e, b, a, d]
    library.set_stars(conn, pid, a, None)
    library.set_stars(conn, pid, e, 4)                              # starring an Added track moves it into the list
    rows = {t["id"]: t for t in library.playlist_tracks(conn, pid)}
    assert rows[e]["status"] == "member" and [t["id"] for t in library.playlist_tracks(conn, pid)][:2] == [b, e]
    library.set_stars(conn, pid, b, None)                           # clearing stars: no longer a seed
    assert rows[b]["stars"] == 5 and library.playlist_tracks(conn, pid)[-1]["stars"] is None
    with pytest.raises(ValueError):
        library.set_stars(conn, pid, a, 6)


def test_remove_drops_audio_only_when_in_no_playlist(lib):
    conn, track, root = lib
    p1, p2 = library.create_playlist(conn, "one"), library.create_playlist(conn, "two")
    t = track("Shared")
    library.add_to_playlist(conn, p1, t)
    assert library.add_to_playlist(conn, p2, t) and not library.add_to_playlist(conn, p2, t)   # no duplicates
    library.remove_from_playlist(conn, p1, t)
    path = conn.execute("SELECT file_path FROM tracks WHERE id = ?", (t,)).fetchone()[0]
    assert path and (root / path).exists()                           # still in "two"
    library.remove_from_playlist(conn, p2, t)
    assert conn.execute("SELECT file_path FROM tracks WHERE id = ?", (t,)).fetchone()[0] is None
    assert not (root / path).exists()
    assert conn.execute("SELECT count(*) FROM tracks WHERE id = ?", (t,)).fetchone()[0] == 1   # kept for learning


def test_list_and_delete(lib):
    conn, track, _ = lib
    pid = library.create_playlist(conn, "  ")
    t1, t2 = track("X"), track("Y")
    library.add_to_playlist(conn, pid, t1)
    library.add_to_playlist(conn, pid, t2, status="added")
    library.set_stars(conn, pid, t1, 3)
    [p] = library.list_playlists(conn)
    assert p["name"] == "Untitled playlist" and (p["tracks"], p["seeds"], p["waiting"]) == (2, 1, 1)
    library.rename_playlist(conn, pid, "Renamed")
    assert library.list_playlists(conn)[0]["name"] == "Renamed"
    library.delete_playlist(conn, pid)
    assert library.list_playlists(conn) == []
    assert conn.execute("SELECT count(file_path) FROM tracks").fetchone()[0] == 0    # orphaned audio removed


def test_playlist_api(lib, monkeypatch):
    from fastapi.testclient import TestClient
    from music_hunter import web

    conn, track, _ = lib
    monkeypatch.setattr(web.worker, "start_if_needed", lambda: None)
    t = track("Song")
    with TestClient(web.app) as client:
        pid = client.post("/api/playlists", json={"name": "Mine"}).json()["playlist_id"]
        other = client.post("/api/playlists", json={"name": "Other"}).json()["playlist_id"]
        assert client.post(f"/api/playlists/{pid}/tracks", json={"track_id": t}).json()["added"]
        assert client.put(f"/api/playlists/{pid}/tracks/{t}", json={"stars": 4}).status_code == 200
        assert client.put(f"/api/playlists/{pid}/tracks/{t}", json={"stars": 9}).status_code == 400
        d = client.get(f"/api/playlists/{pid}").json()
        assert d["tracks"][0]["stars"] == 4 and d["tracks"][0]["audio"] == f"/audio/{t}"
        assert d["others"] == [{"id": other, "name": "Other"}]
        assert client.post(f"/api/playlists/{other}/tracks", json={"track_id": t}).json()["added"]
        assert client.get("/").status_code == 200 and client.get(f"/playlist/{pid}").status_code == 200
        assert client.delete(f"/api/playlists/{pid}/tracks/{t}").status_code == 200
        assert client.get(f"/api/playlists/{pid}").json()["tracks"] == []
        assert client.get(f"/api/playlists/{other}").json()["tracks"][0]["id"] == t
        assert client.post("/api/playlists", json={"spotify_url": "not a link"}).status_code == 400


def test_same_bpm_render(lib):
    """Same BPM: a playlist's copies are rendered on the server at its target; one target at a time."""
    import subprocess
    import time

    from fastapi.testclient import TestClient
    from music_hunter import web

    conn, track, root = lib
    t = track("Tone")
    f = root / "library" / "Someone - Tone.webm"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                    "-c:a", "libopus", str(f)], check=True)
    with conn:
        conn.execute("UPDATE tracks SET bpm = 93.6 WHERE id = ?", (t,))          # a 2:3 reading of ~140
    pid = library.create_playlist(conn, "p")
    library.add_to_playlist(conn, pid, t)
    client = TestClient(web.app)
    d = client.get(f"/api/playlists/{pid}").json()
    assert d["tempo"]["on"] is False and d["tracks"][0]["rate"] == 1 and d["tracks"][0]["bpm_shown"] == 94
    assert client.put(f"/api/playlists/{pid}/tempo", json={"on": True, "bpm": 145}).json()["target"] == 145
    tr = client.get(f"/api/playlists/{pid}").json()["tracks"][0]
    assert tr["rate"] == round(145 / 140.4, 3) and tr["tempo_note"].startswith("145 BPM")

    def wait(rate):
        for _ in range(100):
            st = client.get(f"/tempo/{t}", params={"rate": rate, "pl": pid}).json()
            if st["state"] != "rendering":
                return st
            time.sleep(0.1)
    st = wait(tr["rate"])
    assert st["state"] == "ready" and len(client.get(st["url"]).content) > 1000
    folder = root / "cache" / "tempo"
    assert len(list(folder.glob("*.webm"))) == 1
    client.put(f"/api/playlists/{pid}/tempo", json={"on": True, "bpm": 138})       # new target: old copy gone
    rate2 = client.get(f"/api/playlists/{pid}").json()["tracks"][0]["rate"]
    assert wait(rate2)["state"] == "ready"
    assert [p.name for p in folder.glob("*.webm")] == [f"pl{pid}-{t}-{rate2:.3f}.webm"]
    client.put(f"/api/playlists/{pid}/tempo", json={"on": False})                  # off: none kept
    assert not list(folder.glob("*.webm"))
    assert client.get(f"/tempo/{t}", params={"rate": 1.05}).status_code == 400      # which playlist?
