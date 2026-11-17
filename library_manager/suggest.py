from __future__ import annotations

import glob
import os
import re


def list_footprints(repo_path: str) -> list[str]:
    """
    Return footprints in KiCad reference form: <Library>:<Footprint>
    where <Library> is the pretty folder name without .pretty.
    """
    root = os.path.join(repo_path, "Footprints")
    pattern = os.path.join(root, "**", "*.pretty", "*.kicad_mod")
    out: list[str] = []
    for p in glob.glob(pattern, recursive=True):
        pretty_dir = os.path.basename(os.path.dirname(p))  # e.g. "MyLib.pretty"
        lib = pretty_dir[:-7] if pretty_dir.endswith(".pretty") else pretty_dir
        fp = os.path.splitext(os.path.basename(p))[0]
        out.append(f"{lib}:{fp}")
    return sorted(set(out))


_SYMBOL_RE = re.compile(r'\(symbol\s+"([^"]+)"')
_UNIT_VARIANT_RE = re.compile(r".*_\d+_\d+$")


def list_symbols(repo_path: str) -> list[str]:
    """Return refs from packed and unpacked company symbol libraries."""
    from pathlib import Path
    from .library_formats import symbol_names
    root = Path(repo_path) / "Symbols"
    libraries = list(root.glob("*.kicad_sym")) + list(root.glob("*.kicad_symdir"))
    return sorted({ref for library in libraries for ref in symbol_names(library, library.stem)})


def group_density_variants(footprints: list[str]) -> dict[str, list[str]]:
    """
    Group footprint variants by a "base name".

    Current behavior:
    - Primary rule: if a footprint name ends with L/M/N, treat the last character as a
      density variant token and group by the base name (name without that last char).
      Example: RESC160X080X055L025N -> base RESC160X080X055L025.

    - Extended rule (best-effort, to support other suffixes like MANF):
      If a base is "proven" (i.e. we saw at least one L/M/N variant for it),
      then we also group any other footprints in the same library that match:
        <base><TOKEN>
        <base>_<TOKEN>
        <base>-<TOKEN>
      where TOKEN is alphabetic (A–Z/a–z) with length 2..12.

      This avoids ambiguity: we do NOT try to infer base names for arbitrary footprints
      unless L/M/N variants establish the base first.
    """
    import re as _re

    # Index by library.
    by_lib: dict[str, list[str]] = {}
    for ref in footprints:
        try:
            lib, fp = ref.split(":", 1)
        except ValueError:
            continue
        by_lib.setdefault(lib, []).append(fp)

    # Pass 1: find "proven" bases via L/M/N suffix.
    proven_by_lib: dict[str, set[str]] = {}
    for lib, fps in by_lib.items():
        for fp in fps:
            if fp and fp[-1] in ("L", "M", "N") and len(fp) > 1:
                proven_by_lib.setdefault(lib, set()).add(fp[:-1])

    # Pass 2: build groups, assigning "unknown token" variants to a proven base
    # instead of creating a separate base entry.
    groups: dict[str, set[str]] = {}
    order = {"N": 0, "L": 1, "M": 2}
    token_re = _re.compile(r"^[A-Za-z]{2,12}$")

    for lib, fps in by_lib.items():
        proven_list = sorted(proven_by_lib.get(lib, set()), key=len, reverse=True)

        def _match_proven_base(fp: str) -> str | None:
            for base in proven_list:
                if not fp.startswith(base) or fp == base:
                    continue
                tail = fp[len(base) :]
                if not tail:
                    continue
                tok = tail[1:] if tail[0] in ("_", "-") else tail
                if tok and token_re.match(tok):
                    return base
            return None

        for fp in fps:
            base = ""
            if fp and fp[-1] in ("L", "M", "N") and len(fp) > 1:
                base = fp[:-1]
            else:
                base = _match_proven_base(fp) or fp
            key = f"{lib}:{base}"
            groups.setdefault(key, set()).add(f"{lib}:{fp}")

    def sort_key(full_ref: str) -> tuple[int, int, str]:
        try:
            _lib, _fp = full_ref.split(":", 1)
        except ValueError:
            return (99, 99, full_ref)
        # Prefer N/L/M first (N, then L, then M), then base, then other tokens alphabetically.
        if _fp and _fp[-1] in order:
            return (0, order.get(_fp[-1], 50), _fp)
        return (1, 50, _fp)

    out: dict[str, list[str]] = {}
    for k, refs in groups.items():
        out[k] = sorted(refs, key=sort_key)
    return out

