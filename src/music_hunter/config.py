"""Loads config.toml from the project root. All tunable numbers live there."""

import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config.toml"

with CONFIG_PATH.open("rb") as f:
    CONFIG = tomllib.load(f)


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
