from __future__ import annotations

import datetime as _dt
import os as _os
import threading as _threading


_LOCK = _threading.Lock()


def _log_path() -> str:
    # Keep it simple and user-accessible.
    # Linux: ~/.cache/kicad_library_manager/footprint_browser_debug.log
    from ..cache_dir import plugin_cache_dir
    d = plugin_cache_dir()
    try:
        _os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return _os.path.join(d, "footprint_browser_debug.log")


def log_line(msg: str) -> None:
    """
    Best-effort append-only debug logging.

    This must never crash the plugin. It also truncates the file if it grows too large.
    """
    try:
        if str(_os.environ.get("KICAD_LIBRARY_MANAGER_DEBUG", "")).strip().lower() not in ("1", "true", "yes", "on"):
            return
    except Exception:
        return
    try:
        p = _log_path()
        with _LOCK:
            try:
                if _os.path.exists(p) and _os.path.getsize(p) > 1_000_000:
                    # Keep last ~200KB
                    try:
                        with open(p, "rb") as f:
                            f.seek(-200_000, 2)
                            tail = f.read()
                        with open(p, "wb") as f:
                            f.write(tail)
                    except Exception:
                        pass
            except Exception:
                pass

            ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(p, "a", encoding="utf-8", errors="ignore") as f:
                f.write(f"[{ts}] {msg}\n")
    except Exception:
        return

