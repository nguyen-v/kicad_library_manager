from __future__ import annotations

import json as _json
import glob
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class InitResult:
    created: list[str]
    skipped_existing: list[str]


@dataclass(frozen=True)
class UpdateResult:
    created: list[str]
    updated: list[str]
    skipped_same: list[str]


def _scaffold_root() -> str:
    here = os.path.abspath(os.path.dirname(__file__))
    return os.path.join(here, "scaffold", "db_repo")


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _norm_dbl_filename(name: str) -> str:
    s = os.path.basename(str(name or "").strip())
    if not s:
        s = "library.kicad_dbl"
    if not s.endswith(".kicad_dbl"):
        s = s + ".kicad_dbl"
    return s


def guess_dbl_filename(repo_path: str, preferred: str | None = None) -> str:
    """
    Pick a DBL filename for this repo.

    - If `preferred` is set, use it (normalized) even if repo has none.
    - Else, if repo has exactly one Database/*.kicad_dbl, use that.
    - Else, default to library.kicad_dbl.
    """
    if preferred and str(preferred).strip():
        return _norm_dbl_filename(str(preferred))
    rp = str(repo_path or "").strip()
    if rp:
        try:
            cands = sorted(glob.glob(os.path.join(rp, "Database", "*.kicad_dbl")))
            if len(cands) == 1:
                return os.path.basename(cands[0])
        except Exception:
            pass
    return "library.kicad_dbl"


def compute_init_actions(*, repo_path: str, base_branch: str, dbl_filename: str) -> list[tuple[str, str]]:
    """
    Returns a list of (repo_relative_path, file_text) to create.
    This is *create-missing-only*; caller decides whether to skip existing files.
    """
    rp = os.path.abspath(str(repo_path or "").strip())
    br = (base_branch or "").strip() or "main"
    dbl = _norm_dbl_filename(dbl_filename)

    root = _scaffold_root()
    out: list[tuple[str, str]] = []

    def add_from_template(rel: str, *, replace_branch: bool = False) -> None:
        src = os.path.join(root, rel)
        txt = _read_text(src)
        if replace_branch:
            txt = txt.replace("__BASE_BRANCH__", br)
        out.append((rel, txt))

    # Workflows + tools
    add_from_template(".github/workflows/build_db.yml", replace_branch=True)
    add_from_template(".github/workflows/assign_ipn.yml", replace_branch=False)
    add_from_template("tools/process_requests.py", replace_branch=False)
    add_from_template("tools/assign_ipn.py", replace_branch=False)
    add_from_template("tools/update_dbl.py", replace_branch=False)
    add_from_template("tools/build_sqlite.py", replace_branch=False)

    # Database seed
    out.append((os.path.join("Database", dbl).replace("\\", "/"), _read_text(os.path.join(root, "Database", "template.kicad_dbl"))))
    add_from_template("Database/categories.yml", replace_branch=False)
    # Repo-local settings (portable across machines). Remote URL is user-specific, so leave empty.
    try:
        settings_txt = _json.dumps(
            {
                "version": 1,
                "remote_db_url": "",
                "github_base_branch": br,
                "dbl_filename": dbl,
            },
            indent=2,
            sort_keys=True,
        )
    except Exception:
        settings_txt = ""
    if settings_txt:
        settings_txt = settings_txt + "\n"
    out.append((os.path.join("Database", "kicad_library_manager.json").replace("\\", "/"), settings_txt))

    # Gitkeep markers to keep empty dirs present in the repo
    out.append(("Requests/.gitkeep", ""))
    out.append(("Symbols/.gitkeep", ""))
    out.append(("Footprints/.gitkeep", ""))
    out.append(("Database/category_fields/.gitkeep", ""))

    # Normalize paths
    fixed: list[tuple[str, str]] = []
    for rel, txt in out:
        fixed.append((str(rel).replace("\\", "/"), str(txt)))
    return fixed


def compute_update_actions(*, repo_path: str, base_branch: str) -> list[tuple[str, str]]:
    """
    Return (repo_relative_path, file_text) for scaffold-managed *tooling* files.

    Unlike initialization, this is intended to be safe to apply to existing databases:
    - updates only workflows + tools shipped by this plugin
    - does NOT touch user data files (Database/db-*.csv, categories.yml, parts.sqlite, etc.)
    """
    rp = os.path.abspath(str(repo_path or "").strip())
    br = (base_branch or "").strip() or "main"
    if not rp:
        return []
    root = _scaffold_root()
    out: list[tuple[str, str]] = []

    def add_from_template(rel: str, *, replace_branch: bool = False) -> None:
        src = os.path.join(root, rel)
        txt = _read_text(src)
        if replace_branch:
            txt = txt.replace("__BASE_BRANCH__", br)
        out.append((rel, txt))

    # Only the "scaffold-managed" automation files.
    add_from_template(".github/workflows/build_db.yml", replace_branch=True)
    add_from_template(".github/workflows/assign_ipn.yml", replace_branch=False)
    add_from_template("tools/process_requests.py", replace_branch=False)
    add_from_template("tools/assign_ipn.py", replace_branch=False)
    add_from_template("tools/update_dbl.py", replace_branch=False)
    add_from_template("tools/build_sqlite.py", replace_branch=False)

    return [(str(rel).replace("\\", "/"), str(txt)) for rel, txt in out]


def update_repo_scaffold_tools(*, repo_path: str, base_branch: str) -> UpdateResult:
    """
    Apply scaffold-managed tooling updates into an existing repo.

    Overwrites files only when content differs. Creates missing files.
    """
    rp = os.path.abspath(str(repo_path or "").strip())
    if not rp:
        raise RuntimeError("Missing repo_path")
    actions = compute_update_actions(repo_path=rp, base_branch=base_branch)
    created: list[str] = []
    updated: list[str] = []
    skipped_same: list[str] = []

    for rel, txt in actions:
        ap = os.path.join(rp, rel)
        if os.path.exists(ap):
            try:
                cur = _read_text(ap)
            except Exception:
                cur = None
            if cur == txt:
                skipped_same.append(rel)
                continue
            _write_text(ap, txt)
            updated.append(rel)
        else:
            _write_text(ap, txt)
            created.append(rel)

    return UpdateResult(created=created, updated=updated, skipped_same=skipped_same)


def init_repo_create_missing_only(*, repo_path: str, base_branch: str, dbl_filename: str) -> InitResult:
    """
    Create scaffold files that are missing; never overwrite existing files.
    Returns which paths were created or skipped.
    """
    rp = os.path.abspath(str(repo_path or "").strip())
    if not rp:
        raise RuntimeError("Missing repo_path")

    actions = compute_init_actions(repo_path=rp, base_branch=base_branch, dbl_filename=dbl_filename)
    created: list[str] = []
    skipped: list[str] = []
    for rel, txt in actions:
        abs_path = os.path.join(rp, rel)
        if os.path.exists(abs_path):
            skipped.append(rel)
            continue
        _write_text(abs_path, txt)
        created.append(rel)
    return InitResult(created=created, skipped_existing=skipped)


def ensure_git_clean_and_origin(repo_path: str) -> None:
    """
    Safety checks before initializing:
    - must be a git worktree
    - must have clean status
    - must have an origin remote
    """
    from .ui.git_ops import run_git

    rp = os.path.abspath(str(repo_path or "").strip())
    if not rp:
        raise RuntimeError("Missing repo_path")
    # Validate git repo
    run_git(["git", "-C", rp, "rev-parse", "--is-inside-work-tree"], cwd=rp)
    # Clean worktree (including untracked)
    st = run_git(["git", "-C", rp, "status", "--porcelain"], cwd=rp).strip()
    if st:
        raise RuntimeError("Local changes detected. Please commit/stash/clean before initializing.")
    # Origin exists
    run_git(["git", "-C", rp, "remote", "get-url", "origin"], cwd=rp)


def commit_and_push_init(*, repo_path: str, commit_message: str, base_branch: str, paths: list[str]) -> None:
    """
    Commit and push created scaffold files.
    """
    from .ui.git_ops import run_git

    rp = os.path.abspath(str(repo_path or "").strip())
    br = (base_branch or "").strip() or "main"
    msg = (commit_message or "").strip() or "chore: initialize database repo"
    want_paths = [str(p).replace("\\", "/") for p in (paths or []) if str(p or "").strip()]
    if not want_paths:
        return

    # Ensure we commit/push to the configured base branch.
    # Even if the repo is currently on another branch, initialization should end up on `br`.
    try:
        # Best-effort: update origin/<br> if it exists.
        run_git(["git", "-C", rp, "fetch", "origin", br, "--quiet"], cwd=rp)
    except Exception:
        pass
    try:
        # Prefer tracking origin/<br> when available.
        run_git(["git", "-C", rp, "checkout", "-B", br, f"origin/{br}"], cwd=rp)
    except Exception:
        # Fall back to creating/resetting the branch locally.
        run_git(["git", "-C", rp, "checkout", "-B", br], cwd=rp)

    add_args = ["git", "-C", rp, "add", "-A", "--"]
    add_args.extend(want_paths)
    run_git(add_args, cwd=rp)

    staged = run_git(["git", "-C", rp, "diff", "--cached", "--name-only"], cwd=rp).strip()
    if not staged:
        return

    run_git(["git", "-C", rp, "commit", "-m", msg], cwd=rp)
    run_git(["git", "-C", rp, "push", "-u", "origin", br], cwd=rp)

