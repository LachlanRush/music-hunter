"""v5 Find music per playlist (SPEC.md §18.2–18.3): sessions, seeds from stars, Adds into the playlist."""

import pytest

from music_hunter import db, find, library, pool
from music_hunter.config import CONFIG
from music_hunter.pull import is_v5_session, seeds


@pytest.fixture
def setup(conn):
    """A playlist in the library made of tracks from the copied run (they have fingerprints)."""
    lib = conn
    tracks = [r[0] for r in lib.execute("SELECT t.id FROM tracks t JOIN track_embeddings e ON e.track_id = t.id"
                                        " WHERE t.origin = 'pool' ORDER BY t.id LIMIT 6")]
    pid = library.create_playlist(lib, "test")
    for t in tracks:
        library.add_to_playlist(lib, pid, t)
    return lib, pid, tracks


def test_seeds_follow_stars(setup):
    lib, pid, tracks = setup
    library.set_stars(lib, pid, tracks[0], 5)
    library.set_stars(lib, pid, tracks[1], 1)
    sess = db.connect(pid)
    assert find.started(pid) and not is_v5_session(sess)
    counts = find.sync_seeds(lib, sess, pid)
    assert counts == {"seeds": 2, "added": 2, "unstarred": 0} and is_v5_session(sess)
    s = {x.stars for x in seeds(sess)}
    assert s == {5.0, 1.0}
    assert sess.execute("SELECT count(*) FROM track_embeddings").fetchone()[0] == 2        # fingerprints copied
    library.set_stars(lib, pid, tracks[1], None)                                          # unstarred: not a seed
    library.set_stars(lib, pid, tracks[2], 3)
    counts = find.sync_seeds(lib, sess, pid)
    assert counts["added"] == 1 and counts["unstarred"] == 1
    assert sorted(x.stars for x in seeds(sess)) == [3.0, 5.0]
    from music_hunter.web import Worker
    assert Worker.job_needed(sess) == "pull"                                              # first pull due


def test_shares_follow_stars(setup):
    lib, pid, tracks = setup
    library.set_stars(lib, pid, tracks[0], 5)
    library.set_stars(lib, pid, tracks[1], 1)
    sess = db.connect(pid)
    find.sync_seeds(lib, sess, pid)
    ids = [s.track_id for s in seeds(sess)]
    target = pool.seed_targets(sess, ids)
    big, small = sorted(target.values(), reverse=True)
    assert sum(target.values()) == CONFIG["pool"]["target_size"] and big > 3 * small      # 5 stars vs 1 (floored)


def test_adds_land_in_the_playlist(setup):
    lib, pid, tracks = setup
    library.set_stars(lib, pid, tracks[0], 4)
    sess = db.connect(pid)
    find.sync_seeds(lib, sess, pid)
    with sess:                                                    # a pool track in the session, rated Add
        tid = sess.execute("INSERT INTO tracks (artist, title, file_path, duration_s, origin, video_id)"
                           " VALUES ('New', 'Find', 'library/New - Find.webm', 200, 'pool', 'vidNEW')").lastrowid
        sess.execute("INSERT INTO track_embeddings (track_id, model, dim, n_chunks, embedding)"
                     " SELECT ?, model, dim, n_chunks, embedding FROM track_embeddings LIMIT 1", (tid,))
        play = sess.execute("INSERT INTO plays (track_id, generation, play_number, is_control, rating, components, score, rank, pool_size, provenance, shown_at)"
                            " VALUES (?, 1, 1, 0, 3, '{}', 0, 1, 1, '[]', datetime('now'))", (tid,)).lastrowid
    find.sync_add(lib, sess, pid, play)
    added = [t for t in library.playlist_tracks(lib, pid) if t["status"] == "added"]
    assert [t["title"] for t in added] == ["Find"] and added[0]["stars"] is None
    lid = added[0]["id"]
    assert lib.execute("SELECT count(*) FROM track_embeddings WHERE track_id = ?", (lid,)).fetchone()[0] == 1
    find.sync_add(lib, sess, pid, play)                           # idempotent
    assert sum(t["status"] == "added" for t in library.playlist_tracks(lib, pid)) == 1
    with sess:
        sess.execute("UPDATE plays SET rating = 2 WHERE id = ?", (play,))
    find.sync_add(lib, sess, pid, play)                           # changed away from Add: leaves the playlist
    assert not any(t["status"] == "added" for t in library.playlist_tracks(lib, pid))
    # An Add is pulled for once (as a 3-star seed by default), then drops out unless starred
    with sess:
        sess.execute("UPDATE plays SET rating = 3 WHERE id = ?", (play,))
    assert {s.track_id: s.stars for s in seeds(sess)}[tid] == CONFIG["find"]["add_seed_stars"]
    from music_hunter.pull import mark_pulled
    mark_pulled(sess, [tid])
    assert tid not in {s.track_id for s in seeds(sess)}


def test_find_api(setup, monkeypatch):
    from fastapi.testclient import TestClient
    from music_hunter import web

    lib, pid, tracks = setup
    monkeypatch.setattr(web.Worker, "start_if_needed", lambda self: None)
    with TestClient(web.app) as client:
        assert client.get(f"/api/playlists/{pid}/find").json()["state"] == "none"
        assert client.post(f"/api/playlists/{pid}/find").status_code == 400            # nothing starred yet
        assert client.get(f"/find/{pid}/api/state").status_code == 404                  # no session yet
        client.put(f"/api/playlists/{pid}/tracks/{tracks[0]}", json={"stars": 5})
        st = client.post(f"/api/playlists/{pid}/find").json()
        assert st["state"] == "preparing" and st["seeds"] == 1
        state = client.get(f"/find/{pid}/api/state").json()                              # the session's own API
        assert state["seeds"] == 1 and state["unplayed"] == 0
        assert client.get(f"/find/{pid}").status_code == 200
        assert client.get("/find/9999/api/state").status_code == 404
