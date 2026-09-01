"""
Linux host dependency helpers (previews + ODBC).

KiCad cannot install apt packages without elevation. On Linux we detect missing
tools and offer to run scripts/setup_linux.sh via pkexec (GUI auth) or sudo.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Sequence

import wx

from .._subprocess import SUBPROCESS_NO_WINDOW


def _plugin_root() -> str:
    # library_manager/ui/linux_deps.py -> plugin root
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def setup_linux_script() -> str:
    return os.path.join(_plugin_root(), "scripts", "setup_linux.sh")


def _which(cmd: str) -> str | None:
    try:
        return shutil.which(cmd)
    except Exception:
        return None


def sqlite_odbc_registered() -> bool:
    """True if the SQLite3 ODBC Driver name KiCad DBL expects is registered."""
    odbcinst = _which("odbcinst")
    if not odbcinst:
        return False
    try:
        cp = subprocess.run(
            [odbcinst, "-q", "-d"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            **SUBPROCESS_NO_WINDOW,
        )
        out = cp.stdout or ""
        return "SQLite3 ODBC Driver" in out
    except Exception:
        return False


def missing_linux_host_deps() -> list[str]:
    """
    Human-readable list of missing host packages/capabilities.
    Empty when Linux host looks ready (or when not on Linux).
    """
    if not sys.platform.startswith("linux"):
        return []
    missing: list[str] = []
    if not _which("rsvg-convert"):
        missing.append("librsvg2-bin (symbol/footprint previews)")
    if not sqlite_odbc_registered():
        missing.append("SQLite3 ODBC Driver (KiCad database libraries / DBL)")
    return missing


def _skip_marker_path() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    d = os.path.join(base, "kicad_library_manager")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return os.path.join(d, "linux_deps_prompt_skip")


def clear_linux_deps_prompt_skip() -> None:
    try:
        p = _skip_marker_path()
        if os.path.isfile(p):
            os.remove(p)
    except Exception:
        pass


def _prompt_skipped() -> bool:
    try:
        return os.path.isfile(_skip_marker_path())
    except Exception:
        return False


def _set_prompt_skipped() -> None:
    try:
        with open(_skip_marker_path(), "w", encoding="utf-8") as f:
            f.write("1\n")
    except Exception:
        pass


def _run_setup_elevated(script: str) -> tuple[int, str]:
    """
    Run setup_linux.sh with GUI elevation when possible.
    Returns (returncode, combined_output).
    """
    if not os.path.isfile(script):
        return 1, f"Missing setup script: {script}"

    # Prefer PolicyKit GUI auth inside desktop sessions.
    pkexec = _which("pkexec")
    if pkexec:
        cmd: Sequence[str] = [pkexec, "/bin/bash", script]
    elif _which("sudo"):
        cmd = ["sudo", "/bin/bash", script]
    else:
        return 1, "Neither pkexec nor sudo is available to install system packages."

    try:
        cp = subprocess.run(
            list(cmd),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            **SUBPROCESS_NO_WINDOW,
        )
        return int(cp.returncode), (cp.stdout or "").strip()
    except Exception as exc:
        return 1, str(exc)


def offer_install_linux_host_deps(
    parent: wx.Window | None,
    *,
    force: bool = False,
    missing: list[str] | None = None,
) -> bool:
    """
    If Linux host deps are missing, ask the user and optionally run setup_linux.sh.

    Returns True if deps are OK after this call (already OK, or install succeeded).
    """
    if not sys.platform.startswith("linux"):
        return True

    miss = list(missing) if missing is not None else missing_linux_host_deps()
    if not miss:
        clear_linux_deps_prompt_skip()
        return True
    if (not force) and _prompt_skipped():
        return False

    script = setup_linux_script()
    body = (
        "Some Linux system packages required by KiCad Library Manager are missing:\n\n"
        + "\n".join(f"  • {m}" for m in miss)
        + "\n\nInstall them now? (you may be prompted for your password)\n\n"
        f"This runs:\n  {script}"
    )
    try:
        resp = wx.MessageBox(
            body,
            "Install Linux dependencies",
            wx.YES_NO | wx.YES_DEFAULT | wx.ICON_QUESTION,
            parent=parent,
        )
    except Exception:
        return False

    if resp != wx.YES:
        _set_prompt_skipped()
        return False

    # Show a busy info; install can take a bit.
    try:
        wx.BeginBusyCursor()
    except Exception:
        pass
    try:
        code, out = _run_setup_elevated(script)
    finally:
        try:
            wx.EndBusyCursor()
        except Exception:
            pass

    still = missing_linux_host_deps()
    if code == 0 and not still:
        clear_linux_deps_prompt_skip()
        try:
            wx.MessageBox(
                "Linux dependencies installed successfully.\n\n"
                "Restart previews / reopen dialogs if they were already open.",
                "Install Linux dependencies",
                wx.OK | wx.ICON_INFORMATION,
                parent=parent,
            )
        except Exception:
            pass
        return True

    detail = out[-2000:] if out else "(no output)"
    remain = "\n".join(f"  • {m}" for m in still) if still else "(install reported failure)"
    try:
        wx.MessageBox(
            "Could not finish installing Linux dependencies.\n\n"
            f"Still missing:\n{remain}\n\n"
            f"You can run manually:\n  sudo {script}\n\n"
            f"Details:\n{detail}",
            "Install Linux dependencies",
            wx.OK | wx.ICON_WARNING,
            parent=parent,
        )
    except Exception:
        pass
    return False


def maybe_offer_linux_host_deps_on_startup(parent: wx.Window | None) -> None:
    """Best-effort one-shot prompt after the main window is shown."""
    try:
        if not sys.platform.startswith("linux"):
            return
        if not missing_linux_host_deps():
            return
        offer_install_linux_host_deps(parent, force=False)
    except Exception:
        return
