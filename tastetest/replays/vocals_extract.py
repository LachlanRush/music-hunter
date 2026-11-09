"""Demucs vocal share and Essentia's voice / gender / other descriptors for the Pi's rated plays (taste-test tooling)."""
import json, sys
from pathlib import Path
sys.path.insert(0, "tastetest")
import common, extract
D = json.load(open("runs/taste-test/vocals/dataset.json")); seen = {}
for r in D:
    hits = sorted(common.AUDIO.glob(f"{r['video_id']}.*"))
    if hits and r["video_id"] not in seen: seen[r["video_id"]] = {"key": r["video_id"], "audio": hits[0]}
rows = list(seen.values()); print(len(rows), "tracks", flush=True)
extract.tracks = lambda: rows
F = Path("runs/taste-test/vocals/features"); F.mkdir(parents=True, exist_ok=True)
common.FEATURES = F
extract.FEATURES = F
for name in ("stems", "essentia"):
    extract.RUNNERS[name]()
print("extraction finished", flush=True)
