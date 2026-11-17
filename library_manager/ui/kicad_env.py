from __future__ import annotations

import glob as _glob
import os
import json
import re
from pathlib import Path
import shutil as _shutil
import subprocess
import sys
import threading

from .._subprocess import SUBPROCESS_NO_WINDOW


_KICAD_ENV_LOCK = threading.Lock()
_KICAD_ENV_VARS: dict[str, str] | None = None
_KICAD_CLI_PATH: str | None = None


_KICAD_MAJOR: int | None = None
_PROJECT_DIR: str | None = None


def runtime_major() -> int:
    return _KICAD_MAJOR or 9


def configure_runtime(major: int, project_dir: str = "") -> None:
    """Bind filesystem/CLI discovery to the editor that launched this process."""
    if major not in (9, 10):
        raise RuntimeError(f"KiCad {major} is not supported; use KiCad 9 or 10.")
    global _KICAD_MAJOR, _PROJECT_DIR, _KICAD_CLI_PATH, _KICAD_ENV_VARS
    _KICAD_MAJOR = major
    _PROJECT_DIR = project_dir or None
    _KICAD_CLI_PATH = None
    _KICAD_ENV_VARS = None


def _cli_major(path: str) -> int | None:
    try:
        cp = subprocess.run([path, "version"], capture_output=True, text=True,
                            timeout=8, **SUBPROCESS_NO_WINDOW)
        match = re.search(r"(?<![0-9])(\d+)\.\d+", cp.stdout)
        return int(match[1]) if cp.returncode == 0 and match else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def resolve_kicad_cli() -> str:
    """Select the launching editor's major; reject a mismatched explicit override."""
    global _KICAD_CLI_PATH
    if _KICAD_CLI_PATH:
        return _KICAD_CLI_PATH
    override = os.environ.get("KICAD_CLI", "").strip()
    candidates = [override] if override else []
    if not override:
        name = "kicad-cli.exe" if sys.platform == "win32" else "kicad-cli"
        appdir = os.environ.get("APPDIR", "")
        if appdir:
            candidates.append(str(Path(appdir) / "bin" / name))
        candidates.append(str(Path(sys.executable).parent / name))
        candidates.append(_shutil.which(name) or "")
        if sys.platform == "win32":
            for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                         os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
                candidates.extend(_glob.glob(os.path.join(base, "KiCad", "*", "bin", name)))
        elif sys.platform == "darwin":
            for base in ("/Applications", os.path.expanduser("~/Applications")):
                candidates.extend(_glob.glob(base + "/KiCad*/KiCad.app/Contents/MacOS/kicad-cli"))
                candidates.extend(_glob.glob(base + "/KiCad*.app/Contents/MacOS/kicad-cli"))
    for path in dict.fromkeys(candidates):
        if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
            continue
        major = _cli_major(path)
        if major not in (9, 10) or (_KICAD_MAJOR and major != _KICAD_MAJOR):
            continue
        _KICAD_CLI_PATH = path
        return path
    expected = f" {_KICAD_MAJOR}" if _KICAD_MAJOR else " 9 or 10"
    raise RuntimeError(f"A matching KiCad{expected} kicad-cli was not found. "
                       "Set KICAD_CLI to that installation's executable.")


def kicad_cli_env_vars() -> dict[str, str]:
    """Read Configure Paths settings and installation defaults (CLI has no env command)."""
    major = _KICAD_MAJOR or _cli_major(resolve_kicad_cli())
    result: dict[str, str] = {}
    config = kicad_config_root()
    if config:
        directory = kicad_version_dir(config, f"{major}.0")
        if directory:
            try:
                data = json.loads((Path(directory) / "kicad_common.json").read_text())
                variables = (data.get("environment") or {}).get("vars") or {}
                result.update({str(k): str(v) for k, v in variables.items()})
            except (OSError, ValueError):
                pass
    # KICAD_STOCK_DATA_HOME is supplied by the official AppImage. Native installs
    # put the same tree at <prefix>/share/kicad (or SharedSupport on macOS).
    roots = [Path(os.environ["KICAD_STOCK_DATA_HOME"])] if os.environ.get("KICAD_STOCK_DATA_HOME") else []
    exe = Path(resolve_kicad_cli()).resolve()
    roots.extend([exe.parent.parent / "share/kicad", exe.parent.parent / "SharedSupport"])
    for root in roots:
        if (root / 'symbols').is_dir():
            for suffix, subdir in (("SYMBOL_DIR", "symbols"), ("FOOTPRINT_DIR", "footprints"),
                                   ("3DMODEL_DIR", "3dmodels"), ("TEMPLATE_DIR", "template")):
                result.setdefault(f"KICAD{major}_{suffix}", str(root / subdir))
            break
    # Explicit environment overrides Configure Paths, as it does in KiCad.
    result.update(os.environ)
    return result


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
        for key in ("KICAD_CONFIG_HOME", f"KICAD{_KICAD_MAJOR or 9}_CONFIG_HOME"):
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


def kicad_version_dir(config_root: str, preferred: str | None = None) -> str | None:
    """Use the launching major; standalone fallback sorts numeric version tuples."""
    preferred = preferred or (f"{_KICAD_MAJOR}.0" if _KICAD_MAJOR else None)
    root = Path(config_root)
    if preferred:
        selected = root / preferred
        return str(selected) if selected.is_dir() else None
    try:
        versions = [p for p in root.iterdir() if p.is_dir() and re.fullmatch(r"\d+\.\d+", p.name)]
        return str(max(versions, key=lambda p: tuple(map(int, p.name.split('.'))))) if versions else None
    except OSError:
        return None


def project_root_from_repo(repo_path: str) -> str:
    """
    Compute KIPRJMOD-ish path based on the submodule layout (ui.py behavior).
    """
    if _PROJECT_DIR:
        return _PROJECT_DIR
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
    vars_map["KIPRJMOD"] = project_root_from_repo(repo_path)
    try:
        import re as _re

        def _sub(m):
            k = m.group(1)
            # KiCad remaps older built-in path variables when they have no
            # explicit value (for example a 9 footprint URI in a 10 project).
            old = _re.fullmatch(r'KICAD(\d+)_(SYMBOL_DIR|FOOTPRINT_DIR|3DMODEL_DIR|TEMPLATE_DIR)', k)
            if k not in vars_map and old and _KICAD_MAJOR and int(old[1]) < _KICAD_MAJOR:
                return vars_map.get(f'KICAD{_KICAD_MAJOR}_{old[2]}', m.group(0))
            return vars_map.get(k, m.group(0))

        out = _re.sub(r"\$\{([^}]+)\}", _sub, out)
    except Exception:
        pass
    out = os.path.expanduser(out)
    out = os.path.expandvars(out)
    return out

