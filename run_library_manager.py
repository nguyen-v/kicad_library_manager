from __future__ import annotations

import datetime as _dt
import os as _os
import sys as _sys
import json as _json


def _boot_log_path() -> str:
    base = _os.environ.get("XDG_CACHE_HOME") or _os.path.join(_os.path.expanduser("~"), ".cache")
    d = _os.path.join(base, "kicad_library_manager")
    try:
        _os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return _os.path.join(d, "ipc_plugin_boot.log")


def _boot_log(msg: str) -> None:
    try:
        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_boot_log_path(), "a", encoding="utf-8", errors="ignore") as f:
            f.write(f"[{ts}] [bundle_entry] {msg}\n")
    except Exception:
        return


def _pid_file_path() -> str:
    base = _os.environ.get("XDG_CACHE_HOME") or _os.path.join(_os.path.expanduser("~"), ".cache")
    d = _os.path.join(base, "kicad_library_manager")
    try:
        _os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return _os.path.join(d, "ipc_plugin_pid.json")


def _write_pid_file() -> None:
    try:
        payload = {
            "pid": _os.getpid(),
            "exe": _sys.executable,
            "cwd": _os.getcwd(),
            "argv": list(_sys.argv),
            "kicad_api_socket": _os.environ.get("KICAD_API_SOCKET"),
        }
        with open(_pid_file_path(), "w", encoding="utf-8", errors="ignore") as f:
            f.write(_json.dumps(payload, indent=2, sort_keys=True))
            f.write("\n")
    except Exception:
        return


def main() -> int:
    # Ensure `import library_manager` works when run as a script.
    root = _os.path.abspath(_os.path.dirname(__file__))
    if root not in _sys.path:
        _sys.path.insert(0, root)

    _boot_log("=== bundle entrypoint start ===")
    _boot_log(f"pid={_os.getpid()}")
    # The inner entrypoint writes the PID after choosing the editor's profile
    # and acquiring its single-instance lock.
    _boot_log(f"argv={_sys.argv!r}")
    _boot_log(f"cwd={_os.getcwd()!r}")
    _boot_log(f"exe={_sys.executable!r}")
    try:
        _boot_log(f"KICAD_API_SOCKET={_os.environ.get('KICAD_API_SOCKET')!r}")
        _boot_log(f"KICAD_API_TOKEN={'set' if _os.environ.get('KICAD_API_TOKEN') else 'missing'}")
    except Exception:
        pass

    from library_manager.plugin import main as _inner_main  # type: ignore

    return int(_inner_main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())

