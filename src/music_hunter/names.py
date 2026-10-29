"""Artist/title normalisation for duplicate and version detection (SPEC.md §4.1 step 4).

A "song" is identified by its normalised title plus its set of normalised artists.
Length/edit labels (Extended Mix, Radio Edit, Original Mix …) are stripped, so
versions of one song share a key; named remixes/edits ("X Remix", "Y Edit") are kept
in the title, so they stay separate tracks.
"""

import re
import unicodedata
from dataclasses import dataclass

from .config import CONFIG

VERSION_LABELS = {l.lower() for l in CONFIG["versions"]["labels"]}

_BRACKETS = re.compile(r"\s*[\(\[]([^\(\)\[\]]*)[\)\]]")
_FEAT = re.compile(r"\b(feat\.?|ft\.?|featuring)\s.*$", re.I)
_ARTIST_SPLIT = re.compile(r"\s*(?:,|&|\bx\b|\bvs\.?|\bfeat\.?|\bft\.?|\bfeaturing\b|\band\b)\s*", re.I)
_CATALOGUE = re.compile(r"^[A-Z]{1,12}\s?-?\d{2,5}[A-Z]?$")  # e.g. AF001, CATERINCA05


@dataclass(frozen=True)
class Song:
    title: str                  # normalised, version labels removed
    artists: frozenset[str]     # normalised
    is_extended: bool           # carried an "extended" version label

    def same_song(self, other: "Song") -> bool:
        return self.title == other.title and bool(self.artists & other.artists)


def fold(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse spaces."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"['’`]", "", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _is_label(part: str) -> bool:
    return fold(part) in VERSION_LABELS


def strip_versions(title: str) -> tuple[str, bool]:
    """Remove length/edit labels and featured artists from a title.

    Returns (title without labels, whether an 'extended' label was present).
    Handles "(Extended Mix)", "[Extended mix]", "- Extended Mix", "(Radio-Edit)",
    and mixed brackets like "(Tommy Mc Remix - Edit)" -> "(Tommy Mc Remix)".
    """
    extended = False

    def bracket(m: re.Match) -> str:
        nonlocal extended
        content = m.group(1)
        if _FEAT.match(content.strip()) or _CATALOGUE.match(content.strip()):
            return ""
        parts = [p for p in re.split(r"\s+-\s+", content)]
        kept = []
        for p in parts:
            if _is_label(p):
                extended |= "extended" in fold(p)
            else:
                kept.append(p)
        return f" ({' - '.join(kept)})" if kept else ""

    out = _BRACKETS.sub(bracket, title)
    # Dash-suffix labels: "234 - Extended Mix", "Trapped - Radio-Edit"
    parts = re.split(r"\s+-\s+", out)
    while len(parts) > 1 and _is_label(parts[-1]):
        extended |= "extended" in fold(parts.pop())
    out = " - ".join(parts)
    out = _FEAT.sub("", out)
    return out.strip(), extended


def split_artists(artist: str) -> frozenset[str]:
    artist = _BRACKETS.sub("", artist)            # "Capron (NL)" -> "Capron"
    return frozenset(a for a in (fold(p) for p in _ARTIST_SPLIT.split(artist)) if a)


def song(artists: list[str] | str, title: str) -> Song:
    if isinstance(artists, str):
        artists = [artists]
    clean, extended = strip_versions(title)
    names = frozenset().union(*(split_artists(a) for a in artists)) if artists else frozenset()
    return Song(fold(clean), names, extended)


def parse_upload(channel: str, title: str) -> tuple[str, str]:
    """User uploads put the channel in the artist field and 'Artist - Title' in the title.

    Returns (artist, title) parsed from the title when it has that form,
    otherwise the channel and title unchanged (often the artist's own channel).
    """
    if " - " in title:
        artist, rest = title.split(" - ", 1)
        rest = _BRACKETS.sub(lambda m: "" if _CATALOGUE.match(m.group(1).strip()) else m.group(0), rest)
        return artist.strip(), rest.strip()
    return channel, title
