"""Cover art for the player (SPEC.md §14 item 10, §15.9): fetched once at download, stored locally.

YouTube Music's song details carry square album-art thumbnails; the largest is saved to
cache/art/<video_id>.jpg. Failures are logged and never block a track.
"""

import requests

from .config import path


def fetch(yt, video_id: str):
    """Save the track's cover art. Returns (file path or None, publish date 'YYYY-MM-DD' or None).

    The publish date stands in for the release date when Deezer didn't supply one.
    """
    song = yt.stats.timed(yt.api.get_song, video_id)
    micro = song.get("microformat", {}).get("microformatDataRenderer", {})
    published = (micro.get("publishDate") or micro.get("uploadDate") or "")[:10] or None
    thumbs = song.get("videoDetails", {}).get("thumbnail", {}).get("thumbnails", [])
    if not thumbs:
        return None, published
    best = max(thumbs, key=lambda t: t.get("width", 0) * t.get("height", 0))
    resp = requests.get(best["url"], timeout=30)
    resp.raise_for_status()
    folder = path("cache") / "art"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{video_id}.jpg"
    dest.write_bytes(resp.content)
    return dest, published
