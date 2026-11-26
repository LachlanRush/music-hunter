"""The player first (2026-11-18): background work waits while the player asks for something, and stretched copies
for the player are rendered before background ones."""

import threading
import time
from pathlib import Path

from music_hunter import priority, tempo


def test_player_paths():
    for p in ["/find/7/api/next", "/find/7/api/resume", "/find/7/audio/538", "/find/7/tempo/538",
              "/audio/12", "/tempo/12/audio", "/api/playlists/7/played", "/api/playlists/7/tempo"]:
        assert priority.is_player(p), p
    for p in ["/", "/playlist/7", "/api/playlists", "/api/worker/claim", "/api/playlists/7"]:
        assert not priority.is_player(p), p


def test_background_waits_for_the_player(monkeypatch):
    monkeypatch.setattr(priority, "QUIET_S", 0.2)
    priority.begin()
    waited = {}
    t = threading.Thread(target=lambda: waited.setdefault("s", priority.yield_to_player(max_wait=5)))
    t.start()
    time.sleep(0.3)
    assert t.is_alive()                                  # still waiting while the request runs
    priority.end()
    t.join(2)
    assert 0.4 < waited["s"] < 2                          # … then the quiet spell, then on


def test_never_waits_forever(monkeypatch):
    priority.begin()
    try:
        assert priority.yield_to_player(max_wait=0.2) < 0.5
    finally:
        priority.end()


def test_player_renders_jump_the_queue(monkeypatch, tmp_path):
    order, gate = [], threading.Event()

    def fake_render(src, dest, rate, background=False):
        gate.wait(5)
        order.append(dest.name)
        dest.write_bytes(b"x")
    monkeypatch.setattr(tempo, "_render", fake_render)
    monkeypatch.setattr(tempo.db, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(tempo.db, "connect_library", lambda: None)
    r = tempo.Renderer()
    src = Path(tmp_path / "a.opus")
    for i in range(3):
        r.submit(src, tmp_path / f"pl1-{i}-1.050.webm", 1.05, tempo.LATER)
    time.sleep(0.1)                                       # the "any" lane is busy with the first background one
    assert r.submit(src, tmp_path / "find7-9-1.050.webm", 1.05, tempo.NOW) == "rendering"
    gate.set()
    for _ in range(100):
        if len(order) == 4:
            break
        time.sleep(0.05)
    assert len(order) == 4
    assert order.index("find7-9-1.050.webm") <= 1         # done by the player's lane, not after the queue


def test_far_off_song_doesnt_stretch_the_tempo_range():
    """No More (#71): a 4★ song at 127 BPM among ~140 BPM ones no longer pulls the range down to 127."""
    items = [(127.0, 4), (129.8, 4), (131.2, 5), (135.7, 4), (136.0, 5), (140.0, 5), (141.8, 4), (142.1, 5),
             (142.1, 4), (142.2, 5)]
    core = tempo.core_from(items)
    assert core["lo"] >= 135 and core["hi"] == 142.2
    few = [(127.0, 5), (142.0, 5), (143.0, 5)]                    # too few left without it: kept
    assert tempo.core_from(few)["lo"] == 127.0


def test_tempo_ties_go_to_the_plain_reading():
    """#144: one starred song (Love Sensation, 117) made Disco 175.8 (its 3:2); Haus Muzik's 139–143 songs came out
    as 94.8 (their 2:3) on rounding noise. d&b's misread songs still fold to 174."""
    assert tempo.core_from([(117.17, 5)])["center"] == 117.2
    assert tempo.core_from([(139.12, 4), (142.04, 5), (142.26, 5), (143.0, 5)])["center"] == 142.3
    assert tempo.core_from([(173, 5), (116, 5), (87, 5), (174, 5)])["center"] == 174


def test_wide_tempo_playlist_isnt_folded():
    """Soul Set (2026-11-26): 5-star songs at 98-158 BPM were all folded (x1.5, x2) to agree at ~174, so the playlist's
    tempo became 196 and every song showed 170-240. Folding now costs a little, so they keep their measured tempo."""
    soul = [(138.06, 5), (158.09, 5), (115.77, 5), (99.09, 5), (125.16, 5), (98.11, 5), (115.53, 5), (115.92, 5)]
    core = tempo.core_from(soul)
    assert 110 < core["center"] < 120
    assert [tempo.shown_bpm(b, core["center"]) for b in (98.11, 138.06, 158.09)] == [98, 138, 158]
    assert tempo.shown_bpm(87, 174) == 174 and tempo.shown_bpm(93.6, 142) == 140     # real misreads still fold


def test_find_music_prune_keeps_renders_in_flight(tmp_path, monkeypatch):
    """A copy being rendered for the pick just started was cancelled by the prune after a rating, deleted the moment
    it was made (404 in the player) and rendered again (2026-11-25)."""
    dest = tmp_path / "find7-3719-1.044.webm"
    with tempo.RENDERER.cv:
        tempo.RENDERER.running["test"] = dest
    try:
        monkeypatch.setattr(tempo, "_copies", lambda scope: iter(()))
        monkeypatch.setattr(tempo, "session_settings", lambda sess, session: {"on": True, "target": 142.0,
                                                                              "engaged": True})
        import sqlite3
        sess = sqlite3.connect(":memory:")
        sess.executescript("CREATE TABLE plays (id INTEGER PRIMARY KEY, track_id INT);"
                           "CREATE TABLE tracks (id INTEGER PRIMARY KEY, bpm REAL, veto_state TEXT);"
                           "INSERT INTO tracks VALUES (3719, 136.0, NULL); INSERT INTO plays (track_id) VALUES (3719);")
        tempo.sync_session(sess, 7)
        assert dest not in tempo.RENDERER.cancelled
    finally:
        with tempo.RENDERER.cv:
            tempo.RENDERER.running.pop("test", None)
            tempo.RENDERER.cancelled.discard(dest)


def test_age_restricted_video_isnt_youtube_blocking_us():
    """#77: one age-restricted video paused every download for up to 40 min at a time."""
    from music_hunter.analysis import refusal
    assert refusal("ERROR: [youtube] tpzcD8krTrU: Sign in to confirm your age. This video may be inappropriate") is None
    assert refusal("ERROR: [youtube] x: Sign in to confirm you're not a bot.") == "blocked"
    assert refusal("ERROR: unable to download video data: HTTP Error 403: Forbidden") == "temporary"
