from __future__ import annotations

import os
import sys


def cache_root_dir() -> str:
    """
    Cross-platform user-local cache directory.
    """
    try:
        xdg = str(os.environ.get("XDG_CACHE_HOME") or "").strip()
    except Exception:
        xdg = ""
    if xdg:
        return xdg

    home = os.path.expanduser("~")

    if sys.platform == "win32":
        try:
            base = str(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or "").strip()
        except Exception:
            base = ""
        if base:
            return base
        return os.path.join(home, "AppData", "Local")

    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Caches")

    return os.path.join(home, ".cache")


def plugin_cache_dir() -> str:
    """
    Cache dir for this plugin (previews, remote-sha state, logs).
    """
    from ..paths import cache_directory
    d = str(cache_directory())
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d

