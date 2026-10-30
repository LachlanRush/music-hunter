"""v3 milestone 2 check (offline): merging by ISRC and name, Extended preference, source families,
multi-source-first ordering, and matching on YouTube Music only when a candidate is selected."""

from music_hunter import pool
from music_hunter.pull import Matcher, Pull, families, seeds
from music_hunter.sources import Item


class FakeYT:
    """search_songs returns whatever the test put in `results` for that query."""

    def __init__(self, results):
        self.results = results

    def search_songs(self, query):
        return self.results.get(query, [])


def new_pull(conn):
    with conn:                                   # a fresh run: nothing shown, nothing in the pool
        conn.execute("UPDATE candidates SET status = 'not_selected' WHERE status = 'pool'")
    return Pull(conn, yt=None, lastfm=None)


def candidate(conn, cid):
    return conn.execute("SELECT * FROM candidates WHERE id = ?", (cid,)).fetchone()


def last_suggestion(conn):
    return conn.execute("SELECT * FROM suggestions ORDER BY id DESC LIMIT 1").fetchone()


def test_merging_and_families(conn):
    p = new_pull(conn)
    s1, s2 = seeds(conn)[:2]
    # Deezer suggestion (no YouTube ID yet), then TIDAL's with the same ISRC under a slightly different name.
    p.process(s1, "deezer", 1, Item("Zed Artist", "Night Drive", 241.0, deezer_id=1, isrc="QZTEST000001"))
    cid = last_suggestion(conn)["candidate_id"]
    assert candidate(conn, cid)["matched"] == 0 and candidate(conn, cid)["video_id"] is None
    p.process(s1, "tidal", 3, Item("Zed Artist feat. Someone", "Night Drive (Club Cut)", 240.0, isrc="QZTEST000001"))
    assert (last_suggestion(conn)["outcome"], last_suggestion(conn)["reason"]) == ("merged", "duplicate_isrc")
    # YouTube radio gives the video: the candidate is now matched (nothing to search for).
    p.process(s2, "ytmusic", 5, Item("Zed Artist", "Night Drive", 241.0, video_id="vidNIGHT"))
    assert last_suggestion(conn)["candidate_id"] == cid
    assert candidate(conn, cid)["video_id"] == "vidNIGHT" and candidate(conn, cid)["matched"] == 1
    # Families: YouTube radio and "You might also like" count separately (config [sources.families]);
    # the same source suggesting a track for two seeds is still one family.
    p.process(s1, "ytmusic", 2, Item("Other Artist", "Solo", 200.0, video_id="vidSOLO"))
    solo = last_suggestion(conn)["candidate_id"]
    p.process(s2, "ytmusic", 4, Item("Other Artist", "Solo", 200.0, video_id="vidSOLO"))
    p.process(s1, "ytmusic", 6, Item("Third Artist", "Pair", 200.0, video_id="vidPAIR"))
    pair = last_suggestion(conn)["candidate_id"]
    p.process(s1, "ytrelated", 7, Item("Third Artist", "Pair", None, video_id="vidPAIR"))
    fams = families(conn, [cid, solo, pair])
    assert fams[cid] == {"deezer", "tidal", "ytmusic"} and fams[solo] == {"ytmusic"}
    assert fams[pair] == {"ytmusic", "ytrelated"}
    assert pool.multi_source_order(conn, [cid, solo, pair]) == [cid, pair]


def test_extended_preferred(conn):
    p = new_pull(conn)
    s = seeds(conn)[0]
    p.process(s, "ytmusic", 1, Item("Zed Artist", "Sunrise", 200.0, video_id="vidSHORT"))
    cid = last_suggestion(conn)["candidate_id"]
    # TIDAL knows an Extended Mix: the candidate switches to it, keeping the radio video as a fallback.
    p.process(s, "tidal", 2, Item("Zed Artist", "Sunrise (Extended Mix)", 330.0, isrc="QZTEST000002"))
    assert last_suggestion(conn)["reason"] == "other_version"
    c = candidate(conn, cid)
    assert c["is_extended"] == 1 and c["matched"] == 0 and c["video_id"] == "vidSHORT"
    # When selected: the Extended version is found on YouTube Music…
    yt = FakeYT({"Zed Artist Sunrise (Extended Mix)": [Item("Zed Artist", "Sunrise (Extended Mix)", 331.0, "vidEXT")]})
    assert Matcher(conn, yt, None, None).resolve(cid) is None
    assert candidate(conn, cid)["video_id"] == "vidEXT"
    # …and if it isn't there, the known version is used rather than dropping the song.
    p.process(s, "ytmusic", 3, Item("Zed Artist", "Moonset", 210.0, video_id="vidMOON"))
    moon = last_suggestion(conn)["candidate_id"]
    p.process(s, "deezer", 4, Item("Zed Artist", "Moonset (Extended)", 300.0, deezer_id=2))
    assert Matcher(conn, FakeYT({}), None, None).resolve(moon) is None
    assert candidate(conn, moon)["video_id"] == "vidMOON" and candidate(conn, moon)["matched"] == 1


def test_matching_when_selected(conn):
    p = new_pull(conn)
    s1, s2 = seeds(conn)[:2]
    p.process(s1, "tidal", 1, Item("Zed Artist", "Echo", 250.0, isrc="QZTEST000003"))
    tid = last_suggestion(conn)["candidate_id"]
    p.process(s2, "ytmusic", 1, Item("Zed Artist & Friend", "Echo - Remastered", 250.0, video_id="vidECHO"))
    yt_cid = last_suggestion(conn)["candidate_id"]
    assert tid != yt_cid                         # different names: two candidates until matched
    p.process(s1, "deezer", 2, Item("Nobody", "Unfindable", 180.0, deezer_id=3))
    lost = last_suggestion(conn)["candidate_id"]

    yt = FakeYT({"Zed Artist Echo": [Item("Zed Artist", "Echo", 251.0, "vidECHO")]})
    m = Matcher(conn, yt, None, None)
    # Matching reveals the same YouTube video: merged, provenance moved, the other candidate takes the slot.
    why = m.resolve(tid)
    assert why.startswith("merged: duplicate_video_id") and m.merged_into == yt_cid
    srcs = {r[0] for r in conn.execute("SELECT source FROM suggestions WHERE candidate_id = ?", (yt_cid,))}
    assert srcs == {"tidal", "ytmusic"}
    # No YouTube Music result: match failed, with the reason kept.
    assert m.resolve(lost) == "match_failed: yt_match_failed:no_results"
    assert candidate(conn, lost)["status"] == "match_failed"
