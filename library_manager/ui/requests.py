from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass

import wx

from ..config import Config
from ..github_api import GitHubError, GitHubRepo, create_file, get_token
from .local_ci import ensure_skip_ci_message, repo_has_ci_tools, schedule_local_db_ci


@dataclass(frozen=True)
class CommitPromptResult:
    message: str
    process_locally: bool


class _CommitMessageDialog(wx.Dialog):
    def __init__(
        self,
        parent: wx.Window,
        *,
        default: str,
        repo_path: str,
        initial_process_locally: bool,
        can_process_locally: bool,
    ) -> None:
        super().__init__(parent, title="Commit message", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._process_locally = bool(initial_process_locally and can_process_locally)

        msg_lbl = wx.StaticText(self, label="Commit message for this request:")
        self._msg = wx.TextCtrl(self, value=str(default or ""), style=wx.TE_MULTILINE)
        try:
            self._msg.SetMinSize((420, 72))
        except Exception:
            pass

        self._local_cb = wx.CheckBox(
            self,
            label="Process locally (skip GitHub CI)",
        )
        self._local_cb.SetValue(self._process_locally)
        self._local_cb.Enable(bool(can_process_locally))
        hint = (
            "Runs tools/process_requests.py and rebuilds the database locally, then pushes with [skip ci]. "
            "Requires a local git clone with tools/ and push access."
        )
        if not can_process_locally:
            hint = "Unavailable: local library repo is missing tools/process_requests.py (update repo tools in Settings)."
        self._hint = wx.StaticText(self, label=hint)
        try:
            self._hint.Wrap(460)
        except Exception:
            pass

        btns = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)

        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(msg_lbl, 0, wx.ALL, 8)
        root.Add(self._msg, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)
        root.Add(self._local_cb, 0, wx.ALL, 8)
        root.Add(self._hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        root.Add(btns, 0, wx.ALL | wx.EXPAND, 8)
        self.SetSizerAndFit(root)
        try:
            self.SetMinSize((520, 260))
        except Exception:
            pass
        try:
            self._msg.SetFocus()
        except Exception:
            pass

    def get_result(self) -> CommitPromptResult | None:
        msg = (self._msg.GetValue() or "").strip()
        if not msg:
            return None
        return CommitPromptResult(message=msg, process_locally=bool(self._local_cb.GetValue()))


def prompt_commit_message(
    parent: wx.Window,
    *,
    default: str,
    repo_path: str = "",
    allow_local_ci: bool = True,
) -> CommitPromptResult | None:
    """
    Prompt for a commit message.

    When allow_local_ci is True and the repo has CI tools, offers an opt-in checkbox to
    process the request locally instead of relying on GitHub Actions.
    """
    rp = str(repo_path or "").strip()
    can_local = bool(allow_local_ci and rp and repo_has_ci_tools(rp))
    initial_local = False
    try:
        initial_local = bool(Config.load().process_requests_locally)
    except Exception:
        initial_local = False

    dlg = _CommitMessageDialog(
        parent,
        default=str(default or ""),
        repo_path=rp,
        initial_process_locally=initial_local,
        can_process_locally=can_local,
    )
    try:
        if dlg.ShowModal() != wx.ID_OK:
            return None
        result = dlg.get_result()
        if result is None:
            return None
        try:
            cfg = Config.load()
            cfg.process_requests_locally = bool(result.process_locally)
            cfg.save()
        except Exception:
            pass
        return result
    finally:
        dlg.Destroy()


def submit_request(cfg: Config, *, action: str, payload: dict, commit_message: str | None) -> str:
    token = get_token()
    repo = GitHubRepo(
        owner=cfg.github_owner.strip(),
        repo=cfg.github_repo.strip(),
        base_branch=(cfg.github_base_branch.strip() or "main"),
    )

    user = os.environ.get("USER") or os.environ.get("USERNAME") or "user"
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    rnd = random.randint(0, 999999)
    req_name = f"{ts}_{user}_{rnd:06d}.json"
    path = f"Requests/{req_name}"

    body = {
        "schema_version": 1,
        "action": str(action or "").strip(),
        "created_at": ts,
        "created_by": user,
        "source": "kicad_plugin_ui",
    }
    body.update(dict(payload or {}))

    create_file(
        repo,
        token,
        path=path,
        branch=repo.base_branch,
        message=commit_message or f"request: {action}",
        content_text=json.dumps(body, indent=2, sort_keys=True) + "\n",
    )
    return path


def submit_request_with_optional_local_ci(
    parent: wx.Window,
    cfg: Config,
    *,
    repo_path: str,
    action: str,
    payload: dict,
    default: str,
) -> str | None:
    """
    Prompt, submit request to GitHub, and optionally run local DB CI (background).

    Returns the Requests/*.json path, or None if the user cancelled.
    Raises GitHubError / RuntimeError on submission failures.
    """
    result = prompt_commit_message(parent, default=default, repo_path=repo_path)
    if result is None:
        return None

    msg = result.message
    if result.process_locally:
        msg = ensure_skip_ci_message(msg)

    req_path = submit_request(cfg, action=action, payload=payload, commit_message=msg)

    if result.process_locally:
        br = (cfg.github_base_branch or "main").strip() or "main"
        schedule_local_db_ci(parent, repo_path, branch=br)

    return req_path
