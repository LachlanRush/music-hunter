"""The playlist's shuffle round (static/shuffle.js, feedback #33), run in Node when it's available."""

import json
import shutil
import subprocess

import pytest

import music_hunter.config as config

SCRIPT = """
const ShuffleRound = require(%s);
const data = {};
const store = {getItem: (k) => k in data ? data[k] : null, setItem: (k, v) => { data[k] = v; }};
const tracks = [1, 2, 3, 4, 5, 6].map((id) => ({id}));
const ids = (l) => l.map((t) => t.id);
const out = {};
let r = ShuffleRound.create(7, store);
for (const t of r.order(tracks).slice(0, 4)) r.mark(t.id);
const heard = JSON.parse(data["shuffle-heard:7"]);
r = ShuffleRound.create(7, store);                 // the page is reloaded
out.heard = heard;
out.after_reload = ids(r.order(tracks));
for (const id of out.after_reload.slice(0, 2)) r.mark(id);
out.last = out.after_reload[1];
out.new_round = ids(r.order(tracks));
r.setOn(true);
out.on = ShuffleRound.create(7, store).on();
out.other_playlist_on = ShuffleRound.create(8, store).on();
const broken = {getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }};
const b = ShuffleRound.create(9, broken);
b.mark(1); b.setOn(true);
out.blocked = ids(b.order(tracks)).length;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_shuffle_round_survives_a_reload():
    js = config.ROOT / "src" / "music_hunter" / "static" / "shuffle.js"
    out = json.loads(subprocess.run(["node", "-e", SCRIPT % json.dumps(str(js))], capture_output=True, text=True,
                                    check=True).stdout)
    assert set(out["after_reload"][2:]) == set(out["heard"])       # heard tracks wait for the rest
    assert set(out["after_reload"][:2]).isdisjoint(out["heard"])
    assert sorted(out["new_round"]) == [1, 2, 3, 4, 5, 6]          # all heard: a new round, whole playlist
    assert out["new_round"][-1] == out["last"]                     # the last one played doesn't come straight back
    assert out["on"] is True and out["other_playlist_on"] is False
    assert out["blocked"] == 6                                     # browser storage blocked: still plays
