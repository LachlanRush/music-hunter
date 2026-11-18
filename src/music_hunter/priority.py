"""The player first (2026-11-18): while the player is asking for something — the next song, its audio, its tempo
copy, a rating — background work (re-scoring, pulls, top-ups, restores, catch-ups) waits at its next checkpoint.

The Pi runs everything in one Python process, and Python runs one thread at a time: a background re-score of seven
Find music sessions held the start of a song up for ~5 s (2026-11-18 10:15). Background loops call `yield_to_player()`
between steps; it returns at once unless a player request is in flight or ended under `[priority] quiet_s` ago, and
never waits longer than `[priority] max_wait_s` at a time, so background work is slowed, never stopped.
"""

import contextvars
import re
import threading
import time

from .config import CONFIG

CFG = CONFIG.get("priority", {})
QUIET_S = CFG.get("quiet_s", 1.5)
MAX_WAIT_S = CFG.get("max_wait_s", 10.0)

# What the player waits for. Everything else (pages, lists, the PC worker's API) is ordinary.
PLAYER = re.compile(r"^(/find/\d+)?/(api/(next|resume|upcoming|rate|skip|change|recent|event|tempo)|audio/|tempo/)"
                    r"|^/api/playlists/\d+/(played|tempo)")

_busy = 0
_last = 0.0
_guard = threading.Condition()
_in_player = contextvars.ContextVar("in_player", default=False)


def is_player(path: str) -> bool:
    return bool(PLAYER.match(path))


def begin() -> None:
    global _busy
    with _guard:
        _busy += 1


def end() -> None:
    global _busy, _last
    with _guard:
        _busy = max(0, _busy - 1)
        _last = time.monotonic()
        _guard.notify_all()


def yield_to_player(max_wait: float | None = None) -> float:
    """Wait while the player is busy (or was a moment ago). Returns the seconds waited. Never waits inside a player
    request itself."""
    if _in_player.get():
        return 0.0
    limit = MAX_WAIT_S if max_wait is None else max_wait
    t0 = time.monotonic()
    with _guard:
        while True:
            now = time.monotonic()
            quiet_for = now - _last
            if _busy == 0 and quiet_for >= QUIET_S:
                break
            left = limit - (now - t0)
            if left <= 0:
                break
            _guard.wait(min(left, 0.1 if _busy else QUIET_S - quiet_for))
    return time.monotonic() - t0


class Middleware:
    """Pure ASGI: a player request counts from its start until its last byte is sent (an audio file included)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not is_player(scope.get("path", "")):
            return await self.app(scope, receive, send)
        begin()
        token = _in_player.set(True)
        try:
            await self.app(scope, receive, send)
        finally:
            _in_player.reset(token)
            end()
