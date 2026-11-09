"""Every rated Find music play on the Pi, with its tags: the list for the vocals check, most useful first."""
import json, sqlite3, sys
root = sys.argv[1]; out = []
for pid in (1, 2, 3):
    c = sqlite3.connect(f"{root}/data/sessions/playlist-{pid}.db"); c.row_factory = sqlite3.Row
    for r in c.execute("SELECT p.id, p.rating, p.is_control, p.tags, t.video_id, t.artist, t.title, t.id AS tid FROM plays p JOIN tracks t ON t.id=p.track_id WHERE p.rating IS NOT NULL AND t.video_id IS NOT NULL ORDER BY p.id"):
        tags = json.loads(r["tags"]) if r["tags"] else []
        out.append(dict(key=f"p{pid}:{r['id']}", pid=pid, play=r["id"], rating=r["rating"], control=r["is_control"], tags=tags, video_id=r["video_id"], artist=r["artist"], title=r["title"]))
out.sort(key=lambda r: (not ({"vocals", "lyrics", "great vocals"} & set(r["tags"])), not r["tags"]))
json.dump(out, open(sys.argv[2], "w"), indent=0)
print(len(out), "plays;", len({r["video_id"] for r in out}), "distinct videos; vocals-tagged", sum(bool({"vocals", "lyrics"} & set(r["tags"])) for r in out))
