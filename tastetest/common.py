"""Shared paths, settings and helpers for the taste-model test (SPEC.md §16.1)."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))           # reuse music_hunter.names for artist normalisation

with (ROOT / "config.toml").open("rb") as f:
    CFG = tomllib.load(f)["taste_test"]
OUT = ROOT / CFG["output"]
FEATURES = OUT / "features"
AUDIO = OUT / "audio"

# (run, database, project root the stored file paths are relative to, "liked" threshold)
RUNS = [("v1", ROOT / "runs" / "v1" / "data" / "music_hunter.db", ROOT / "runs" / "v1", CFG["liked_v1"]),
        ("v2", ROOT / "runs" / "v2" / "data" / "music_hunter.db", ROOT / "runs" / "v2", CFG["liked_v2"]),
        ("v3", ROOT / "data" / "music_hunter.db", ROOT, CFG["liked_v3"])]


def decode(path: Path, sr: int) -> np.ndarray:
    """Any audio file → mono float32 at sr (ffmpeg)."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32).copy()


def split(y: np.ndarray, sr: int, seconds: float, min_tail: float = 0.5) -> list[np.ndarray]:
    """Consecutive windows of `seconds`; a final window shorter than min_tail × seconds is dropped."""
    n = int(seconds * sr)
    pieces = [y[i:i + n] for i in range(0, len(y), n)]
    if len(pieces) > 1 and len(pieces[-1]) < min_tail * n:
        pieces.pop()
    return [np.pad(p, (0, n - len(p))) for p in pieces]


def load_dataset() -> list[dict]:
    return json.loads((OUT / "dataset.json").read_text())


def save_features(name: str, keys: list[str], track: np.ndarray, chunks: list | None = None,
                  names: list[str] | None = None, meta: dict | None = None) -> None:
    """features/<name>.npz: keys, track-level features (n, …), optional per-chunk arrays and feature names.

    With TASTE_RUN set (extracting one run only), the new tracks are merged into the existing file."""
    import os

    FEATURES.mkdir(parents=True, exist_ok=True)
    old_file = FEATURES / f"{name}.npz"
    if os.environ.get("TASTE_RUN") and old_file.exists():
        old = np.load(old_file, allow_pickle=True)
        keep = [i for i, k in enumerate(old["keys"]) if k not in set(keys)]
        keys = [str(k) for k in old["keys"][keep]] + list(keys)
        track = np.concatenate([old["track"][keep], track])
        if chunks is not None and "chunks" in old:
            chunks = list(old["chunks"][keep]) + list(chunks)
    payload = {"keys": np.array(keys), "track": track, "meta": np.array(json.dumps(meta or {}))}
    if chunks is not None:
        payload["chunks"] = np.array(chunks, dtype=object)
    if names is not None:
        payload["names"] = np.array(names)
    np.savez(FEATURES / f"{name}.npz", **payload)


def load_features(name: str) -> dict:
    with np.load(FEATURES / f"{name}.npz", allow_pickle=True) as z:
        return {k: z[k] for k in z.files}
