"""Version-separated plugin state; retain the existing KiCad 9 locations."""
from __future__ import annotations

from pathlib import Path
import os
import sys


def profile_directory(base: str | Path) -> Path:
    from .ui.kicad_env import runtime_major
    directory = Path(base) / 'kicad_library_manager'
    return directory / '10.0' if runtime_major() == 10 else directory


def config_directory() -> Path:
    # Preserve the original default, including on macOS, for KiCad 9 users.
    return profile_directory(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')


def cache_directory() -> Path:
    if os.environ.get('XDG_CACHE_HOME'):
        base = Path(os.environ['XDG_CACHE_HOME'])
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library/Caches'
    elif sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local')
    else:
        base = Path.home() / '.cache'
    return profile_directory(base)
