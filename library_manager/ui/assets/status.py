from __future__ import annotations

from dataclasses import dataclass

import os

from ..git_ops import fetch_stale_threshold_seconds, git_diff_name_status, git_fetch_head_age_seconds, git_ls_tree_paths, git_status_entries, paths_changed_under


@dataclass(frozen=True)
class LocalSummary:
    count: int
    files: list[str]
    msg: str


@dataclass(frozen=True)
class RemoteSummary:
    files: list[tuple[str, str]]
    msg: str


def local_asset_paths(repo_path: str, prefixes: list[str]) -> set[str]:
    """
    Local "asset changed" paths under prefixes.

    Primary source is `git status --porcelain` (uncommitted changes).
    Additionally, include files that exist on disk under prefixes but are not present in HEAD
    (covers gitignored/untracked asset files, which users still consider "local assets").
    """
    prefs = [str(p or "").strip().strip("/").strip("\\") for p in (prefixes or []) if str(p or "").strip()]
    if not prefs:
        return set()

    try:
        entries = git_status_entries(repo_path)
        local_paths = set(paths_changed_under(entries, prefs))
    except Exception:
        local_paths = set()

    # Include new files not in HEAD.
    try:
        head_files = git_ls_tree_paths(repo_path, "HEAD", prefs)
    except Exception:
        head_files = set()

    def _ext_ok(rel: str) -> bool:
        # Keep it tight to avoid counting random files: only actual KiCad asset formats.
        r = (rel or "").lower()
        return r.endswith(".kicad_mod") or r.endswith(".kicad_sym")

    for pref in prefs:
        root = os.path.join(repo_path, pref)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                ap = os.path.join(dirpath, fn)
                try:
                    rel = os.path.relpath(ap, repo_path).replace(os.sep, "/")
                except Exception:
                    continue
                if not _ext_ok(rel):
                    continue
                if rel not in head_files:
                    local_paths.add(rel)

    return local_paths


def asset_change_sets(repo_path: str) -> tuple[set[str], set[str], bool]:
    """
    Returns (local_paths, remote_paths, remote_known).

    - local_paths: repo-relative paths changed in working tree under Symbols/Footprints
    - remote_paths: repo-relative paths that differ between HEAD and origin/<branch> under Symbols/Footprints
    - remote_known: False when FETCH_HEAD is stale/unknown
    """
    local_paths = local_asset_paths(repo_path, ["Symbols", "Footprints"])

    age = git_fetch_head_age_seconds(repo_path)
    stale = (age is None) or (age > fetch_stale_threshold_seconds(repo_path))
    if stale:
        return (local_paths, set(), False)

    try:
        from ..config import Config

        br = (Config.load_effective(repo_path).github_base_branch or "main").strip() or "main"
        remote = git_diff_name_status(repo_path, "HEAD", f"origin/{br}", ["Symbols", "Footprints"])
        remote_paths = {p for _st, p in remote if p}
    except Exception:
        remote_paths = set()
    return (local_paths, remote_paths, True)


def local_summary_scoped(repo_path: str, prefixes: list[str], label: str) -> LocalSummary:
    """
    Port of ui.py `_assets_local_summary_scoped`.
    """
    try:
        entries = git_status_entries(repo_path)
    except Exception:
        entries = []

    assets_set = local_asset_paths(repo_path, prefixes)
    assets = sorted(assets_set)
    if not assets:
        return LocalSummary(count=0, files=[], msg=f"Local {label} (uncommitted): none")

    added = modified = deleted = 0
    aset = set(assets)
    by_path: dict[str, str] = {}
    for st, p in list(entries or []):
        if p:
            by_path[str(p)] = str(st or "")

    for p in aset:
        st = by_path.get(p, "")
        if st == "??" or not st:
            # Not in git status but present on disk and not in HEAD -> treat as added local asset.
            added += 1
            continue
        s = (st or "").strip()
        if "D" in s:
            deleted += 1
        elif "A" in s:
            added += 1
        elif "R" in s:
            modified += 1
        elif "M" in s:
            modified += 1
        else:
            modified += 1

    return LocalSummary(
        count=len(assets),
        files=assets,
        msg=f"Local {label} (uncommitted): added {added}, modified {modified}, deleted {deleted}",
    )


def remote_summary_scoped(repo_path: str, prefixes: list[str], label: str) -> RemoteSummary:
    """
    Port of ui.py `_assets_remote_summary_scoped`.
    Requires a fresh FETCH_HEAD (caller should gate on staleness).
    """
    try:
        from ..config import Config

        br = (Config.load_effective(repo_path).github_base_branch or "main").strip() or "main"
        ref = f"origin/{br}"
    except Exception:
        br = "main"
        ref = "origin/main"
    files = git_diff_name_status(repo_path, "HEAD", ref, prefixes)
    a = sum(1 for st, _p in files if (st or "").startswith("A"))
    d = sum(1 for st, _p in files if (st or "").startswith("D"))
    m = len(files) - a - d
    msg = f"Remote {label} (origin/{br}): none" if not files else f"Remote {label} (origin/{br}): added {a}, modified {m}, deleted {d}"
    return RemoteSummary(files=files, msg=msg)

