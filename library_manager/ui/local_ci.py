from __future__ import annotations

import os
import subprocess
import sys
import time

import wx

from .async_ui import WindowTaskRunner, is_window_alive
from .git_ops import _git_env_no_prompt, git_status_entries, run_git


def repo_has_ci_tools(repo_path: str) -> bool:
    rp = os.path.abspath(str(repo_path or "").strip())
    if not rp:
        return False
    return os.path.isfile(os.path.join(rp, "tools", "process_requests.py"))


def ensure_skip_ci_message(message: str) -> str:
    msg = str(message or "").strip()
    if not msg:
        return "[skip ci]"
    if "[skip ci]" in msg.lower():
        return msg
    return f"{msg} [skip ci]"


def _run_python_tool(repo_path: str, script: str) -> None:
    script_path = os.path.join(repo_path, "tools", script)
    if not os.path.isfile(script_path):
        raise RuntimeError(f"Missing tool script: {script_path}")
    cp = subprocess.run(
        [sys.executable, script_path, "--repo", repo_path],
        cwd=repo_path,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        env=_git_env_no_prompt(),
    )
    out = (cp.stdout or "").strip()
    if int(cp.returncode) != 0:
        raise RuntimeError(f"{script} failed (exit {cp.returncode}):\n{out}")


def _repo_is_git(repo_path: str) -> bool:
    try:
        run_git(["git", "-C", repo_path, "rev-parse", "--git-dir"], cwd=repo_path)
        return True
    except Exception:
        return False


def _sync_to_origin(repo_path: str, *, branch: str) -> None:
    br = (branch or "main").strip() or "main"
    dirty = git_status_entries(repo_path)
    if dirty:
        try:
            run_git(["git", "-C", repo_path, "merge", "--ff-only", f"origin/{br}"], cwd=repo_path)
        except Exception as exc:
            lines = "\n".join([f"  {st} {p}" for st, p in dirty[:20]])
            extra = f"\n  ... ({len(dirty) - 20} more)" if len(dirty) > 20 else ""
            raise RuntimeError(
                "Cannot run local DB CI: the local library repo has uncommitted changes.\n"
                "Commit, stash, or discard them first.\n\n"
                f"{lines}{extra}"
            ) from exc
        return

    run_git(["git", "-C", repo_path, "checkout", br], cwd=repo_path)
    run_git(["git", "-C", repo_path, "reset", "--hard", f"origin/{br}"], cwd=repo_path)


def run_local_db_ci(repo_path: str, *, branch: str) -> str:
    """
    Mirror library_manager/scaffold/db_repo/.github/workflows/build_db.yml locally.

    Expects the request commit to already exist on origin (typically with [skip ci]).
    """
    rp = os.path.abspath(str(repo_path or "").strip())
    if not rp:
        raise RuntimeError("Missing local library repo path.")
    if not _repo_is_git(rp):
        raise RuntimeError(f"Not a git repository:\n{rp}")
    if not repo_has_ci_tools(rp):
        raise RuntimeError(
            "Local CI tools not found under tools/.\n"
            "Use Settings → Update repo tools, or re-initialize the library repo."
        )

    br = (branch or "main").strip() or "main"
    last_err: Exception | None = None

    for attempt in (1, 2, 3):
        try:
            # Give GitHub a moment to make the request commit visible (usually instant).
            if attempt == 1:
                time.sleep(0.5)

            run_git(["git", "-C", rp, "fetch", "origin", br, "--quiet"], cwd=rp)
            _sync_to_origin(rp, branch=br)

            _run_python_tool(rp, "process_requests.py")
            _run_python_tool(rp, "assign_ipn.py")
            _run_python_tool(rp, "update_dbl.py")
            _run_python_tool(rp, "build_sqlite.py")

            dirty_after = git_status_entries(rp)
            if not dirty_after:
                return "Local DB CI finished: no database changes were needed."

            run_git(["git", "-C", rp, "add", "-A", "Requests", "Database", "tools"], cwd=rp)
            run_git(
                ["git", "-C", rp, "commit", "-m", "ci: rebuild database [skip ci]"],
                cwd=rp,
            )
            run_git(["git", "-C", rp, "push", "-u", "origin", f"HEAD:{br}"], cwd=rp)
            return "Local DB CI completed and pushed database rebuild."
        except Exception as exc:
            last_err = exc
            if attempt < 3:
                time.sleep(float(attempt * 2))
                continue
            break

    assert last_err is not None
    raise last_err


def _task_runner(parent: wx.Window) -> WindowTaskRunner:
    r = getattr(parent, "_local_ci_tasks", None)
    if r is None:
        r = WindowTaskRunner(parent)
        try:
            parent._local_ci_tasks = r  # type: ignore[attr-defined]
        except Exception:
            pass
    return r


def _refresh_after_local_ci(parent: wx.Window, repo_path: str, *, branch: str) -> None:
    br = (branch or "main").strip() or "main"
    try:
        run_git(["git", "-C", repo_path, "fetch", "origin", br, "--quiet"], cwd=repo_path)
    except Exception:
        pass
    try:
        from .pending import reconcile_pending_against_local_csv, update_pending_states_after_fetch

        update_pending_states_after_fetch(repo_path)
        reconcile_pending_against_local_csv(repo_path)
    except Exception:
        pass
    for meth in (
        "_notify_owner_refresh_best_effort",
        "_refresh_top_status",
        "_reload_category_statuses",
        "_rebuild_list",
        "_reload",
    ):
        try:
            fn = getattr(parent, meth, None)
            if callable(fn):
                fn()
        except Exception:
            pass


def schedule_local_db_ci(
    parent: wx.Window,
    repo_path: str,
    *,
    branch: str,
    title: str = "Local DB CI",
) -> None:
    """
    Run local DB CI in a background thread; show result on the UI thread.
    """

    def work() -> str:
        return run_local_db_ci(repo_path, branch=branch)

    def done(res: str | None, err: Exception | None) -> None:
        if not is_window_alive(parent):
            return
        if err:
            try:
                wx.MessageBox(
                    f"Local DB CI failed:\n\n{err}",
                    title,
                    wx.OK | wx.ICON_ERROR,
                )
            except Exception:
                pass
            return
        try:
            wx.MessageBox(str(res or "Done."), title, wx.OK | wx.ICON_INFORMATION)
        except Exception:
            pass
        try:
            _refresh_after_local_ci(parent, repo_path, branch=branch)
        except Exception:
            pass

    _task_runner(parent).run(work, done)
