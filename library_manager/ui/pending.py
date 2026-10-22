from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass

from ..repo import Category
from .git_ops import git_object_exists, run_git


@dataclass(frozen=True)
class PendingState:
    # state:
    # - submitted: request file exists on origin/<branch> (or not yet checked)
    # - applied_remote: request file no longer exists on origin/<branch> (CI likely applied + deleted)
    state: str


class PendingStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_category: dict[str, list[dict]] = {}
        self._loaded = False

    def _store_path(self) -> str:
        """
        Persist pending UI state across plugin restarts.

        We keep this alongside config.json under XDG config dir:
          ~/.config/kicad_library_manager/pending.json
        """
        from ..paths import config_directory
        return str(config_directory() / 'pending.json')

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        p = self._store_path()
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        except Exception:
            # Corrupted file shouldn't crash KiCad.
            data = {}
        by_cat: dict[str, list[dict]] = {}
        if isinstance(data, dict):
            for k, v in data.items():
                kk = str(k or "").strip()
                if not kk or not isinstance(v, list):
                    continue
                items: list[dict] = []
                for it in v:
                    if isinstance(it, dict):
                        items.append(dict(it))
                if items:
                    by_cat[kk] = items
        self._by_category = by_cat
        self._loaded = True

    def _save_best_effort(self) -> None:
        try:
            p = self._store_path()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._by_category, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp, p)
        except Exception:
            return

    def add(self, category: str, item: dict) -> None:
        with self._lock:
            self._ensure_loaded()
            self._by_category.setdefault(category, []).append(dict(item or {}))
            self._save_best_effort()

    def list_for(self, category: str) -> list[dict]:
        with self._lock:
            self._ensure_loaded()
            return list(self._by_category.get(category, []))

    def has_any(self, category: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            return bool(self._by_category.get(category))

    def set_items(self, category: str, items: list[dict]) -> None:
        with self._lock:
            self._ensure_loaded()
            if items:
                self._by_category[category] = list(items)
            else:
                self._by_category.pop(category, None)
            self._save_best_effort()

    def items_by_category(self) -> dict[str, list[dict]]:
        with self._lock:
            self._ensure_loaded()
            return {k: list(v) for k, v in self._by_category.items()}


PENDING = PendingStore()


def _now() -> float:
    try:
        return float(time.time())
    except Exception:
        return 0.0


def update_pending_states_after_fetch(repo_path: str, *, category_name: str, branch: str, fetch_mtime: float | None) -> None:
    """
    After a successful fetch, update pending items' `state` using request file existence on origin/<branch>.
    """
    pend = PENDING.list_for(category_name)
    if not pend:
        return
    if fetch_mtime is None:
        return
    # Capture current remote-tracking SHA so we can avoid false "applied_remote" transitions
    # when the remote hasn't actually advanced since submission (common with eventual consistency).
    cur_sha = ""
    try:
        br = (branch or "").strip() or "main"
        cur_sha = (run_git(["git", "rev-parse", f"origin/{br}"], cwd=repo_path) or "").strip()
    except Exception:
        cur_sha = ""
    updated: list[dict] = []
    for p in pend:
        rp = str((p.get("req_path") or "")).strip()
        fetched_at_submit = float(p.get("fetch_mtime_at_submit") or 0.0)
        if rp and fetch_mtime > fetched_at_submit:
            # If we have no evidence the remote advanced since submit and we've never seen
            # the request file on origin/<branch>, do not conclude it was applied.
            sha_at_submit = str(p.get("origin_sha_at_submit") or "").strip()
            seen_remote = bool(p.get("seen_remote"))
            try:
                sha_unchanged = bool(sha_at_submit and cur_sha and sha_at_submit == cur_sha)
            except Exception:
                sha_unchanged = False
            if sha_unchanged and not seen_remote:
                updated.append(p)
                continue

            exists = git_object_exists(repo_path, f"origin/{branch}:{rp}")
            pp = dict(p)
            if exists:
                pp["seen_remote"] = True
                pp["state"] = "submitted"
            else:
                # Only mark applied if we've either seen it on remote before, or the
                # remote-tracking SHA changed since submit (request processed quickly).
                if seen_remote or (sha_at_submit and cur_sha and sha_at_submit != cur_sha):
                    pp["state"] = "applied_remote"
                else:
                    pp["state"] = "submitted"
            p = pp
        updated.append(p)
    PENDING.set_items(category_name, updated)


def reconcile_pending_against_local_csv(
    repo_path: str,
    *,
    category_name: str,
    local_by_ipn: dict[str, dict[str, str]],
) -> None:
    """
    Drop pending items that are now reflected in local CSV after a Sync (git pull).

    This is intentionally conservative for pending adds (only clears if we can match a resolved_ipn).
    """
    pend = PENDING.list_for(category_name)
    if not pend:
        return
    remaining: list[dict] = []
    for p in pend:
        action = str(p.get("action") or "").strip().lower()
        # IMPORTANT:
        # Do NOT drop items just because the request file is missing from local HEAD.
        # For component requests, this can be true *before* a user syncs (HEAD is still
        # behind origin/<branch>). We only clear pending here when the local CSV reflects
        # the requested change (add/update/delete), which happens after a Sync.

        if action == "delete":
            ipn = str(p.get("ipn") or "").strip()
            if ipn and ipn not in local_by_ipn:
                continue
            remaining.append(p)
            continue

        if action == "update":
            ipn = str(p.get("ipn") or "").strip()
            set_fields = dict(p.get("set") or {})
            if ipn and ipn in local_by_ipn and set_fields:
                row = local_by_ipn[ipn]
                ok = True
                for k, v in set_fields.items():
                    if str(row.get(k, "") or "") != str(v or ""):
                        ok = False
                        break
                if ok:
                    continue
            remaining.append(p)
            continue

        if action == "add":
            ripn = str(p.get("resolved_ipn") or "").strip()
            if ripn and ripn in local_by_ipn:
                continue
            remaining.append(p)
            continue

        remaining.append(p)

    PENDING.set_items(category_name, remaining)


def pending_tag_for_category(category_name: str) -> tuple[bool, bool]:
    """
    Return (has_pending, has_applied_remote) for the category.
    """
    pend = PENDING.list_for(category_name)
    if not pend:
        return (False, False)
    applied = any(str(p.get("state") or "") == "applied_remote" for p in pend)
    return (True, applied)


def drop_applied_pending_if_already_synced(repo_path: str, *, category_name: str) -> bool:
    """
    If we are already synced to a commit where the request file is gone from HEAD,
    drop `applied_remote` items immediately.

    This prevents contradictory UI states like "synchronized ... (sync needed)" when
    behind==0 but a pending item is still marked applied_remote.
    """
    pend = PENDING.list_for(category_name)
    if not pend:
        return False
    kept: list[dict] = []
    changed = False
    for p in pend:
        st = str(p.get("state") or "").strip()
        rp = str(p.get("req_path") or "").strip()
        # IMPORTANT: callers must only invoke this when the repo is actually synced
        # (behind==0 and up_to_date), otherwise we can drop too early.
        if st == "applied_remote" and rp:
            try:
                if not git_object_exists(repo_path, f"HEAD:{rp}"):
                    changed = True
                    continue
            except Exception:
                # If in doubt, keep it.
                pass
        kept.append(p)
    if changed:
        PENDING.set_items(category_name, kept)
    return changed

