"""Paced downloads for the vocals check. A YouTube refusal waits and tries again; no sign-in, no workarounds."""
import json, random, subprocess, sys, time
from pathlib import Path
D = json.load(open(sys.argv[1])); A = Path(sys.argv[2]); seen, todo = set(), []
for r in D:
    v = r["video_id"]
    if v not in seen and not list(A.glob(f"{v}.*")): todo.append(v)
    seen.add(v)
print(len(seen), "videos,", len(seen) - len(todo), "already here,", len(todo), "to fetch", flush=True)
pause = [600, 1200, 2400, 3600]; strikes = 0; failed = []
for n, v in enumerate(todo, 1):
    while True:
        p = subprocess.run(["yt-dlp", "--no-playlist", "-q", "--no-warnings", "-f", "251/bestaudio", "-o", str(A / "%(id)s.%(ext)s"),
                            f"https://music.youtube.com/watch?v={v}"], capture_output=True, text=True)
        err = p.stderr or ""
        if p.returncode == 0: strikes = 0; break
        if "confirm you" in err or "429" in err or "403" in err:
            w = pause[min(strikes, len(pause) - 1)]; strikes += 1
            print(f"refused at {n}; waiting {w//60} min", flush=True); time.sleep(w); continue
        failed.append(v); print(f"failed {v}: {err.strip()[:150]}", flush=True); break
    if n % 20 == 0: print(f"{n}/{len(todo)}", flush=True)
    time.sleep(5 + random.random() * 3)
print("downloads finished; failed:", failed, flush=True)
