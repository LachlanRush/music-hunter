import json, re, sqlite3
from datetime import datetime
import numpy as np
S = "runs/pi/2026-11-09_1114"; ts = lambda s: datetime.fromisoformat(s[:19]); rows = []
for pid in (1, 2, 3):
    c = sqlite3.connect(f"{S}/data/sessions/playlist-{pid}.db"); c.row_factory = sqlite3.Row
    sug = {}
    for r in c.execute("SELECT candidate_id, source FROM suggestions WHERE candidate_id IS NOT NULL"): sug.setdefault(r[0], set()).add(r[1])
    run, last = 0, None
    for q in c.execute("SELECT q.*, t.duration_s d, t.candidate_id cid, t.artist a FROM plays q JOIN tracks t ON t.id=q.track_id ORDER BY q.id").fetchall():
        t0 = ts(q["shown_at"]); run = run + 1 if last and (t0 - last).total_seconds() < 1800 else 1; last = t0
        v = json.loads(q["veto"]) if q["veto"] else {}
        if q["rating"] and not q["is_control"] and "z_components" in v:
            rows.append(dict(r=q["rating"], sit=run, d=q["d"] or 0, lf="lastfm" in sug.get(q["cid"], set()), pid=pid, wk=t0.weekday() >= 5, n=1 + q["a"].count(",") + q["a"].count("&"), secs=q["played_seconds"] or 0))
def show(lab, m):
    x = [r for r in rows if m(r)]; n = len(x)
    if n: print(f"  {lab:24s} {n:4d} plays  Off {100*sum(r['r']==1 for r in x)/n:3.0f}%  Close {100*sum(r['r']==2 for r in x)/n:3.0f}%  Add {100*sum(r['r']==3 for r in x)/n:3.0f}%")
print("position in a sitting (gap of 30+ min starts a new one)")
for a, b in ((1, 5), (6, 10), (11, 20), (21, 999)): show(f"plays {a}-{b}", lambda r: a <= r["sit"] <= b)
print("track length")
for a, b in ((0, 240), (240, 330), (330, 420), (420, 9999)): show(f"{a//60}:{a%60:02d}-{b//60}:{b%60:02d}", lambda r: a <= r["d"] < b)
print("suggested by Last.fm"); show("yes", lambda r: r["lf"]); show("no", lambda r: not r["lf"])
print("credited artists"); show("one", lambda r: r["n"] == 1); show("two or more", lambda r: r["n"] > 1)
print("day"); show("weekday", lambda r: not r["wk"]); show("weekend", lambda r: r["wk"])
