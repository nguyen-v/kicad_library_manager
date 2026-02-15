from __future__ import annotations

import glob as _glob
import os
import shutil as _shutil
import subprocess
import sys
import threading

from .._subprocess import SUBPROCESS_NO_WINDOW


_KICAD_ENV_LOCK = threading.Lock()
_KICAD_ENV_VARS: dict[str, str] | None = None
_KICAD_CLI_PATH: str | None = None


def resolve_kicad_cli() -> str:
    """
    Return an executable path for `kicad-cli`.

    On macOS, KiCad is commonly installed as an app bundle and `kicad-cli` is not on PATH
    inside KiCad's IPC plugin environment. This resolver searches common locations.

    Override:
      - set env var `KICAD_CLI` to the full path of the kicad-cli executable.
    """
    global _KICAD_CLI_PATH
    if _KICAD_CLI_PATH:
        return str(_KICAD_CLI_PATH)

    # Explicit override.
    try:
        override = str(os.environ.get("KICAD_CLI") or "").strip()
    except Exception:
        override = ""
    if override:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            _KICAD_CLI_PATH = override
            return override

    # PATH lookup.
    exe_name = "kicad-cli.exe" if sys.platform == "win32" else "kicad-cli"
    p = _shutil.which(exe_name) or _shutil.which("kicad-cli")
    if p:
        _KICAD_CLI_PATH = p
        return p

    # macOS app bundle locations (KiCad 7/8/9).
    if sys.platform == "darwin":
        cands: list[str] = []
        # System-wide app install(s)
        cands.extend(sorted(_glob.glob("/Applications/KiCad*/KiCad.app/Contents/MacOS/kicad-cli")))
        cands.extend(sorted(_glob.glob("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")))
        # User-local Applications
        home = os.path.expanduser("~")
        cands.extend(sorted(_glob.glob(os.path.join(home, "Applications", "KiCad*.app", "Contents", "MacOS", "kicad-cli"))))
        cands.extend(sorted(_glob.glob(os.path.join(home, "Applications", "KiCad.app", "Contents", "MacOS", "kicad-cli"))))
        for cand in cands:
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                _KICAD_CLI_PATH = cand
                return cand

    raise RuntimeError(
        "kicad-cli was not found.\n\n"
        "Fix:\n"
        "- Ensure KiCad is installed, and that `kicad-cli` is available, or\n"
        "- Set environment variable KICAD_CLI to the full path of the kicad-cli executable.\n\n"
        "On macOS, a common path is:\n"
        "  /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
    )


def kicad_cli_env_vars() -> dict[str, str]:
    """
    Best-effort read KiCad environment variables via kicad-cli.
    Matches ui.py behavior (tries `kicad-cli env` and `kicad-cli env vars`).
    """
    exe = None
    try:
        exe = resolve_kicad_cli()
    except Exception:
        exe = None
    if not exe:
        return {}
    cmds = [[exe, "env"], [exe, "env", "vars"]]
    for cmd in cmds:
        try:
            cp = subprocess.run(
                cmd,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                **SUBPROCESS_NO_WINDOW,
            )
        except Exception:
            continue
        if cp.returncode != 0:
            continue
        txt = cp.stdout or ""
        out: dict[str, str] = {}
        for line in txt.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k = ""
            v = ""
            if "=" in line:
                k, v = line.split("=", 1)
            elif ":" in line:
                k, v = line.split(":", 1)
            else:
                parts = line.split(None, 1)
                if len(parts) == 2:
                    k, v = parts[0], parts[1]
                else:
                    continue
            k = (k or "").strip().strip('"')
            v = (v or "").strip().strip('"')
            if v.startswith("="):
                v = v.lstrip("=").strip()
            if not k:
                continue
            out[k] = v
        if out:
            return out
    return {}


def prime_kicad_env_vars() -> dict[str, str]:
    """
    Cache KiCad env vars once per process.
    Safe to call from background threads.
    """
    global _KICAD_ENV_VARS
    with _KICAD_ENV_LOCK:
        if _KICAD_ENV_VARS is not None:
            return dict(_KICAD_ENV_VARS)
        try:
            _KICAD_ENV_VARS = kicad_cli_env_vars()
        except Exception:
            _KICAD_ENV_VARS = {}
        return dict(_KICAD_ENV_VARS)


def kicad_config_root() -> str | None:
    """
    Best-effort locate KiCad's user config root (contains versions like 9.0/).
    Ported from ui.py.
    """
    try:
        for key in ("KICAD_CONFIG_HOME", "KICAD9_CONFIG_HOME"):
            v = (os.environ.get(key) or "").strip()
            if v and os.path.isdir(v):
                return v
    except Exception:
        pass

    home = os.path.expanduser("~")
    appdata = (os.environ.get("APPDATA") or "").strip()
    if appdata:
        cand = os.path.join(appdata, "kicad")
        if os.path.isdir(cand):
            return cand

    cand = os.path.join(home, "Library", "Preferences", "kicad")
    if os.path.isdir(cand):
        return cand

    xdg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
    if xdg:
        cand = os.path.join(xdg, "kicad")
        if os.path.isdir(cand):
            return cand
    cand = os.path.join(home, ".config", "kicad")
    if os.path.isdir(cand):
        return cand

    return None


def kicad_version_dir(config_root: str, preferred: str = "9.0") -> str | None:
    """
    Pick the KiCad version subdir (prefer 9.0), ported from ui.py.
    """
    try:
        pref = os.path.join(config_root, preferred)
        if os.path.isdir(pref):
            return pref
        vers: list[str] = []
        for n in os.listdir(config_root):
            p = os.path.join(config_root, n)
            if not os.path.isdir(p):
                continue
            if "." in n and all(part.isdigit() for part in n.split(".") if part):
                vers.append(n)
        if vers:
            vers.sort()
            return os.path.join(config_root, vers[-1])
    except Exception:
        return None
    return None


def project_root_from_repo(repo_path: str) -> str:
    """
    Compute KIPRJMOD-ish path based on the submodule layout (ui.py behavior).
    """
    rp = os.path.abspath(repo_path or "")
    base = os.path.basename(rp)
    # Common layout: <project>/Libraries/<repo_root>
    try:
        parent = os.path.dirname(rp)
        if os.path.basename(parent) == "Libraries":
            return os.path.dirname(parent)
        # Some users may have nested: <project>/Libraries/<something>/<repo_root>
        grand = os.path.dirname(parent)
        if os.path.basename(grand) == "Libraries":
            return os.path.dirname(grand)
    except Exception:
        pass
    return os.path.dirname(rp)


def expand_kicad_uri(uri: str, repo_path: str) -> str:
    """
    Expand ${VARS} commonly used in KiCad lib tables.
    Ported from ui.py.
    """
    if not uri:
        return ""
    out = uri.strip().strip('"').strip()
    vars_map = dict(os.environ)
    try:
        vars_map.update(prime_kicad_env_vars())
    except Exception:
        pass
    vars_map.setdefault("KIPRJMOD", project_root_from_repo(repo_path))
    try:
        import re as _re

        def _sub(m):
            k = m.group(1)
            return vars_map.get(k, m.group(0))

        out = _re.sub(r"\$\{([^}]+)\}", _sub, out)
    except Exception:
        pass
    out = os.path.expanduser(out)
    out = os.path.expandvars(out)
    return out

