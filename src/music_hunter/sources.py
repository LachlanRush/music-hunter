"""Candidate sources: Last.fm artist similarity and YouTube Music radio/search (SPEC.md §4.1).

Calls are paced by [sources] api_delay_seconds. These return lists only — no downloads.
"""

import os
import time
from dataclasses import dataclass

import requests

from .config import CONFIG, ROOT
from .names import fold, parse_upload, split_artists

CFG = CONFIG["sources"]
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"


@dataclass
class Item:
    artist: str             # display artist(s), comma-separated
    title: str
    duration_s: float | None
    video_id: str | None = None   # YouTube Music ID, when known
    via: str | None = None        # Last.fm: the similar artist this came through


def _pause() -> None:
    time.sleep(CFG["api_delay_seconds"])


class CallStats:
    """Counts and times API calls (excluding the pacing pause) for the operational log."""

    def __init__(self):
        self.calls, self.errors, self.seconds = 0, 0, 0.0

    def timed(self, fn, *args, **kwargs):
        start = time.monotonic()
        self.calls += 1
        try:
            return fn(*args, **kwargs)
        except Exception:
            self.errors += 1
            raise
        finally:
            self.seconds += time.monotonic() - start

    def as_dict(self) -> dict:
        return {"calls": self.calls, "errors": self.errors, "seconds": round(self.seconds, 1)}


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def _seconds(length: str | None) -> float | None:
    if not length:
        return None
    secs = 0
    for part in length.split(":"):
        secs = secs * 60 + int(part)
    return float(secs)


class YouTubeMusic:
    def __init__(self):
        from ytmusicapi import YTMusic

        self.api = YTMusic()
        self.stats = CallStats()

    @staticmethod
    def _item(r: dict) -> Item:
        artist = ", ".join(a["name"] for a in r.get("artists") or [])
        title = r["title"]
        if r.get("videoType") == "MUSIC_VIDEO_TYPE_UGC":
            artist, title = parse_upload(artist, title)
        duration = r.get("duration_seconds") or _seconds(r.get("length") or r.get("duration"))
        return Item(artist, title, duration, r.get("videoId"))

    def search_songs(self, query: str) -> list[Item]:
        _pause()
        results = self.stats.timed(self.api.search, query, filter="songs", limit=CFG["yt_search_limit"])
        return [self._item(r) for r in results[:CFG["yt_search_limit"]] if r.get("videoId")]

    def radio(self, video_id: str) -> list[Item]:
        """Radio for a seed, excluding the seed itself, capped at ytmusic_radio_limit."""
        _pause()
        playlist = self.stats.timed(self.api.get_watch_playlist, video_id, radio=True,
                                    limit=CFG["ytmusic_radio_limit"] + 1)
        items = [self._item(t) for t in playlist.get("tracks", []) if t.get("videoId") != video_id]
        return items[:CFG["ytmusic_radio_limit"]]


class LastFM:
    def __init__(self):
        load_env()
        self.key = os.environ.get("LASTFM_API_KEY", "")
        self.stats = CallStats()
        if not self.key:
            raise RuntimeError("LASTFM_API_KEY is not set in .env")

    def _call(self, method: str, **params) -> dict:
        _pause()
        resp = self.stats.timed(requests.get, LASTFM_URL, timeout=30, params={
            "method": method, **params, "autocorrect": 1, "api_key": self.key, "format": "json"})
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Last.fm {method} error {data['error']}: {data.get('message')}")
        return data

    def similar_by_artist(self, seed_artist: str) -> list[Item]:
        """artist.getSimilar -> artist.getTopTracks (SPEC.md §4.1 step 2).

        Items carry the similar artist they came through in `via`, and no duration
        (getTopTracks has none; see `duration`).
        """
        own = split_artists(seed_artist)
        primary = seed_artist.split(",")[0].strip()
        similar = self._call("artist.getSimilar", artist=primary,
                             limit=CFG["lastfm_similar_artists"] + 5)["similarartists"]["artist"]
        artists, seen = [], set()
        for a in similar:
            key = split_artists(a["name"])
            if key & own or fold(a["name"]) in seen:
                continue
            seen.add(fold(a["name"]))
            artists.append(a["name"])
        # Similar artists Last.fm can't return tracks for (e.g. "Omar+") are skipped,
        # so up to lastfm_similar_artists artists with tracks are used.
        items, used = [], 0
        for name in artists:
            if used == CFG["lastfm_similar_artists"]:
                break
            try:
                tracks = self._call("artist.getTopTracks", artist=name,
                                    limit=CFG["lastfm_top_tracks"])["toptracks"]["track"]
            except RuntimeError as e:
                print(f"    Last.fm: skipping similar artist {name!r}: {e}")
                continue
            if tracks:
                used += 1
                items += [Item(t["artist"]["name"], t["name"], None, via=name)
                          for t in tracks[:CFG["lastfm_top_tracks"]]]
        return items

    def duration(self, artist: str, title: str) -> float | None:
        """Track length from track.getInfo, or None when Last.fm doesn't know it."""
        try:
            ms = int(self._call("track.getInfo", artist=artist, track=title)["track"].get("duration") or 0)
        except RuntimeError:
            return None
        return ms / 1000 if ms else None
