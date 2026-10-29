"""Audio decoding (via ffmpeg) and artist/title tag reading."""

import subprocess
from pathlib import Path

import mutagen
import numpy as np

from .config import CONFIG


def decode(file: Path, sample_rate: int) -> np.ndarray:
    """Decode any audio file to mono float32 at the given sample rate."""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(file),
         "-f", "f32le", "-ac", "1", "-ar", str(sample_rate), "-"],
        capture_output=True, check=True,
    )
    return np.frombuffer(result.stdout, dtype=np.float32)


def artist_title(file: Path) -> tuple[str, str]:
    """Artist and title from tags, falling back to an 'Artist - Title' filename."""
    tags = mutagen.File(file, easy=True)
    if tags is not None:
        artist = ", ".join(a.strip() for a in tags.get("artist") or [] if a.strip())
        title = (tags.get("title") or [""])[0].strip()
        if artist and title:
            return artist, title
    if " - " in file.stem:
        artist, title = file.stem.split(" - ", 1)
        return artist.strip(), title.strip()
    raise ValueError(f"No artist/title tags and filename is not 'Artist - Title': {file.name}")


def audio_files(folder: Path) -> list[Path]:
    exts = set(CONFIG["audio"]["extensions"])
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in exts)
