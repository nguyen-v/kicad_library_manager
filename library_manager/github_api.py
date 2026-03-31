from __future__ import annotations

import base64
import json
import os
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

from ._subprocess import SUBPROCESS_NO_WINDOW


GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class GitHubRepo:
    owner: str
    repo: str
    base_branch: str = "main"


class GitHubError(RuntimeError):
    pass


def _gh_hosts_yml_candidates() -> list[str]:
    """
    Return candidate paths for the gh CLI hosts.yml config file.

    gh stores its config in different locations per platform:
      - Linux/macOS: ~/.config/gh/hosts.yml  (XDG_CONFIG_HOME/gh/)
      - Windows:     %APPDATA%/GitHub CLI/hosts.yml
    """
    paths: list[str] = []
    # GH_CONFIG_DIR overrides everything (all platforms).
    gh_config = (os.environ.get("GH_CONFIG_DIR") or "").strip()
    if gh_config:
        paths.append(os.path.join(gh_config, "hosts.yml"))
    # Windows: %APPDATA%\GitHub CLI
    appdata = (os.environ.get("APPDATA") or "").strip()
    if appdata:
        paths.append(os.path.join(appdata, "GitHub CLI", "hosts.yml"))
    # XDG / Linux / macOS default
    xdg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
    if xdg:
        paths.append(os.path.join(xdg, "gh", "hosts.yml"))
    paths.append(os.path.join(os.path.expanduser("~"), ".config", "gh", "hosts.yml"))
    return paths


def _read_gh_hosts_token() -> str | None:
    # Try each candidate location for the gh CLI hosts.yml config.
    for path in _gh_hosts_yml_candidates():
        try:
            with open(path, "r", encoding="utf-8") as f:
                txt = f.read()
        except Exception:
            continue

        in_github = False
        for raw in txt.splitlines():
            line = raw.rstrip("\n")
            if not line.strip() or line.strip().startswith("#"):
                continue
            if not line.startswith(" ") and line.strip().endswith(":"):
                host = line.strip()[:-1]
                in_github = host == "github.com"
                continue
            if in_github and "oauth_token:" in line:
                _, v = line.split("oauth_token:", 1)
                token = v.strip().strip('"').strip("'")
                if token:
                    return token
    return None


def _find_gh_executable() -> str | None:
    """
    Locate the ``gh`` CLI executable.

    On Windows, KiCad may not inherit the full user PATH, so ``shutil.which``
    can fail even when ``gh`` is installed.  Fall back to common install locations.
    """
    found = shutil.which("gh")
    if found:
        return found
    if sys.platform == "darwin":
        # KiCad.app may not inherit the full user PATH; check common locations.
        mac_candidates = [
            "/opt/homebrew/bin/gh",       # Apple Silicon Homebrew
            "/usr/local/bin/gh",          # Intel Homebrew / manual install
            "/opt/local/bin/gh",          # MacPorts
        ]
        for c in mac_candidates:
            if os.path.isfile(c):
                return c
        return None
    if sys.platform != "win32":
        return None
    # Common Windows install locations for gh CLI.
    candidates: list[str] = []
    pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
    pf86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    localappdata = os.environ.get("LOCALAPPDATA") or ""
    userprofile = os.environ.get("USERPROFILE") or ""
    candidates.append(os.path.join(pf, "GitHub CLI", "gh.exe"))
    candidates.append(os.path.join(pf86, "GitHub CLI", "gh.exe"))
    if localappdata:
        candidates.append(os.path.join(localappdata, "Programs", "GitHub CLI", "gh.exe"))
    if userprofile:
        candidates.append(os.path.join(userprofile, "scoop", "shims", "gh.exe"))
    choco = os.environ.get("ChocolateyInstall") or ""
    if choco:
        candidates.append(os.path.join(choco, "bin", "gh.exe"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _gh_auth_token() -> str | None:
    """
    Try to retrieve a GitHub token via ``gh auth token``.
    """
    gh = _find_gh_executable()
    if not gh:
        return None
    try:
        cp = subprocess.run(
            [gh, "auth", "token"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            **SUBPROCESS_NO_WINDOW,
        )
        tok = (cp.stdout or "").strip()
        return tok if tok else None
    except Exception:
        return None


def get_token() -> str:
    # Priority:
    # 1) env var (allows CI/testing)
    # 2) gh CLI (tries PATH, then common install locations on Windows)
    # 3) gh config file
    env = os.environ.get("GITHUB_TOKEN") or os.environ.get("KICAD_LIBRARY_MANAGER_GITHUB_TOKEN")
    if env:
        return env.strip()

    tok = _gh_auth_token()
    if tok:
        return tok

    tok = _read_gh_hosts_token()
    if tok:
        return tok

    raise GitHubError(
        "No GitHub token found. Please authenticate with `gh auth login` "
        "or set GITHUB_TOKEN / KICAD_LIBRARY_MANAGER_GITHUB_TOKEN."
    )


def _make_ssl_context() -> ssl.SSLContext:
    """Build an SSL context that works inside KiCad's bundled Python on macOS."""
    ctx = ssl.create_default_context()
    # On macOS the bundled Python often can't locate the system cert store.
    # Try certifi first, then well-known cert file locations.
    try:
        import certifi  # noqa: F811
        ctx.load_verify_locations(certifi.where())
        return ctx
    except Exception:
        pass
    for ca in (
        "/etc/ssl/cert.pem",                    # macOS system certs
        "/opt/homebrew/etc/openssl/cert.pem",    # Homebrew Apple Silicon
        "/usr/local/etc/openssl/cert.pem",       # Homebrew Intel
        "/opt/local/etc/openssl/cert.pem",       # MacPorts
        "/etc/ssl/certs/ca-certificates.crt",    # Debian/Ubuntu
    ):
        if os.path.isfile(ca):
            try:
                ctx.load_verify_locations(ca)
                return ctx
            except Exception:
                continue
    return ctx


def _request(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "kicad-library-manager")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30, context=_make_ssl_context()) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        try:
            msg = e.read().decode("utf-8", errors="replace")
        except Exception:
            msg = str(e)
        raise GitHubError(f"GitHub API error: {e.code} {e.reason}: {msg}")
    except urllib.error.URLError as e:
        raise GitHubError(f"GitHub API connection error: {e}")


def get_ref_sha(repo: GitHubRepo, token: str, branch: str) -> str:
    url = f"{GITHUB_API}/repos/{repo.owner}/{repo.repo}/git/ref/heads/{branch}"
    data = _request("GET", url, token)
    sha = (((data or {}).get("object") or {}).get("sha") or "").strip()
    if not sha:
        raise GitHubError(f"Could not resolve branch SHA for {branch}")
    return sha


def get_branch_head_sha(repo: GitHubRepo, token: str) -> str:
    """
    Convenience: latest commit SHA for repo.base_branch.
    """
    return get_ref_sha(repo, token, repo.base_branch)


def create_branch(repo: GitHubRepo, token: str, new_branch: str, from_sha: str) -> None:
    url = f"{GITHUB_API}/repos/{repo.owner}/{repo.repo}/git/refs"
    _request("POST", url, token, {"ref": f"refs/heads/{new_branch}", "sha": from_sha})


def get_file(repo: GitHubRepo, token: str, path: str, ref: str) -> tuple[str, str]:
    """
    Returns (decoded_text, sha)
    """
    url = f"{GITHUB_API}/repos/{repo.owner}/{repo.repo}/contents/{path}?ref={ref}"
    data = _request("GET", url, token)
    content_b64 = (data.get("content") or "").encode("utf-8")
    sha = (data.get("sha") or "").strip()
    if not sha:
        raise GitHubError(f"Missing sha for file {path}")
    decoded = base64.b64decode(content_b64).decode("utf-8", errors="replace")
    return decoded, sha


def put_file(repo: GitHubRepo, token: str, path: str, branch: str, message: str, content_text: str, sha: str) -> None:
    url = f"{GITHUB_API}/repos/{repo.owner}/{repo.repo}/contents/{path}"
    content_b64 = base64.b64encode(content_text.encode("utf-8")).decode("utf-8")
    _request(
        "PUT",
        url,
        token,
        {
            "message": message,
            "content": content_b64,
            "branch": branch,
            "sha": sha,
        },
    )


def create_file(repo: GitHubRepo, token: str, path: str, branch: str, message: str, content_text: str) -> None:
    """
    Create a brand-new file. This will fail if the file already exists.
    """
    url = f"{GITHUB_API}/repos/{repo.owner}/{repo.repo}/contents/{path}"
    content_b64 = base64.b64encode(content_text.encode("utf-8")).decode("utf-8")
    _request(
        "PUT",
        url,
        token,
        {
            "message": message,
            "content": content_b64,
            "branch": branch,
        },
    )


def create_pr(repo: GitHubRepo, token: str, head: str, title: str, body: str) -> str:
    url = f"{GITHUB_API}/repos/{repo.owner}/{repo.repo}/pulls"
    data = _request(
        "POST",
        url,
        token,
        {"title": title, "head": head, "base": repo.base_branch, "body": body},
    )
    html_url = (data.get("html_url") or "").strip()
    if not html_url:
        raise GitHubError("Failed to create PR (missing html_url)")
    return html_url

