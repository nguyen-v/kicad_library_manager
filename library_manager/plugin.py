from __future__ import annotations

import os
import sys
import traceback
import datetime as _dt
import json as _json

_INSTANCE_CHECKER = None


def _cache_root_dir() -> str:
    """
    Cross-platform user-local cache directory.
    """
    # Respect XDG when set (Linux and some macOS setups).
    try:
        xdg = str(os.environ.get("XDG_CACHE_HOME") or "").strip()
    except Exception:
        xdg = ""
    if xdg:
        return xdg

    home = os.path.expanduser("~")

    # Windows: prefer LocalAppData.
    if sys.platform == "win32":
        try:
            base = str(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or "").strip()
        except Exception:
            base = ""
        if base:
            return base
        return os.path.join(home, "AppData", "Local")

    # macOS: conventional cache location.
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Caches")

    # Linux / other POSIX.
    return os.path.join(home, ".cache")


def _plugin_cache_dir() -> str:
    d = os.path.join(_cache_root_dir(), "kicad_library_manager")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _boot_log_path() -> str:
    return os.path.join(_plugin_cache_dir(), "ipc_plugin_boot.log")


def _boot_log(msg: str) -> None:
    """
    Always-on, best-effort boot log for IPC launch debugging.
    """
    try:
        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_boot_log_path(), "a", encoding="utf-8", errors="ignore") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        return


def _pid_file_path() -> str:
    return os.path.join(_plugin_cache_dir(), "ipc_plugin_pid.json")


def _write_pid_file() -> None:
    """
    Always-on best-effort PID file to make gdb attach easy.
    """
    try:
        payload = {
            "pid": os.getpid(),
            "exe": sys.executable,
            "cwd": os.getcwd(),
            "argv": list(sys.argv),
            "kicad_api_socket": os.environ.get("KICAD_API_SOCKET"),
        }
        with open(_pid_file_path(), "w", encoding="utf-8", errors="ignore") as f:
            f.write(_json.dumps(payload, indent=2, sort_keys=True))
            f.write("\n")
    except Exception:
        return


def _single_instance_dir() -> str:
    """
    Directory for the single-instance lock.
    Keep consistent with other boot artifacts (PID file, boot log).
    """
    return _plugin_cache_dir()


def _instance_user_key() -> str:
    """
    A stable per-user key for the lock name (cross-platform).
    """
    try:
        return str(os.getuid())
    except Exception:
        pass
    try:
        u = str(os.environ.get("USERNAME") or os.environ.get("USER") or "").strip()
        if u:
            return u
    except Exception:
        pass
    return "user"


def _pid_is_alive(pid: int) -> bool:
    """
    Best-effort check whether a PID is alive.
    """
    try:
        p = int(pid)
    except Exception:
        return False
    if p <= 0:
        return False
    try:
        # POSIX: signal 0 checks existence without sending a signal.
        if hasattr(os, "kill"):
            os.kill(p, 0)
            return True
    except PermissionError:
        # Process exists but we can't signal it.
        return True
    except ProcessLookupError:
        return False
    except Exception:
        pass
    # Unknown platform / failure: do not claim alive.
    return False


def _read_existing_pid() -> int | None:
    try:
        p = _pid_file_path()
        if not os.path.isfile(p):
            return None
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
        d = _json.loads(txt or "{}")
        pid = d.get("pid")
        return int(pid) if str(pid).isdigit() else None
    except Exception:
        return None


def _ensure_single_instance_or_notify(app) -> bool:
    """
    Return True if this is the only running instance.
    If another instance is running, show a message and return False.
    """
    global _INSTANCE_CHECKER
    try:
        import wx  # type: ignore

        # One instance per user account (not per-project).
        name = f"kicad_library_manager_single_instance_{_instance_user_key()}"
        lock_path = os.path.join(_single_instance_dir(), name)
        _INSTANCE_CHECKER = wx.SingleInstanceChecker(name, _single_instance_dir())
        if _INSTANCE_CHECKER.IsAnotherRunning():
            # If we can detect that the previous instance is gone, clear stale lock artifacts
            # so we don't permanently lock out the user after a crash.
            existing_pid = _read_existing_pid()
            if existing_pid is not None and not _pid_is_alive(existing_pid):
                try:
                    os.remove(lock_path)
                except Exception:
                    pass
                try:
                    os.remove(_pid_file_path())
                except Exception:
                    pass
                try:
                    _INSTANCE_CHECKER = wx.SingleInstanceChecker(name, _single_instance_dir())
                except Exception:
                    _INSTANCE_CHECKER = None
                try:
                    if _INSTANCE_CHECKER and not _INSTANCE_CHECKER.IsAnotherRunning():
                        return True
                except Exception:
                    pass

            try:
                pid_hint = f"\n\nDetected running PID: {existing_pid}" if existing_pid else ""
                wx.MessageBox(
                    "KiCad Library Manager is already running.\n\n"
                    "Close the existing window before launching it again."
                    + pid_hint
                    + "\n\n"
                    "If you can’t find the window, it may be hidden in the background.\n"
                    "You can terminate it and try again.",
                    "KiCad Library Manager",
                    wx.OK | wx.ICON_INFORMATION,
                )
            except Exception:
                pass
            try:
                # Best-effort release.
                _INSTANCE_CHECKER = None
            except Exception:
                pass
            try:
                if wx.GetApp() is app and not wx.GetTopLevelWindows():
                    app.ExitMainLoop()
            except Exception:
                pass
            return False
    except Exception:
        # If the checker fails for any reason, do not block the plugin.
        return True
    return True


def _ensure_sys_path_for_package() -> None:
    """
    KiCad IPC plugins run as standalone scripts. When this file is executed directly,
    Python's sys.path includes this directory (library_manager/), but NOT its parent.

    Our codebase uses package-relative imports (e.g. ui imports ..repo), so we must add
    the parent directory to sys.path so `import library_manager` works.
    """

    this_dir = os.path.abspath(os.path.dirname(__file__))
    parent = os.path.dirname(this_dir)
    if parent and parent not in sys.path:
        sys.path.insert(0, parent)


def _show_error_dialog(title: str, message: str) -> None:
    try:
        import wx  # type: ignore

        app = wx.GetApp() or wx.App(False)
        wx.MessageBox(message, title, wx.OK | wx.ICON_ERROR)
        # If we just created an app, make sure we don't hang around.
        try:
            if wx.GetApp() is app and not wx.GetTopLevelWindows():
                app.ExitMainLoop()
        except Exception:
            pass
    except Exception:
        # Last resort: stdout/stderr
        sys.stderr.write(f"{title}\n{message}\n")


def main() -> int:
    _boot_log("=== plugin start ===")
    _boot_log(f"pid={os.getpid()}")
    try:
        # Always-on crash trace for this external IPC plugin process.
        from library_manager.debug import enable_segfault_trace_always  # type: ignore

        p = enable_segfault_trace_always()
        if p:
            _boot_log(f"fault_handler_log={p!r}")
    except Exception:
        pass
    _boot_log(f"argv={sys.argv!r}")
    _boot_log(f"cwd={os.getcwd()!r}")
    _boot_log(f"exe={sys.executable!r}")
    try:
        _boot_log(f"KICAD_API_SOCKET={os.environ.get('KICAD_API_SOCKET')!r}")
        _boot_log(f"KICAD_API_TOKEN={'set' if os.environ.get('KICAD_API_TOKEN') else 'missing'}")
    except Exception:
        pass

    _ensure_sys_path_for_package()
    _boot_log(f"sys.path[0:3]={sys.path[0:3]!r}")

    # Import late so sys.path fix is active.
    try:
        import wx  # type: ignore
    except Exception:
        _show_error_dialog(
            "KiCad Library Manager",
            "wxPython is not available in this plugin environment.\n\n"
            "KiCad IPC plugins run in an external Python environment; ensure the selected\n"
            "interpreter/virtualenv includes wxPython.",
        )
        return 2

    try:
        import kipy  # type: ignore
    except Exception:
        _show_error_dialog(
            "KiCad Library Manager",
            "Missing dependency: kicad-python (kipy).\n\n"
            "This plugin now uses KiCad's IPC API. Ensure the plugin environment has\n"
            "`kicad-python` installed.",
        )
        return 2

    try:
        from library_manager.config import Config  # type: ignore
        from library_manager.repo import find_repo_root_auto, find_repo_root_from_project, is_repo_root  # type: ignore
        from library_manager.ui.main_window import MainDialog  # type: ignore
    except Exception:
        _boot_log("import failed:\n" + traceback.format_exc())
        _show_error_dialog(
            "KiCad Library Manager",
            "Failed to import plugin modules.\n\n" + traceback.format_exc(),
        )
        return 2

    # Standalone process: we must create the wx App.
    app = wx.App(False)

    # Ensure only one instance runs at a time (per user).
    if not _ensure_single_instance_or_notify(app):
        _boot_log("another instance detected; exiting")
        return 0

    # Only after passing the single-instance check, write PID file.
    _write_pid_file()

    # Resolve project path from the running pcbnew instance via IPC.
    repo_path: str | None = None
    project_dir: str = ""
    try:
        kicad = kipy.KiCad(timeout_ms=4000)
        board = kicad.get_board()
        if board is None:
            wx.MessageBox(
                "No board is open in PCB Editor.\n\nOpen a PCB in pcbnew and run the plugin again.",
                "KiCad Library Manager",
                wx.OK | wx.ICON_WARNING,
            )
            return 1
        project = board.get_project()
        start_path = getattr(project, "path", None) or getattr(board, "name", None) or ""
        try:
            sp = str(start_path or "")
            if sp and os.path.isfile(sp):
                project_dir = os.path.dirname(sp)
            elif sp and os.path.isdir(sp):
                project_dir = sp
        except Exception:
            project_dir = ""
        repo_path = find_repo_root_from_project(str(start_path))
        if not repo_path:
            # Try settings path first.
            try:
                cfg = Config.load()
                if cfg.repo_path and is_repo_root(cfg.repo_path):
                    repo_path = cfg.repo_path
            except Exception:
                pass
        if not repo_path:
            # Best-effort auto discovery using sentinel files (Database/categories.yml, Footprints/, Symbols/).
            try:
                here = os.path.abspath(os.path.dirname(__file__))
            except Exception:
                here = ""
            repo_path = find_repo_root_auto([str(start_path), os.getcwd(), here])
        if repo_path:
            # Persist discovered path for next launches (best-effort).
            try:
                cfg = Config.load()
                if not (cfg.repo_path or "").strip():
                    cfg.repo_path = str(repo_path)
                    cfg.save()
            except Exception:
                pass
        _boot_log(f"project_path={getattr(project, 'path', None)!r} board_name={getattr(board, 'name', None)!r} repo_path={repo_path!r}")
    except Exception:
        _boot_log("IPC connect failed:\n" + traceback.format_exc())
        wx.MessageBox(
            "Could not connect to KiCad via IPC.\n\n"
            "Make sure the IPC API server is enabled in KiCad settings.\n\n"
            f"{traceback.format_exc()}",
            "KiCad Library Manager",
            wx.OK | wx.ICON_ERROR,
        )
        return 1

    if not repo_path:
        # Do NOT abort: users must be able to open Settings and initialize a new library.
        # The main window handles empty/invalid repo paths via "setup mode".
        _boot_log("repo_path not found; opening UI in setup mode")
        repo_path = ""

    frm = MainDialog(None, str(repo_path or ""), project_path=project_dir)
    app.SetTopWindow(frm)
    frm.Show()
    app.MainLoop()
    _boot_log("wx MainLoop exited")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
