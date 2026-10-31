"""Loads config.toml from the project root. All tunable numbers live there."""

import os
import tomllib
from pathlib import Path

# MUSIC_HUNTER_ROOT moves the data (database, audio, .env and an optional config.toml) elsewhere, e.g. /data in the
# Home Assistant add-on or a scratch copy; without it, everything lives in the project folder.
PROJECT = Path(__file__).resolve().parents[2]
ROOT = Path(os.environ.get("MUSIC_HUNTER_ROOT") or PROJECT)
CONFIG_PATH = PROJECT / "config.toml"


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        base[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return base


with CONFIG_PATH.open("rb") as f:
    CONFIG = tomllib.load(f)
# With a separate data root, its config.toml (written by the add-on from its options) and config.local.toml (my own
# tweaks) override the project's settings key by key, so new settings in later versions still get their defaults.
if ROOT != PROJECT:
    for extra in ("config.toml", "config.local.toml"):
        if (ROOT / extra).exists():
            with (ROOT / extra).open("rb") as f:
                _merge(CONFIG, tomllib.load(f))


# ROOT is looked up at call time so tests can point the project at a temporary folder.

def path(name: str) -> Path:
    """Resolve a [paths] entry relative to the project root."""
    return ROOT / CONFIG["paths"][name]


def resolve(rel: str) -> Path:
    """A file path stored in the DB (relative to the project root) as an absolute path."""
    return ROOT / rel


def relative(file: Path) -> str:
    """An absolute path inside the project as stored in the DB."""
    # abspath, not resolve(): a symlinked folder (e.g. cache/ on another drive) stays inside the project.
    return str(Path(os.path.abspath(file)).relative_to(os.path.abspath(ROOT)))
