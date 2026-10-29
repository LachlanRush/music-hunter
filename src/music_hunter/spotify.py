"""Read track details from Spotify's public pages (no API or account) — SPEC.md §15.1.

Track pages carry artist, title, length and release date in their meta tags; a
playlist's embed page lists its track IDs. These are unofficial pages, so parsing may
need fixing if Spotify changes them.
"""

import html
import re
from dataclasses import dataclass

import requests

LINK = re.compile(r"open\.spotify\.com/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?(?:embed/)?(track|playlist)/([A-Za-z0-9]{22})")
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) music-hunter/0.2"}


@dataclass
class SpotifyTrack:
    id: str
    title: str
    artists: list[str]
    duration_s: float
    release_date: str | None


def parse(link: str) -> tuple[str, str]:
    """('track' | 'playlist', id) from a Spotify link (query strings like ?si=… are ignored)."""
    m = LINK.search(link)
    if not m:
        raise ValueError(f"Not a Spotify track or playlist link: {link}")
    return m.group(1), m.group(2)


def _page(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def _meta(page: str, key: str) -> str | None:
    m = re.search(rf'<meta (?:property|name)="{re.escape(key)}" content="([^"]*)"', page)
    return html.unescape(m.group(1)) if m else None


def track(track_id: str) -> SpotifyTrack:
    page = _page(f"https://open.spotify.com/track/{track_id}")
    title, artists, duration = _meta(page, "og:title"), _meta(page, "music:musician_description"), _meta(page, "music:duration")
    if not (title and artists and duration):
        raise ValueError(f"Couldn't read title/artists/length from Spotify track {track_id}")
    return SpotifyTrack(track_id, title, [a.strip() for a in artists.split(",") if a.strip()],
                        float(duration), _meta(page, "music:release_date"))


def playlist_track_ids(playlist_id: str) -> list[str]:
    page = _page(f"https://open.spotify.com/embed/playlist/{playlist_id}")
    return list(dict.fromkeys(re.findall(r"spotify:track:([A-Za-z0-9]{22})", page)))


def expand(links: list[str]) -> list[str]:
    """Track IDs from a mix of track and playlist links, in order, without repeats."""
    ids = []
    for link in links:
        kind, sid = parse(link)
        ids += [sid] if kind == "track" else playlist_track_ids(sid)
    return list(dict.fromkeys(ids))
