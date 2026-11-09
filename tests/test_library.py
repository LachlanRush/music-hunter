"""v5 playlists and library (SPEC.md §18.1), on an empty temporary project — no network."""

import json

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
    library.remove_from_playlist(conn, p2, t)                         # in Recently deleted: audio kept
    assert (root / path).exists() and [d["id"] for d in library.deleted_tracks(conn, p2)] == [t]
    library.delete_forever(conn, p2, t)
    library.delete_forever(conn, p1, t)
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
        assert d["back_restart_s"] == config.CONFIG["ui"]["back_restart_s"]
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
    assert tr["rate"] == round(145 / 140.4, 3) and tr["tempo_note"] == "Set to 145 BPM"

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
    off = client.put(f"/api/playlists/{pid}/tempo", json={"on": False}).json()      # off: the copy stays ready
    assert off["bpm"] == 138 and client.get(f"/api/playlists/{pid}").json()["tracks"][0]["rate"] == 1
    assert [p.name for p in folder.glob("*.webm")] == [f"pl{pid}-{t}-{rate2:.3f}.webm"]
    assert client.get(f"/tempo/{t}", params={"rate": 1.05}).status_code == 400      # which playlist?
    from music_hunter import tempo
    for _ in range(100):                                                            # nothing left rendering
        if not tempo.RENDERER.queue and tempo.RENDERER.current is None:
            break
        time.sleep(0.05)


def test_feedback(lib, monkeypatch):
    from fastapi.testclient import TestClient
    from music_hunter import web

    conn, _, _ = lib
    monkeypatch.setenv("WORKER_TOKEN", "secret")
    client = TestClient(web.app)
    assert client.post("/api/feedback", json={"text": "  "}).status_code == 400
    r = client.post("/api/feedback", json={"text": "Stars too small", "page": "/find/1",
                                           "context": {"playlist_id": 1, "play_id": 7}})
    assert r.status_code == 200
    row = conn.execute("SELECT page, context, text, status FROM feedback").fetchone()
    assert tuple(row) == ("/find/1", '{"playlist_id": 1, "play_id": 7}', "Stars too small", "new")
    assert client.get("/api/admin/feedback").status_code == 401
    got = client.get("/api/admin/feedback", headers={"Authorization": "Bearer secret"}).json()
    assert got[0]["context"]["play_id"] == 7 and "feedback.js" in client.get("/").text


def test_no_tempo_copies_until_first_engaged(lib):
    from music_hunter import tempo

    conn, track, _ = lib
    pid = library.create_playlist(conn, "p")
    s = tempo.playlist_settings(conn, pid)
    assert tempo.rate_for(130.0, s, ignore_off=True)[0] == 1                 # never turned on: no copy
    tempo.save(conn, tempo.playlist_key(pid), False, 140)
    assert tempo.rate_for(130.0, tempo.playlist_settings(conn, pid), ignore_off=True)[0] == 1
    tempo.save(conn, tempo.playlist_key(pid), True, 140)
    tempo.save(conn, tempo.playlist_key(pid), False, 140)                   # off again: copies kept ready
    assert tempo.rate_for(130.0, tempo.playlist_settings(conn, pid), ignore_off=True)[0] == round(140 / 130, 3)


def test_track_bpm_override(lib):
    """A track's own "Play at … BPM" in a playlist: used instead of Set to BPM while it's on, kept once set, its copy
    made alongside the Set to copy (at most those two), and replaced by a new value."""
    from fastapi.testclient import TestClient
    from music_hunter import web

    conn, track, root = lib
    t = track("Tone")
    with conn:
        conn.execute("UPDATE tracks SET bpm = 140 WHERE id = ?", (t,))
    pid = library.create_playlist(conn, "p")
    library.add_to_playlist(conn, pid, t)
    client = TestClient(web.app)
    url = f"/api/playlists/{pid}/tracks/{t}/bpm"
    assert client.put(url, json={"bpm": 200}).status_code == 400                    # too far from its own tempo
    assert client.put(url, json={"bpm": 145}).json()["bpm_override"] == 145
    client.put(f"/api/playlists/{pid}/tempo", json={"on": True, "bpm": 142})
    tr = client.get(f"/api/playlists/{pid}").json()["tracks"][0]
    assert tr["rate"] == round(145 / 140, 3) and tr["tempo_note"] == "Set to 145 BPM" and tr["bpm_override"] == 145
    client.put(f"/api/playlists/{pid}/tempo", json={"on": False})                   # off: plays at its own tempo
    assert client.get(f"/api/playlists/{pid}").json()["tracks"][0]["rate"] == 1
    client.put(url, json={"bpm": 150})                                              # a new value replaces it
    assert conn.execute("SELECT bpm_override FROM playlist_tracks").fetchone()[0] == 150


def test_recently_deleted(lib):
    """Removing a track moves it to Recently deleted with its stars; Restore puts it back starred; Delete forever and
    the 30-day limit remove it for good, and a Spotify refresh leaves removed tracks out."""
    conn, track, root = lib
    pid = library.create_playlist(conn, "p")
    a, b = track("A"), track("B")
    for t in (a, b):
        library.add_to_playlist(conn, pid, t)
    library.set_stars(conn, pid, a, 4)
    with conn:
        conn.execute("UPDATE tracks SET spotify_id = 'spA' WHERE id = ?", (a,))
    library.remove_from_playlist(conn, pid, a)
    assert [t["id"] for t in library.playlist_tracks(conn, pid)] == [b]
    assert [(d["id"], d["stars"]) for d in library.deleted_tracks(conn, pid)] == [(a, 4)]
    library.restore_track(conn, pid, a)
    assert {t["id"]: t["stars"] for t in library.playlist_tracks(conn, pid)} == {a: 4, b: None}
    library.remove_from_playlist(conn, pid, a)
    with conn:
        conn.execute("UPDATE deleted_tracks SET deleted_at = datetime('now', '-31 days')")
    assert library.purge_deleted(conn) == 1 and library.deleted_tracks(conn, pid) == []
    assert conn.execute("SELECT spotify_id FROM removed_spotify WHERE playlist_id = ?", (pid,)).fetchone()[0] == "spA"
    from music_hunter import spotify
    calls = []
    import pytest
    mp = pytest.MonkeyPatch()
    mp.setattr(spotify, "expand", lambda links: ["spA"])
    mp.setattr(spotify, "track", lambda sid: calls.append(sid))
    out = library.import_spotify(conn, "https://open.spotify.com/playlist/x", pid)
    mp.undo()
    assert out["skipped"] == 1 and calls == []                       # removed for good: not brought back


def test_youtube_links():
    assert library.youtube_video_id("https://music.youtube.com/watch?v=1ORA5_EFQHo&si=abc") == "1ORA5_EFQHo"
    assert library.youtube_video_id("https://youtu.be/1ORA5_EFQHo") == "1ORA5_EFQHo"
    assert library.youtube_video_id("https://open.spotify.com/track/2zGn6hwGCnANDoSsAxlnRx") is None


def test_add_song_and_restore_api(lib, monkeypatch):
    from fastapi.testclient import TestClient
    from music_hunter import web

    conn, track, _ = lib
    monkeypatch.setattr(web.job_runner, "kick", lambda: None)
    pid = library.create_playlist(conn, "p")
    t = track("A")
    library.add_to_playlist(conn, pid, t)
    client = TestClient(web.app)
    assert client.post(f"/api/playlists/{pid}/add", json={"link": "not a link"}).status_code == 400
    assert client.post(f"/api/playlists/{pid}/add", json={"link": "https://open.spotify.com/playlist/abc"}).status_code == 400
    job = client.post(f"/api/playlists/{pid}/add", json={"link": "https://music.youtube.com/watch?v=1ORA5_EFQHo"}).json()
    assert conn.execute("SELECT kind FROM jobs WHERE id = ?", (job["job_id"],)).fetchone()[0] == "add"
    assert client.delete(f"/api/playlists/{pid}/tracks/{t}").status_code == 200
    d = client.get(f"/api/playlists/{pid}").json()
    assert d["tracks"] == [] and d["deleted"][0]["id"] == t
    assert client.post(f"/api/playlists/{pid}/deleted/{t}/restore").status_code == 200
    assert client.get(f"/api/playlists/{pid}").json()["tracks"][0]["id"] == t


def test_star_changes_are_logged_for_replays(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    conn = db.connect(None)
    with conn:
        pid = conn.execute("INSERT INTO playlists (name) VALUES ('p')").lastrowid
        tid = conn.execute("INSERT INTO tracks (artist, title, origin) VALUES ('A', 'T', 'library')").lastrowid
    library.add_to_playlist(conn, pid, tid)
    library.set_stars(conn, pid, tid, 4)
    library.set_stars(conn, pid, tid, None)
    rows = [json.loads(r[0])["stars"] for r in conn.execute("SELECT data FROM events WHERE kind = 'stars_changed' ORDER BY id")]
    assert rows == [4, None]


def test_app_updates_are_published_and_offered(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from music_hunter import analysis, web
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(analysis, "worker_token", lambda: "secret")
    with TestClient(web.app) as client:
        assert client.get("/app/latest.json").status_code == 404
        auth = {"Authorization": "Bearer secret"}
        assert client.post("/api/admin/app?version_code=2&version_name=0.2.0", content=b"not an apk",
                           headers=auth).status_code == 400
        assert client.post("/api/admin/app?version_code=2&version_name=0.2.0", content=b"PK fake").status_code == 401
        r = client.post("/api/admin/app?version_code=2&version_name=0.2.0", content=b"PK fake", headers=auth)
        assert r.status_code == 200
        latest = client.get("/app/latest.json").json()
        assert latest["version_code"] == 2 and latest["url"] == "/app/music-hunter.apk" and latest["size"] == 7
        assert client.get("/app/music-hunter.apk").content == b"PK fake"


def _sync_setup(tmp_path, monkeypatch):
    import subprocess

    from music_hunter import sync
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setitem(sync.CFG, "folder", str(tmp_path / "share"))
    (tmp_path / "library").mkdir()
    def m4a(name):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        "-c:a", "aac", str(tmp_path / "library" / name)], check=True)
    conn = db.connect(None)
    with conn:
        pid = conn.execute("INSERT INTO playlists (name) VALUES ('pump: 140')").lastrowid
    def track(artist, title, status="member", stars=None, file=None):
        with conn:
            tid = conn.execute("INSERT INTO tracks (artist, title, origin, file_path, duration_s, bpm) VALUES"
                               " (?, ?, 'library', ?, 1.0, 140.0)", (artist, title, file)).lastrowid
            conn.execute("INSERT INTO playlist_tracks (playlist_id, track_id, status, stars) VALUES (?, ?, ?, ?)",
                         (pid, tid, status, stars))
        return tid
    return sync, conn, pid, track, m4a


def test_sync_keeps_a_tagged_m4a_folder_and_a_rekordbox_file(tmp_path, monkeypatch):
    import json as js
    import subprocess

    sync, conn, pid, track, m4a = _sync_setup(tmp_path, monkeypatch)
    m4a("a.m4a"); m4a("b.m4a"); m4a("rate.m4a")
    a = track("Art & Co", "One", stars=5, file="library/a.m4a")
    b = track("Art", "Two", file="library/b.m4a")
    track("Art", "Opus yet", file="library/o.webm")               # no M4A yet: waits
    track("Art", "To rate", status="added", file="library/rate.m4a")   # not reviewed: not synced
    sync.run_once(conn)                                           # off: nothing happens
    assert not (tmp_path / "share").exists()
    db.set_setting(conn, sync.setting(pid), True)
    sync.run_once(conn)
    folder = tmp_path / "share" / "pump_ 140"
    assert sorted(f.name for f in folder.iterdir()) == ["Art & Co - One.m4a", "Art - Two.m4a"]
    tags = js.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json",
                                    str(folder / "Art & Co - One.m4a")], capture_output=True, text=True).stdout)
    assert tags["format"]["tags"]["title"] == "One" and tags["format"]["tags"]["album"] == "pump: 140"
    assert sync.state(conn, pid) == {"on": True, "tracks": 3, "ready": 2}
    xml = (tmp_path / "share" / "rekordbox.xml").read_text()
    assert 'Rating="255"' in xml and 'Name="pump: 140"' in xml and "Art &amp; Co" in xml
    assert 'file://localhost/C:/Users/lachl/Music/Songs/Playlists/pump_%20140/Art%20%26%20Co%20-%20One.m4a' in xml
    import xml.dom.minidom as md
    md.parseString(xml)                                           # well formed
    # A track leaving the playlist leaves the folder; files that aren't ours stay.
    (folder / "mine.txt").write_text("keep")
    library.remove_from_playlist(conn, pid, b)
    sync.run_once(conn)
    assert sorted(f.name for f in folder.iterdir()) == ["Art & Co - One.m4a", "mine.txt"]
    # Renaming moves the folder; switching off leaves the files.
    library.rename_playlist(conn, pid, "pumped")
    sync.run_once(conn)
    assert (tmp_path / "share" / "pumped" / "Art & Co - One.m4a").exists() and not folder.exists()
    db.set_setting(conn, sync.setting(pid), False)
    library.remove_from_playlist(conn, pid, a)
    sync.run_once(conn)
    assert (tmp_path / "share" / "pumped" / "Art & Co - One.m4a").exists()


def test_sync_switch_through_the_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from music_hunter import web

    sync, conn, pid, track, m4a = _sync_setup(tmp_path, monkeypatch)
    m4a("a.m4a")
    track("A", "T", file="library/a.m4a")
    with TestClient(web.app) as client:
        assert client.get(f"/api/playlists/{pid}").json()["sync"] == {"on": False, "tracks": 1, "ready": 0}
        assert client.put(f"/api/playlists/{pid}/sync", json={"on": True}).json()["on"] is True
        sync.wait()
        assert client.get(f"/api/playlists/{pid}").json()["sync"] == {"on": True, "tracks": 1, "ready": 1}
