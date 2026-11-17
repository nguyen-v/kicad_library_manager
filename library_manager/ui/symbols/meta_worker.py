from __future__ import annotations

import json
import os
import re
import sys
from typing import Any


def _scan_kicad_sym_file_meta(sym_lib_path: str, lib_name: str) -> dict[str, tuple[str, str]]:
    from ...library_formats import symbol_metadata
    return symbol_metadata(sym_lib_path, lib_name)


def main(argv: list[str]) -> int:
    """
    Read jobs from stdin JSON and write results to stdout JSON.

    Input:
      { "libs": [ {"lib": "Nick", "path": "/abs/file.kicad_sym"}, ... ] }

    Output:
      {
        "version": 1,
        "loaded_libs": ["Nick", ...],
        "meta": { "Nick:Sym": ["desc", "ds"], ... },
        "errors": { "Nick": "error text", ... }
      }
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    jobs = payload.get("libs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        jobs = []

    out_meta: dict[str, list[str]] = {}
    loaded: list[str] = []
    errors: dict[str, str] = {}

    for j in jobs:
        try:
            if not isinstance(j, dict):
                continue
            lib = str(j.get("lib") or "").strip()
            p = str(j.get("path") or "").strip()
            if not lib or not p:
                continue
            if not os.path.exists(p):
                errors[lib] = "file not found"
                continue
            mm = _scan_kicad_sym_file_meta(p, lib)
            for k, (d, ds) in mm.items():
                out_meta[str(k)] = [str(d or ""), str(ds or "")]
            loaded.append(lib)
        except Exception as e:  # noqa: BLE001
            try:
                errors[str(j.get("lib") or lib or "?")] = str(e)
            except Exception:
                pass

    resp: dict[str, Any] = {"version": 1, "loaded_libs": sorted(set([x for x in loaded if x])), "meta": out_meta, "errors": errors}
    sys.stdout.write(json.dumps(resp))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

