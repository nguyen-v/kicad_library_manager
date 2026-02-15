from __future__ import annotations

import json
import os
from dataclasses import dataclass


def _default_config_path() -> str:
    # Prefer XDG config dir on Linux; otherwise fall back to home.
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        base = xdg
    else:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "kicad_library_manager", "config.json")


def _repo_settings_relpath() -> str:
    """
    Repo-local settings file path (relative to repo root).

    This file is intended to live *inside* the database repo so it is portable across:
    - computers
    - operating systems (Linux/macOS/Windows)

    It should contain only non-secret, repo-specific settings (no tokens).
    """
    return os.path.join("Database", "kicad_library_manager.json")


@dataclass
class Config:
    repo_path: str = ""
    # Single-string remote setting (preferred UI): paste a GitHub repo URL or "owner/repo".
    # Used to populate the legacy fields below for backwards compatibility.
    remote_db_url: str = ""
    github_owner: str = ""
    github_repo: str = ""
    github_base_branch: str = "main"
    # DBL filename under Database/ (e.g. "library.kicad_dbl").
    # Used by the repo initializer; existing repos may already have a different filename.
    dbl_filename: str = ""
    # Treat remote status as "stale" if FETCH_HEAD is older than this many minutes.
    # This avoids doing expensive diff/status work when we haven't fetched recently.
    fetch_stale_minutes: int = 5

    @staticmethod
    def repo_settings_path(repo_path: str) -> str:
        rp = os.path.abspath(str(repo_path or "").strip())
        return os.path.join(rp, _repo_settings_relpath())

    @staticmethod
    def load_repo_settings(repo_path: str) -> dict:
        """
        Load repo-local settings dict from `<repo>/Database/kicad_library_manager.json`.
        Returns {} if missing/unreadable.
        """
        rp = str(repo_path or "").strip()
        if not rp:
            return {}
        p = Config.repo_settings_path(rp)
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def save_repo_settings(
        repo_path: str,
        *,
        remote_db_url: str,
        github_base_branch: str,
        dbl_filename: str,
    ) -> None:
        """
        Save repo-local settings file (portable, committed in the repo).

        Creates `Database/` if needed; does not touch global config.
        """
        rp = os.path.abspath(str(repo_path or "").strip())
        if not rp:
            raise RuntimeError("Missing repo_path")
        db_dir = os.path.join(rp, "Database")
        os.makedirs(db_dir, exist_ok=True)
        p = Config.repo_settings_path(rp)
        payload = {
            "version": 1,
            "remote_db_url": str(remote_db_url or "").strip(),
            "github_base_branch": str(github_base_branch or "").strip() or "main",
            "dbl_filename": str(dbl_filename or "").strip(),
        }
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")

    @staticmethod
    def parse_remote_db_url(url: str) -> tuple[str, str, str]:
        """
        Parse a GitHub-style repo URL (or "owner/repo") into (owner, repo, branch_from_url).

        Accepted examples:
        - https://github.com/OWNER/REPO
        - https://github.com/OWNER/REPO/tree/BRANCH
        - git@github.com:OWNER/REPO.git
        - OWNER/REPO
        - OWNER/REPO@BRANCH
        """
        raw = str(url or "").strip()
        if not raw:
            return ("", "", "main")

        branch = ""
        s = raw
        # Optional "@branch" suffix.
        if "@" in s and not s.startswith("http"):
            # for safety, only treat '@' suffix as branch in shorthand forms
            left, right = s.rsplit("@", 1)
            if "/" in left and right and " " not in right and "/" not in right:
                s, branch = left.strip(), right.strip()
        elif "@" in s and s.startswith("http"):
            # allow https://...@branch too (rare)
            left, right = s.rsplit("@", 1)
            if right and " " not in right and "/" not in right:
                s, branch = left.strip(), right.strip()

        owner = repo = ""
        # git@github.com:OWNER/REPO(.git)
        if s.startswith("git@github.com:") and ":" in s:
            path = s.split(":", 1)[1].strip()
            if path.endswith(".git"):
                path = path[:-4]
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 2:
                owner, repo = parts[0], parts[1]
        # https://github.com/OWNER/REPO(/tree/BRANCH)
        elif "github.com/" in s:
            try:
                after = s.split("github.com/", 1)[1]
            except Exception:
                after = ""
            after = after.split("?", 1)[0].split("#", 1)[0]
            parts = [p for p in after.split("/") if p]
            if len(parts) >= 2:
                owner, repo = parts[0], parts[1]
                if repo.endswith(".git"):
                    repo = repo[:-4]
            # Optional /tree/<branch>
            if len(parts) >= 4 and parts[2] == "tree" and parts[3]:
                branch = branch or parts[3]
        # OWNER/REPO
        else:
            parts = [p for p in s.split("/") if p]
            if len(parts) == 2:
                owner, repo = parts[0], parts[1]
                if repo.endswith(".git"):
                    repo = repo[:-4]

        br = (branch or "").strip()
        return (str(owner or "").strip(), str(repo or "").strip(), br)

    @staticmethod
    def normalize_remote_repo_url(url: str) -> str:
        """
        Return a git-fetchable remote URL.

        - Keeps SSH/HTTPS URLs as-is (strips whitespace)
        - Accepts "OWNER/REPO" and converts to "https://github.com/OWNER/REPO.git"
        """
        raw = str(url or "").strip()
        if not raw:
            return ""
        if raw.startswith(("git@", "ssh://", "https://", "http://")):
            return raw
        owner, repo, _br = Config.parse_remote_db_url(raw)
        if owner and repo:
            return f"https://github.com/{owner}/{repo}.git"
        return raw

    @staticmethod
    def load(path: str | None = None) -> "Config":
        cfg_path = path or _default_config_path()
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Config(
                repo_path=str(data.get("repo_path", "")),
                remote_db_url=str(data.get("remote_db_url", "")),
                github_owner=str(data.get("github_owner", "")),
                github_repo=str(data.get("github_repo", "")),
                github_base_branch=str(data.get("github_base_branch", "main")),
                dbl_filename=str(data.get("dbl_filename", "")),
                fetch_stale_minutes=int(data.get("fetch_stale_minutes", 5) or 5),
            )
        except FileNotFoundError:
            return Config()
        except Exception:
            # If config is corrupted, don't crash KiCad; start fresh.
            return Config()

    @staticmethod
    def load_effective(repo_path: str | None = None) -> "Config":
        """
        Load effective settings for a given repo:
        - start from global config (user machine)
        - overlay repo-local settings from the database repo (portable)

        The caller should pass the currently selected repo path when available.
        """
        cfg = Config.load()
        rp = str(repo_path or "").strip()
        if rp:
            cfg.repo_path = rp
            d = Config.load_repo_settings(rp)
            if isinstance(d, dict) and d:
                rurl = str(d.get("remote_db_url", "") or "").strip()
                br = str(d.get("github_base_branch", "") or "").strip()
                dbl = str(d.get("dbl_filename", "") or "").strip()
                if rurl:
                    cfg.remote_db_url = rurl
                    owner, repo, branch_from_url = Config.parse_remote_db_url(rurl)
                    cfg.github_owner = owner
                    cfg.github_repo = repo
                    # If repo-local file didn't specify branch, allow URL shorthand to set it.
                    if not br and branch_from_url:
                        br = branch_from_url
                if br:
                    cfg.github_base_branch = br
                if dbl:
                    cfg.dbl_filename = dbl
        return cfg

    def save(self, path: str | None = None) -> None:
        cfg_path = path or _default_config_path()
        os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "repo_path": self.repo_path,
                    "remote_db_url": self.remote_db_url,
                    "github_owner": self.github_owner,
                    "github_repo": self.github_repo,
                    "github_base_branch": self.github_base_branch,
                    "dbl_filename": self.dbl_filename,
                    "fetch_stale_minutes": int(self.fetch_stale_minutes or 5),
                },
                f,
                indent=2,
                sort_keys=True,
            )

