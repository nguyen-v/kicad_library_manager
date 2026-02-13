from __future__ import annotations

import os
import subprocess
import threading
import time

from ..config import Config


_GIT_LOCK = threading.Lock()


def run_git(args: list[str], cwd: str) -> str:
    """
    Run a git command, serialized by a process-local lock.

    This prevents concurrent `git fetch`/`merge`/etc from racing and producing errors like:
      "cannot lock ref 'refs/remotes/origin/main': is at ... but expected ..."
    """
    cmd = " ".join(args)
    with _GIT_LOCK:
        def _run_once() -> tuple[int, str]:
            cp = subprocess.run(
                args,
                cwd=cwd,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            return (int(cp.returncode), (cp.stdout or "").strip())

        rc, out = _run_once()
        if rc != 0:
            # Transient fetch race: retry once after a short delay.
            try:
                if " fetch " in f" {cmd} " and "cannot lock ref 'refs/remotes/" in out and "expected" in out:
                    time.sleep(0.25)
                    rc2, out2 = _run_once()
                    if rc2 == 0:
                        return out2
                    out = out2 or out
            except Exception:
                pass
            raise RuntimeError(f"{cmd} failed:\n{out}")
        return out


def git_object_exists(repo_path: str, spec: str) -> bool:
    """
    Return True if a git object spec exists (e.g. 'origin/main:Requests/x.json').
    """
    s = str(spec or "").strip()
    if not s:
        return False
    try:
        with _GIT_LOCK:
            cp = subprocess.run(
                ["git", "-C", repo_path, "cat-file", "-e", s],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        return cp.returncode == 0
    except Exception:
        return False


def git_ls_remote_head_sha(repo_path: str, *, remote: str = "origin", branch: str = "main", timeout_s: float = 3.0) -> str:
    """
    Lightweight remote check without GitHub API.

    Returns the SHA for refs/heads/<branch> from <remote> using `git ls-remote`.
    """
    env = dict(os.environ)
    # Ensure we never block on interactive credential prompts.
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        with _GIT_LOCK:
            cp = subprocess.run(
                ["git", "-C", repo_path, "ls-remote", "--heads", remote, branch],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                timeout=max(0.1, float(timeout_s)),
            )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"git ls-remote timed out after {timeout_s}s") from e
    out = (cp.stdout or "").strip()
    if cp.returncode != 0:
        raise RuntimeError(f"git ls-remote failed:\n{out}")
    # Expected: "<sha>\trefs/heads/<branch>"
    if not out:
        raise RuntimeError("ls-remote returned no output")
    first = out.splitlines()[0].strip()
    sha = first.split()[0].strip()
    if not sha or len(sha) < 7:
        raise RuntimeError("ls-remote output parse failed")
    return sha


def git_last_updated_epoch_by_path(repo_path: str, paths: list[str], ref: str | None = None) -> dict[str, int]:
    """
    Legacy-compatible port of ui.py's `_git_last_updated_epoch_by_path`.

    Returns {repo_relative_path: last_commit_epoch_seconds} for the given paths on `ref`.
    Uses one `git log` and stops once all paths are found.
    """
    paths = [p.strip() for p in (paths or []) if (p or "").strip()]
    seen: set[str] = set()
    uniq: list[str] = []
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        uniq.append(p)
    if not uniq:
        return {}

    if not ref:
        try:
            br = (Config.load().github_base_branch or "main").strip() or "main"
        except Exception:
            br = "main"
        ref = f"origin/{br}"
    out = run_git(["git", "log", ref, "--format=%ct", "--name-only", "--no-renames", "--"] + uniq, cwd=repo_path)

    wanted = set(uniq)
    res: dict[str, int] = {}
    cur_ts: int | None = None
    for line in (out or "").splitlines():
        s = (line or "").strip()
        if not s:
            continue
        if s.isdigit():
            try:
                cur_ts = int(s)
            except Exception:
                cur_ts = None
            continue
        if cur_ts is None:
            continue
        if s in wanted and s not in res:
            res[s] = int(cur_ts)
            if len(res) >= len(wanted):
                break
    return res


def git_last_updated_epoch(repo_path: str, path: str, ref: str | None = None) -> int | None:
    """
    Port of ui.py `_git_last_updated_epoch` for a single path.
    """
    p = (path or "").strip()
    if not p:
        return None
    try:
        if not ref:
            try:
                br = (Config.load().github_base_branch or "main").strip() or "main"
            except Exception:
                br = "main"
            ref = f"origin/{br}"
        out = run_git(["git", "-C", repo_path, "log", ref, "-1", "--format=%ct", "--", p], cwd=repo_path)
        s = (out or "").strip().splitlines()[0].strip() if out else ""
        return int(s) if s.isdigit() else None
    except Exception:
        return None


def _git_dir(repo_path: str) -> str:
    """
    Return git dir path as reported by `git rev-parse --git-dir`.

    This is critical for submodules, where `.git` is often a *file* pointing at
    the real git dir.
    """
    return run_git(["git", "-C", repo_path, "rev-parse", "--git-dir"], cwd=repo_path).strip()


def _git_file_path(repo_path: str, git_dir: str, name: str) -> str:
    if os.path.isabs(git_dir):
        return os.path.join(git_dir, name)
    return os.path.join(repo_path, git_dir, name)


def git_fetch_head_age_seconds(repo_path: str) -> int | None:
    try:
        git_dir = _git_dir(repo_path)
        p = _git_file_path(repo_path, git_dir, "FETCH_HEAD")
        if not os.path.isfile(p):
            return None
        return int(max(0.0, time.time() - os.path.getmtime(p)))
    except Exception:
        return None


def git_fetch_head_mtime(repo_path: str) -> float | None:
    """
    Legacy-compatible: return FETCH_HEAD mtime (epoch seconds).
    """
    try:
        git_dir = _git_dir(repo_path)
        p = _git_file_path(repo_path, git_dir, "FETCH_HEAD")
        if not os.path.isfile(p):
            return None
        return float(os.path.getmtime(p))
    except Exception:
        return None


def git_ls_tree_paths(repo_path: str, ref: str, prefixes: list[str]) -> set[str]:
    """
    Return repo-relative file paths present in `<ref>` under the given prefixes.
    """
    prefs = [str(p or "").strip().strip("/").strip("\\") for p in (prefixes or []) if str(p or "").strip()]
    if not prefs:
        return set()
    try:
        out = run_git(["git", "ls-tree", "-r", "--name-only", ref, "--"] + prefs, cwd=repo_path)
    except Exception:
        return set()
    res: set[str] = set()
    for line in (out or "").splitlines():
        p = (line or "").strip()
        if p:
            res.add(p.replace(os.sep, "/"))
    return res


def git_status_entries(repo_path: str) -> list[tuple[str, str]]:
    """
    Return `git status --porcelain` entries as (status, path).

    Uses `-z` to avoid quoting/escaping issues (paths with spaces, '#', etc.).
    """
    # IMPORTANT: do NOT use `run_git()` here because it strips leading whitespace, which
    # corrupts porcelain records (e.g. " M path" -> "M path"). Parse bytes directly.
    with _GIT_LOCK:
        cp = subprocess.run(
            ["git", "-C", repo_path, "status", "--porcelain", "-z"],
            cwd=repo_path,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
        )
    if cp.returncode != 0:
        try:
            out = (cp.stdout or b"").decode("utf-8", errors="replace").strip()
        except Exception:
            out = ""
        raise RuntimeError(f"git status --porcelain -z failed:\n{out}")
    data = cp.stdout or b""
    if not data:
        return []
    parts = data.split(b"\0")
    rows: list[tuple[str, str]] = []
    i = 0
    while i < len(parts):
        rec_b = parts[i]
        i += 1
        if not rec_b:
            continue
        # Format: XY<space>path  (path may be followed by another NUL record for renames)
        st = rec_b[:2].decode("utf-8", errors="replace")
        path_b = rec_b[3:] if len(rec_b) >= 4 else b""
        path = path_b.decode("utf-8", errors="replace").strip()
        if not path:
            continue
        # Renames/copies provide two paths: old then new.
        if st and (st[0] in ("R", "C") or st[1] in ("R", "C")):
            if i < len(parts):
                newp = (parts[i] or b"").decode("utf-8", errors="replace").strip()
                # Only consume the extra path if it looks like a real second path.
                if newp:
                    i += 1
                    path = newp
        rows.append((st, path))
    return rows


def suggest_assets_commit_message(entries: list[tuple[str, str]]) -> str:
    """
    Build a more specific commit message for local asset changes under Symbols/Footprints.
    """
    # kind -> lib -> {added, modified, deleted}
    agg: dict[str, dict[str, dict[str, int]]] = {"Footprints": {}, "Symbols": {}}

    def _classify(st: str) -> str:
        st = (st or "").strip()
        if st == "??":
            return "added"
        if "D" in st:
            return "deleted"
        if "A" in st:
            return "added"
        if "R" in st or "C" in st:
            return "modified"
        if "M" in st:
            return "modified"
        return "modified"

    def _norm_path(p: str) -> str:
        return (p or "").replace("\\", "/").strip()

    def _fp_lib_from_path(p: str) -> str:
        parts = _norm_path(p).split("/")
        pretty = ""
        for seg in parts:
            if seg.endswith(".pretty"):
                pretty = seg
        if pretty.endswith(".pretty"):
            return pretty[:-7]
        return "Footprints"

    def _sym_lib_from_path(p: str) -> str:
        parts = _norm_path(p).split("/")
        fn = parts[-1] if parts else ""
        if fn.endswith(".kicad_sym"):
            return fn[:-10]
        return "Symbols"

    for st, p in entries or []:
        p2 = _norm_path(p)
        if not p2:
            continue
        if p2 == "Footprints" or p2.startswith("Footprints/"):
            lib = _fp_lib_from_path(p2)
            cls = _classify(st)
            agg["Footprints"].setdefault(lib, {"added": 0, "modified": 0, "deleted": 0})[cls] += 1
        elif p2 == "Symbols" or p2.startswith("Symbols/"):
            lib = _sym_lib_from_path(p2)
            cls = _classify(st)
            agg["Symbols"].setdefault(lib, {"added": 0, "modified": 0, "deleted": 0})[cls] += 1

    def _fmt_kind(kind: str) -> str:
        libs = agg.get(kind) or {}
        if not libs:
            return ""
        items = []
        for lib, c in libs.items():
            tot = int(c.get("added", 0) or 0) + int(c.get("modified", 0) or 0) + int(c.get("deleted", 0) or 0)
            items.append((tot, lib, c))
        items.sort(key=lambda t: (-t[0], (t[1] or "").lower()))
        chunks: list[str] = []
        max_libs = 4
        for _tot, lib, c in items[:max_libs]:
            a = int(c.get("added", 0) or 0)
            m = int(c.get("modified", 0) or 0)
            d = int(c.get("deleted", 0) or 0)
            parts2: list[str] = []
            if a:
                parts2.append(f"added {a}")
            if m:
                parts2.append(f"modified {m}")
            if d:
                parts2.append(f"deleted {d}")
            if parts2:
                chunks.append(f"{lib} ({', '.join(parts2)})")
            else:
                chunks.append(f"{lib}")
        if len(items) > max_libs:
            chunks.append(f"+{len(items) - max_libs} more")
        return f"{kind}: " + ", ".join(chunks)

    parts = [p for p in (_fmt_kind("Footprints"), _fmt_kind("Symbols")) if p]
    if not parts:
        return "assets: update symbols/footprints"
    return "assets: " + "; ".join(parts)


def git_commit_and_push_assets(repo_path: str, *, commit_message: str, prefixes: list[str] | None = None, branch: str = "main") -> str:
    """
    Commit+push local changes under Symbols/ and Footprints/ (and optionally other prefixes).
    Returns a human-readable summary. Raises on failure.
    """
    prefixes = prefixes or ["Symbols", "Footprints"]
    entries = git_status_entries(repo_path)
    changed = paths_changed_under(entries, prefixes)
    if not changed:
        return "No local symbol/footprint changes to publish."

    br = (branch or "").strip() or "main"
    # Strategy (important for CI-generated commits and request workflows):
    # - Stage + commit assets locally (working tree is dirty so we cannot merge/switch freely).
    # - Fetch origin/<br>.
    # - Rebase our asset commit(s) onto origin/<br> to avoid "diverged, cannot ff-only".
    # - Push to the configured branch.
    #
    # This keeps history linear without relying on user git config.
    try:
        run_git(["git", "-C", repo_path, "fetch", "origin", br, "--quiet"], cwd=repo_path)
    except Exception:
        # Remote may not exist or may be unreachable; commit still useful locally.
        pass

    add_args = ["git", "-C", repo_path, "add", "-A", "--"]
    add_args.extend(prefixes)
    run_git(add_args, cwd=repo_path)

    staged = ""
    try:
        staged = run_git(["git", "-C", repo_path, "diff", "--cached", "--name-only"], cwd=repo_path).strip()
    except Exception:
        staged = ""
    if not staged:
        return "No staged symbol/footprint changes to commit."

    run_git(["git", "-C", repo_path, "commit", "-m", str(commit_message or "").strip() or "assets: update"], cwd=repo_path)
    # Ensure we are based on latest origin/<br> before pushing.
    try:
        run_git(["git", "-C", repo_path, "fetch", "origin", br, "--quiet"], cwd=repo_path)
    except Exception as exc:
        raise RuntimeError(f"Could not fetch origin/{br} before pushing assets:\n{exc}") from exc
    try:
        # Non-interactive rebase; will raise on conflicts.
        run_git(["git", "-C", repo_path, "rebase", f"origin/{br}"], cwd=repo_path)
    except Exception as exc:
        raise RuntimeError(
            "Assets commit could not be rebased onto the latest remote branch.\n\n"
            f"Fix (manual):\n- cd {repo_path}\n- git fetch origin {br}\n- git rebase origin/{br}\n"
            "- resolve any conflicts, then run Sync again.\n\n"
            f"Error:\n{exc}"
        ) from exc
    # Push our current HEAD to the configured branch explicitly.
    run_git(["git", "-C", repo_path, "push", "-u", "origin", f"HEAD:{br}"], cwd=repo_path)

    short = "\n".join([f"- {p}" for p in changed[:30]])
    if len(changed) > 30:
        short += f"\n- ... ({len(changed) - 30} more)"
    return "Published local symbol/footprint changes:\n" + short


def paths_changed_under(entries: list[tuple[str, str]], prefixes: list[str]) -> list[str]:
    out: list[str] = []
    for _st, p in entries:
        for pref in prefixes:
            if p == pref or p.startswith(pref + "/") or p.startswith(pref + "\\"):
                out.append(p)
                break
    return sorted(set(out))


def git_diff_name_status(repo_path: str, a: str, b: str, paths: list[str]) -> list[tuple[str, str]]:
    """
    Return `git diff --name-status a..b -- <paths>` parsed as (status, path).

    Uses `-z` to avoid quoting/escaping issues in paths with spaces, '#', etc.
    """
    cmd = ["git", "-C", repo_path, "diff", "--name-status", "-z", f"{a}..{b}", "--"] + list(paths or [])
    with _GIT_LOCK:
        cp = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
        )
    if cp.returncode != 0:
        try:
            out = (cp.stdout or b"").decode("utf-8", errors="replace").strip()
        except Exception:
            out = ""
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{out}")

    # With -z, output is NUL-delimited fields:
    #   <status>\0<path>\0
    # and for renames/copies:
    #   <status>\0<old>\0<new>\0
    data = cp.stdout or b""
    toks = data.split(b"\0")
    rows: list[tuple[str, str]] = []
    i = 0
    while i < len(toks):
        st_b = toks[i]
        i += 1
        if not st_b:
            continue
        st = st_b.decode("utf-8", errors="replace").strip()
        if not st:
            continue
        if i >= len(toks):
            break
        p1 = (toks[i] or b"").decode("utf-8", errors="replace").strip()
        i += 1
        if not p1:
            continue
        # For R/C, next token is the "new" path.
        if st.startswith("R") or st.startswith("C"):
            if i < len(toks):
                p2 = (toks[i] or b"").decode("utf-8", errors="replace").strip()
                if p2:
                    i += 1
                    rows.append((st, p2))
                    continue
        rows.append((st, p1))
    return rows


def git_sync_status(repo_path: str) -> dict[str, object]:
    age = git_fetch_head_age_seconds(repo_path)
    stale = (age is None) or (age > 300)
    dirty = bool(git_status_entries(repo_path))
    out: dict[str, object] = {"dirty": dirty, "age": age, "stale": stale}
    if stale:
        out["up_to_date"] = False
        out["behind"] = None
        return out

    try:
        br = (Config.load().github_base_branch or "main").strip() or "main"
    except Exception:
        br = "main"
    counts = run_git(["git", "rev-list", "--left-right", "--count", f"HEAD...origin/{br}"], cwd=repo_path)
    parts = counts.replace("\t", " ").split()
    ahead = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
    behind = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    out["ahead"] = ahead
    out["behind"] = behind
    out["up_to_date"] = (ahead == 0 and behind == 0 and not dirty)
    return out


def git_sync_ff_only(repo_path: str, *, branch: str) -> str:
    """
    Deterministic sync that ignores user git pull.rebase config.

    We intentionally avoid `git pull` because it can invoke rebase depending on user config
    and produce errors like "fatal: Cannot rebase onto multiple branches." even for ff-only
    updates.
    """
    br = (branch or "").strip() or "main"
    # Always fetch first to update origin/<branch>.
    run_git(["git", "-C", repo_path, "fetch", "origin", br, "--quiet"], cwd=repo_path)
    # Ensure we are operating on the configured branch.
    # This avoids ff-only failures when the worktree is on another branch (or detached HEAD),
    # which is common for submodules and can also happen after other tooling.
    try:
        cur = run_git(["git", "-C", repo_path, "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path).strip()
    except Exception:
        cur = ""
    if cur != br:
        try:
            # If branch exists locally, just switch to it.
            run_git(["git", "-C", repo_path, "checkout", br], cwd=repo_path)
        except Exception:
            # Otherwise create/reset it to track origin/<br> if available.
            try:
                run_git(["git", "-C", repo_path, "checkout", "-B", br, f"origin/{br}"], cwd=repo_path)
            except Exception:
                run_git(["git", "-C", repo_path, "checkout", "-B", br], cwd=repo_path)
    # Then fast-forward only to the remote tracking ref.
    out = run_git(["git", "-C", repo_path, "merge", "--ff-only", f"origin/{br}"], cwd=repo_path)
    return out or "Already up to date."


def git_log_last_commits_for_path(
    repo_path: str,
    path: str,
    *,
    n: int = 10,
    ref: str | None = None,
) -> list[dict[str, str]]:
    """
    Return last N commits that touched `path`.

    Output rows: {sha, date, author, subject}
    - `date` is `YYYY-MM-DD HH:MM` (no timezone)
    """
    p = (path or "").strip().replace(os.sep, "/")
    if not p:
        return []
    nn = int(n) if int(n) > 0 else 10

    # Use a delimiter that's unlikely to show up in normal output.
    fmt = "%H%x1f%ad%x1f%an%x1f%s"
    # Use a compact, timezone-free date for UI display.
    base = ["git", "-C", repo_path, "log", f"-n{nn}", "--date=format:%Y-%m-%d %H:%M", f"--pretty=format:{fmt}"]
    if ref:
        cmd = base + [ref, "--", p]
    else:
        cmd = base + ["--", p]
    out = run_git(cmd, cwd=repo_path)
    rows: list[dict[str, str]] = []
    for line in (out or "").splitlines():
        parts = line.split("\x1f")
        if len(parts) >= 4:
            sha, dt, author, subj = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()
            if sha:
                rows.append({"sha": sha, "date": dt, "author": author, "subject": subj})
    return rows


def git_show_commit_for_path(repo_path: str, sha: str, path: str) -> str:
    """
    Return `git show` for a commit, scoped to a path.
    """
    s = (sha or "").strip()
    p = (path or "").strip().replace(os.sep, "/")
    if not s or not p:
        return ""
    return run_git(["git", "-C", repo_path, "show", s, "--", p], cwd=repo_path)
