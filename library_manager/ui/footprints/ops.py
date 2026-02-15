from __future__ import annotations

import os
import tempfile
import time

from .libcache import FP_LIBCACHE, resolve_footprint_pretty_dir as _resolve_footprint_pretty_dir_cached
from ..._subprocess import SUBPROCESS_NO_WINDOW
from ..kicad_env import resolve_kicad_cli


def find_pretty_dir_repo_local(repo_path: str, lib: str) -> str | None:
    lib = (lib or "").strip()
    if not lib:
        return None
    if lib.lower().endswith(".pretty"):
        lib = lib[: -len(".pretty")]
    root = os.path.join(repo_path, "Footprints")
    direct = os.path.join(root, f"{lib}.pretty")
    if os.path.isdir(direct):
        return direct
    try:
        for dirpath, dirnames, _filenames in os.walk(root):
            for dn in list(dirnames):
                if not dn.lower().endswith(".pretty"):
                    continue
                base = dn[: -len(".pretty")].strip()
                if base.lower() == lib.lower():
                    return os.path.join(dirpath, dn)
            dirnames[:] = [d for d in dirnames if not d.lower().endswith(".pretty")]
    except Exception:
        return None
    return None


def resolve_footprint_pretty_dir(repo_path: str, lib: str) -> str | None:
    p = find_pretty_dir_repo_local(repo_path, lib)
    if p:
        return p
    # Global/project libs are provided by FP_LIBCACHE (parsed from fp-lib-table).
    return _resolve_footprint_pretty_dir_cached(repo_path, lib)


def find_footprint_mod_any(repo_path: str, lib: str, fpname: str) -> str | None:
    pretty = resolve_footprint_pretty_dir(repo_path, lib)
    if not pretty:
        return None
    p = os.path.join(pretty, f"{fpname}.kicad_mod")
    return p if os.path.exists(p) else None


def extract_kicad_footprint_descr(mod_path: str) -> str:
    try:
        with open(mod_path, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read(8000)
    except Exception:
        return ""
    import re as _re

    m = _re.search(r'\(descr\s+"([^"]+)"\)', txt)
    if m:
        return m.group(1).strip()
    m = _re.search(r'\(tags\s+"([^"]+)"\)', txt)
    if m:
        return m.group(1).strip()
    return ""


def render_footprint_svg(repo_path: str, fp_ref: str, out_svg_path: str) -> None:
    """
    Render footprint SVG using kicad-cli fp export svg (ported from ui.py).
    """
    import glob as _glob
    import subprocess
    import shutil as _shutil

    if ":" not in (fp_ref or ""):
        raise RuntimeError("Bad footprint ref")
    lib, fp = fp_ref.split(":", 1)

    # Dedicated footprint browser normally starts FP_LIBCACHE, but previews can be requested
    # before indexing completes. Ensure cache is started and wait briefly in this background
    # render thread so global libs resolve reliably.
    try:
        FP_LIBCACHE.ensure_started(repo_path)
    except Exception:
        pass

    pretty = resolve_footprint_pretty_dir(repo_path, lib)
    if (not pretty) or (not os.path.isdir(pretty)):
        t0 = time.monotonic()
        while (time.monotonic() - t0) < 2.0:
            try:
                st = FP_LIBCACHE.snapshot(repo_path)
                if bool(st.get("loaded")) and not bool(st.get("loading")):
                    break
            except Exception:
                break
            time.sleep(0.08)
            pretty = resolve_footprint_pretty_dir(repo_path, lib)
            if pretty and os.path.isdir(pretty):
                break
    if not pretty:
        raise RuntimeError(f"Footprint library not found for '{lib}'")
    out_dir = os.path.dirname(out_svg_path)
    os.makedirs(out_dir, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix="fp_", dir=tempfile.gettempdir())
    try:
        exe = resolve_kicad_cli()
        layers = "F.Cu,F.Mask,F.SilkS,F.Fab,F.CrtYd"
        cp = subprocess.run(
            [exe, "fp", "export", "svg", "-o", tmp_dir, "--fp", fp, "--layers", layers, pretty],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            **SUBPROCESS_NO_WINDOW,
        )
        if cp.returncode != 0:
            cp2 = subprocess.run(
                [exe, "fp", "export", "svg", "-o", tmp_dir, "--fp", fp, "--layers", "F.Fab", pretty],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                **SUBPROCESS_NO_WINDOW,
            )
            if cp2.returncode != 0:
                cp3 = subprocess.run(
                    [exe, "fp", "export", "svg", "-o", tmp_dir, "--fp", fp, "--layers", "F.Cu", pretty],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    encoding="utf-8",
                    errors="replace",
                    **SUBPROCESS_NO_WINDOW,
                )
                if cp3.returncode != 0:
                    raise RuntimeError(
                        (cp.stdout or "").strip()
                        or (cp2.stdout or "").strip()
                        or (cp3.stdout or "").strip()
                        or "kicad-cli fp export failed"
                    )
        svgs = sorted(_glob.glob(os.path.join(tmp_dir, "*.svg")))
        if not svgs:
            raise RuntimeError("No SVG produced for footprint")
        os.replace(svgs[0], out_svg_path)
    finally:
        try:
            _shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

