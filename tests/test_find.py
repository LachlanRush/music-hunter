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


def test_removed_seed_takes_its_tracks(setup):
    """A track removed from the playlist takes the pool tracks it found with it, unless another seed found them too;
    unstarring doesn't; adding it back brings them back."""
    import music_hunter.config as config

    lib, pid, tracks = setup
    a, b = tracks[0], tracks[1]
    library.set_stars(lib, pid, a, 4)
    library.set_stars(lib, pid, b, 3)
    sess = db.connect(pid)
    find.sync_seeds(lib, sess, pid)
    seed = {r["library_id"]: r["id"] for r in sess.execute("SELECT id, library_id FROM tracks WHERE library_id IS NOT NULL")}
    (config.ROOT / "cache").mkdir(exist_ok=True)
    pool = {}
    with sess:
        g = sess.execute("INSERT INTO generations DEFAULT VALUES").lastrowid
        for name, by in (("only_a", [a]), ("both", [a, b])):
            c = sess.execute("INSERT INTO candidates (generation, artist, title, song_title, song_artists, is_extended)"
                             " VALUES (?, 'X', ?, ?, '[\"x\"]', 0)", (g, name, name)).lastrowid
            for s in by:
                sess.execute("INSERT INTO suggestions (generation, seed_track_id, source, rank, artist, title, outcome,"
                             " candidate_id) VALUES (?, ?, 'deezer', 1, 'X', ?, 'new', ?)", (g, seed[s], name, c))
            (config.ROOT / "cache" / f"{name}.webm").write_bytes(b"audio")
            pool[name] = sess.execute("INSERT INTO tracks (artist, title, file_path, duration_s, origin, candidate_id)"
                                      " VALUES ('X', ?, ?, 200, 'pool', ?)", (name, f"cache/{name}.webm", c)).lastrowid
    state = lambda n: sess.execute("SELECT veto_state, file_path FROM tracks WHERE id = ?", (pool[n],)).fetchone()
    library.set_stars(lib, pid, a, None)                         # unstarred, still in the playlist: nothing leaves
    find.sync_seeds(lib, sess, pid)
    assert state("only_a")[0] != "dropped"
    library.remove_from_playlist(lib, pid, a)
    find.sync_seeds(lib, sess, pid)
    assert tuple(state("only_a")) == ("dropped", None) and not (config.ROOT / "cache" / "only_a.webm").exists()
    assert state("both")[0] != "dropped"                          # b found it too
    from music_hunter.ratings import playable_ids
    assert pool["only_a"] not in playable_ids(sess)
    library.add_to_playlist(lib, pid, a)                          # back in the playlist: it returns (audio re-fetched)
    find.sync_seeds(lib, sess, pid)
    assert state("only_a")[0] == "playable"


def test_changes_reach_find_music_straight_away(setup, monkeypatch):
    """Starring, unstarring and removing in the playlist change Find music's seeds at once (not at the next pull);
    deleting the playlist deletes its Find music."""
    from fastapi.testclient import TestClient
    from music_hunter import web

    lib, pid, tracks = setup
    monkeypatch.setattr(web.Worker, "start_if_needed", lambda self: None)
    library.set_stars(lib, pid, tracks[0], 5)
    sess = db.connect(pid)
    find.sync_seeds(lib, sess, pid)
    client = TestClient(web.app)
    stars = lambda: {s.stars for s in seeds(sess)}
    client.put(f"/api/playlists/{pid}/tracks/{tracks[1]}", json={"stars": 2})
    assert len(seeds(sess)) == 2 and stars() == {5.0, 2.0}                       # a new seed, now
    client.put(f"/api/playlists/{pid}/tracks/{tracks[1]}", json={"stars": None})
    assert len(seeds(sess)) == 1                                                  # unstarred: not a seed, now
    client.delete(f"/api/playlists/{pid}/tracks/{tracks[0]}")
    assert seeds(sess) == []                                                      # removed: not a seed
    client.post(f"/api/playlists/{pid}/deleted/{tracks[0]}/restore")
    assert stars() == {5.0}                                                       # restored with its stars
    assert db.session_path(pid).exists()
    client.delete(f"/api/playlists/{pid}")
    assert not db.session_path(pid).exists()


def test_shared_pool_audio_is_kept_while_another_playlist_uses_it(tmp_path, monkeypatch):
    import music_hunter.config as config

    monkeypatch.setattr(config, "ROOT", tmp_path)
    (tmp_path / "cache").mkdir()
    f = tmp_path / "cache" / "vidX.webm"
    f.write_bytes(b"audio")
    a, b = db.connect(1), db.connect(2)
    for c in (a, b):
        with c:
            c.execute("INSERT INTO tracks (artist, title, duration_s, origin, file_path) VALUES ('x', 'y', 1, 'pool',"
                      " 'cache/vidX.webm')")
    with a:
        a.execute("UPDATE tracks SET file_path = NULL")
    db.release_audio(a, "cache/vidX.webm")
    assert f.exists()                                                             # playlist 2 still has it
    with b:
        b.execute("UPDATE tracks SET file_path = NULL")
    db.release_audio(b, "cache/vidX.webm")
    assert not f.exists()


def test_removed_add_counts_as_close(setup):
    """An Add removed from the playlist counts as Close; restored, it's an Add again."""
    from music_hunter.ratings import ADD
    CLOSE = 2

    lib, pid, tracks = setup
    library.set_stars(lib, pid, tracks[0], 4)
    sess = db.connect(pid)
    find.sync_seeds(lib, sess, pid)
    with sess:
        tid = sess.execute("INSERT INTO tracks (artist, title, duration_s, origin, video_id) VALUES ('N', 'F', 200, 'pool',"
                           " 'vidQ')").lastrowid
        sess.execute("INSERT INTO track_embeddings (track_id, model, dim, n_chunks, embedding)"
                     " SELECT ?, model, dim, n_chunks, embedding FROM track_embeddings LIMIT 1", (tid,))
        play = sess.execute("INSERT INTO plays (track_id, generation, play_number, is_control, rating, components, score,"
                            " rank, pool_size, provenance) VALUES (?, 1, 1, 0, ?, '{}', 0, 1, 1, '[]')",
                            (tid, ADD)).lastrowid
    find.sync_add(lib, sess, pid, play)
    lid = sess.execute("SELECT library_id FROM tracks WHERE id = ?", (tid,)).fetchone()[0]
    rating = lambda: sess.execute("SELECT rating FROM plays WHERE id = ?", (play,)).fetchone()[0]
    library.remove_from_playlist(lib, pid, lid)
    find.sync_seeds(lib, sess, pid)
    assert rating() == CLOSE
    library.restore_track(lib, pid, lid)
    find.sync_seeds(lib, sess, pid)
    assert rating() == ADD


def test_tracks_already_in_the_playlist_are_not_offered(setup):
    """A pool track that's (now) in the playlist — added by link, imported, from another playlist — isn't offered
    by Find music; if it leaves the playlist for good it can come back."""
    lib, pid, tracks = setup
    library.set_stars(lib, pid, tracks[0], 4)
    sess = db.connect(pid)
    find.sync_seeds(lib, sess, pid)
    with sess:
        tid = sess.execute("INSERT INTO tracks (artist, title, duration_s, origin, video_id) VALUES ('Someone', 'Song X',"
                           " 200, 'pool', 'vidOWN')").lastrowid
    with lib:
        lid = lib.execute("INSERT INTO tracks (artist, title, duration_s, origin, video_id) VALUES ('Someone',"
                          " 'Song X (Extended Mix)', 300, 'library', 'otherVid')").lastrowid
    state = lambda: sess.execute("SELECT veto_state FROM tracks WHERE id = ?", (tid,)).fetchone()[0]
    library.add_to_playlist(lib, pid, lid, "added")                     # the same song, another version
    find.sync_seeds(lib, sess, pid)
    assert state() == "owned"
    from music_hunter.ratings import playable_ids
    assert tid not in playable_ids(sess)
    library.remove_from_playlist(lib, pid, lid)                         # in Recently deleted: still not offered
    find.sync_seeds(lib, sess, pid)
    assert state() == "owned"
    library.delete_forever(lib, pid, lid)                                 # deleted for good: never offered again
    find.sync_seeds(lib, sess, pid)
    assert state() == "owned"
    library.add_to_playlist(lib, pid, lid)                               # back in (by hand): still in the playlist
    with lib:
        lib.execute("DELETE FROM playlist_tracks WHERE track_id = ?", (lid,))   # gone without a trace: offered again
    find.sync_seeds(lib, sess, pid)
    assert state() == "playable"


def test_add_changed_to_close_leaves_no_trace(setup, monkeypatch):
    """Changing an Add to Close in Find music: it's a Close (mild like) from then on — out of the playlist (not in
    Recently deleted), not a seed, and the tracks pulled for it leave the pool unless another seed found them."""
    from fastapi.testclient import TestClient
    from music_hunter import web
    from music_hunter.pull import mark_pulled

    lib, pid, tracks = setup
    monkeypatch.setattr(web.Worker, "start_if_needed", lambda self: None)
    library.set_stars(lib, pid, tracks[0], 4)
    sess = db.connect(pid)
    find.sync_seeds(lib, sess, pid)
    with sess:
        add = sess.execute("INSERT INTO tracks (artist, title, file_path, duration_s, origin, video_id) VALUES"
                           " ('New', 'Find', NULL, 200, 'pool', 'vidADD')").lastrowid
        sess.execute("INSERT INTO track_embeddings (track_id, model, dim, n_chunks, embedding)"
                     " SELECT ?, model, dim, n_chunks, embedding FROM track_embeddings LIMIT 1", (add,))
        play = sess.execute("INSERT INTO plays (track_id, generation, play_number, is_control, rating, rated_at,"
                            " components, score, rank, pool_size, provenance) VALUES (?, 1, 1, 0, 3, datetime('now'),"
                            " '{}', 0, 1, 1, '[]')", (add,)).lastrowid
        g = sess.execute("INSERT INTO generations DEFAULT VALUES").lastrowid
        cid = sess.execute("INSERT INTO candidates (generation, artist, title, song_title, song_artists, is_extended)"
                           " VALUES (?, 'X', 'Only from the Add', 'only from the add', '[\"x\"]', 0)", (g,)).lastrowid
        sess.execute("INSERT INTO suggestions (generation, seed_track_id, source, rank, artist, title, outcome,"
                     " candidate_id) VALUES (?, ?, 'deezer', 1, 'X', 'Only from the Add', 'new', ?)", (g, add, cid))
        found = sess.execute("INSERT INTO tracks (artist, title, duration_s, origin, candidate_id) VALUES"
                             " ('X', 'Only from the Add', 200, 'pool', ?)", (cid,)).lastrowid
    find.sync_add(lib, sess, pid, play)
    mark_pulled(sess, [add])
    lid = sess.execute("SELECT library_id FROM tracks WHERE id = ?", (add,)).fetchone()[0]
    assert any(t["id"] == lid and t["status"] == "added" for t in library.playlist_tracks(lib, pid))
    r = TestClient(web.app).post(f"/find/{pid}/api/change", json={"rating": 2})
    assert r.status_code == 200 and r.json()["rating"] == 2
    assert not any(t["id"] == lid for t in library.playlist_tracks(lib, pid))      # out of the playlist…
    assert library.deleted_tracks(lib, pid) == []                                   # …and not in the bin
    assert add not in {s.track_id for s in seeds(sess)}                              # not a seed
    assert sess.execute("SELECT veto_state FROM tracks WHERE id = ?", (found,)).fetchone()[0] == "dropped"
    assert sess.execute("SELECT rating, demoted FROM plays WHERE id = ?", (play,)).fetchone()[:] == (2, None)
