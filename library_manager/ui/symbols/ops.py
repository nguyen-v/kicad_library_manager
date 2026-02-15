from __future__ import annotations

import glob as _glob
import os
import subprocess
import tempfile
import time

from .libcache import SYMBOL_LIBCACHE, resolve_symbol_lib_path
from ..._subprocess import SUBPROCESS_NO_WINDOW
from ..kicad_env import resolve_kicad_cli


def extract_kicad_symbol_meta(sym_lib_path: str, symbol_name: str) -> tuple[str, str]:
    """
    Best-effort extraction of (Description, Datasheet) properties for a symbol inside a .kicad_sym file.
    """
    try:
        with open(sym_lib_path, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
    except Exception:
        return ("", "")

    import re as _re

    m = _re.search(r'\(symbol\s+"%s"\s' % _re.escape(symbol_name), txt)
    if not m:
        return ("", "")
    window = txt[m.start() : m.start() + 80000]

    def _prop(key: str) -> str:
        mm = _re.search(r'\(property\s+"%s"\s+"([^"]*)"' % _re.escape(key), window)
        return (mm.group(1).strip() if mm else "")

    return (_prop("Description"), _prop("Datasheet"))


def remove_kicad_symbol_from_lib(sym_lib_path: str, symbol_name: str) -> None:
    """
    Best-effort delete a symbol entry from a KiCad `.kicad_sym` file.
    Removes the first `(symbol "<name>" ...)` s-expression block.
    """
    sym_lib_path = os.path.abspath(sym_lib_path or "")
    symbol_name = (symbol_name or "").strip()
    if not sym_lib_path or not os.path.exists(sym_lib_path):
        raise RuntimeError("Symbol library file not found")
    if not symbol_name:
        raise RuntimeError("Bad symbol name")

    try:
        with open(sym_lib_path, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except Exception as e:
        raise RuntimeError(f"Failed to read symbol library: {e}")

    needle = f'(symbol "{symbol_name}"'
    start = txt.find(needle)
    if start < 0:
        raise RuntimeError(f'Symbol "{symbol_name}" not found in library')

    i = start
    depth = 0
    in_str = False
    esc = False
    end = -1
    while i < len(txt):
        ch = txt[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i > start:
                    end = i + 1
                    break
        i += 1
    if end <= start:
        raise RuntimeError("Failed to parse symbol block")

    new_txt = txt[:start] + txt[end:]
    try:
        import re as _re

        new_txt = _re.sub(r"\n{4,}", "\n\n\n", new_txt)
    except Exception:
        pass

    try:
        with open(sym_lib_path, "w", encoding="utf-8") as f:
            f.write(new_txt)
    except Exception as e:
        raise RuntimeError(f"Failed to write symbol library: {e}")


def render_symbol_svg(repo_path: str, sym_ref: str, out_svg_path: str) -> None:
    """
    Render symbol SVG using kicad-cli sym export svg.
    sym_ref format: <Lib>:<SymbolName>
    """
    if ":" not in (sym_ref or ""):
        raise RuntimeError("Bad symbol ref")
    lib, sym = sym_ref.split(":", 1)
    # Component browser can request symbol previews before the global symbol cache
    # is finished indexing. Ensure the cache is started and wait briefly in this
    # background render thread so global libs resolve reliably.
    try:
        SYMBOL_LIBCACHE.ensure_started(repo_path)
    except Exception:
        pass

    lib_path = resolve_symbol_lib_path(repo_path, lib)
    if (not lib_path) or (not os.path.exists(lib_path)):
        # Wait up to ~2s for the cache to finish (best effort).
        t0 = time.monotonic()
        while (time.monotonic() - t0) < 2.0:
            try:
                st = SYMBOL_LIBCACHE.snapshot(repo_path)
                if bool(st.get("loaded")) and not bool(st.get("loading")):
                    break
            except Exception:
                break
            time.sleep(0.08)
            lib_path = resolve_symbol_lib_path(repo_path, lib)
            if lib_path and os.path.exists(lib_path):
                break
    if not lib_path or not os.path.exists(lib_path):
        raise RuntimeError(f"Symbol library not found for '{lib}'")

    out_dir = os.path.dirname(out_svg_path)
    os.makedirs(out_dir, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix="sym_", dir=tempfile.gettempdir())
    try:
        exe = resolve_kicad_cli()
        cp = subprocess.run(
            [exe, "sym", "export", "svg", "-o", tmp_dir, "-s", sym, lib_path],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            **SUBPROCESS_NO_WINDOW,
        )
        if cp.returncode != 0:
            raise RuntimeError((cp.stdout or "").strip() or "kicad-cli sym export failed")
        svgs = sorted(_glob.glob(os.path.join(tmp_dir, "*.svg")))
        if not svgs:
            raise RuntimeError("No SVG produced for symbol")
        os.replace(svgs[0], out_svg_path)
    finally:
        try:
            import shutil as _shutil

            _shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

