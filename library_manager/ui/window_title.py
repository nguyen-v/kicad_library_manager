from __future__ import annotations

import glob
import os

from ..config import Config


def library_display_name(repo_path: str, cfg: Config | None = None) -> str:
    """
    Best-effort human-friendly "library name" for window titles.

    Preference order:
    - Configured DBL filename (without extension)
    - If there is exactly one `Database/*.kicad_dbl`, use its filename (without extension)
    - Repo folder name
    """
    rp = str(repo_path or "").strip()
    if not rp:
        return ""

    try:
        cfg2 = cfg or Config.load_effective(rp)
    except Exception:
        cfg2 = cfg

    try:
        dbl = str(getattr(cfg2, "dbl_filename", "") or "").strip() if cfg2 else ""
    except Exception:
        dbl = ""
    if dbl:
        base = os.path.basename(dbl)
        if base.lower().endswith(".kicad_dbl"):
            base = base[: -len(".kicad_dbl")]
        return base.strip()

    try:
        cands = sorted(glob.glob(os.path.join(rp, "Database", "*.kicad_dbl")))
        if len(cands) == 1:
            base = os.path.basename(cands[0])
            if base.lower().endswith(".kicad_dbl"):
                base = base[: -len(".kicad_dbl")]
            return base.strip()
    except Exception:
        pass

    try:
        return os.path.basename(os.path.abspath(rp)).strip()
    except Exception:
        return os.path.basename(rp).strip()


def with_library_suffix(title: str, repo_path: str, cfg: Config | None = None) -> str:
    """
    Append "— <library>" when we can determine the library name.
    """
    t = str(title or "").strip()
    name = library_display_name(repo_path, cfg=cfg)
    return f"{t} — {name}" if name else t

