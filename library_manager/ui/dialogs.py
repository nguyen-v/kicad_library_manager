from __future__ import annotations

import glob
import os
import wx

from ..config import Config
from ..repo import Category
from ..repo import is_repo_root
from .async_ui import is_window_alive
from .services import validate_row
from .widgets import ComponentFormPanel


class RepoSettingsDialog(wx.Dialog):
    """
    Small, focused settings dialog.
    """

    def __init__(self, parent: wx.Window, cfg: Config, repo_path: str = "", project_path: str = ""):
        super().__init__(parent, title="Repository settings", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._cfg = cfg
        self._repo_path = repo_path
        self._project_path = project_path

        root = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(cols=2, vgap=8, hgap=10)
        grid.AddGrowableCol(1, 1)

        grid.Add(wx.StaticText(self, label="Local database path"), 0, wx.ALIGN_CENTER_VERTICAL)
        # Row: [path text] [Browse...]
        repo_row = wx.BoxSizer(wx.HORIZONTAL)
        self.repo = wx.TextCtrl(self, value=repo_path or cfg.repo_path)
        repo_row.Add(self.repo, 1, wx.EXPAND)
        repo_row.AddSpacer(8)
        browse_btn = wx.Button(self, label="Browse…")
        browse_btn.Bind(wx.EVT_BUTTON, self._on_browse_repo)
        repo_row.Add(browse_btn, 0)
        grid.Add(repo_row, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label="Remote database URL"), 0, wx.ALIGN_CENTER_VERTICAL)
        # Prefer the single-field URL; fall back to reconstructing from legacy fields.
        guess = (cfg.remote_db_url or "").strip()
        owner_guess = (getattr(cfg, "github_owner", "") or "").strip()
        repo_guess = (getattr(cfg, "github_repo", "") or "").strip()
        if not guess and (owner_guess and repo_guess):
            guess = f"https://github.com/{owner_guess}/{repo_guess}.git"
        self.remote_url = wx.TextCtrl(self, value=str(guess or ""))
        try:
            self.remote_url.SetHint("e.g. git@github.com:OWNER/REPO.git or https://github.com/OWNER/REPO.git or OWNER/REPO")
        except Exception:
            pass
        grid.Add(self.remote_url, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label="Branch"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.base = wx.TextCtrl(self, value=str(cfg.github_base_branch or "main"))
        try:
            self.base.SetHint("main")
        except Exception:
            pass
        grid.Add(self.base, 1, wx.EXPAND)

        # DBL filename (used by initializer; not required for existing repos).
        grid.Add(wx.StaticText(self, label="DBL filename"), 0, wx.ALIGN_CENTER_VERTICAL)
        dbl_guess = (cfg.dbl_filename or "").strip()
        rp_guess = str(repo_path or cfg.repo_path or "").strip()
        if not dbl_guess and rp_guess:
            try:
                cands = sorted(glob.glob(os.path.join(rp_guess, "Database", "*.kicad_dbl")))
                if len(cands) == 1:
                    dbl_guess = os.path.basename(cands[0])
            except Exception:
                pass
        if not dbl_guess:
            dbl_guess = "library.kicad_dbl"
        self.dbl = wx.TextCtrl(self, value=str(dbl_guess))
        try:
            self.dbl.SetHint("library.kicad_dbl")
        except Exception:
            pass
        grid.Add(self.dbl, 1, wx.EXPAND)

        root.Add(grid, 1, wx.ALL | wx.EXPAND, 10)
        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.init_btn = wx.Button(self, label="Initialize database repo…")
        self.init_btn.Bind(wx.EVT_BUTTON, self._on_init_repo)
        btns.Add(self.init_btn, 0, wx.ALL, 10)
        self.update_btn = wx.Button(self, label="Update repo tools…")
        self.update_btn.Bind(wx.EVT_BUTTON, self._on_update_repo_tools)
        btns.Add(self.update_btn, 0, wx.ALL, 10)
        btns.AddStretchSpacer(1)
        # NOTE: wx.ALIGN_RIGHT is invalid inside a horizontal sizer on some wx builds
        # (asserts). The stretch spacer already pushes this to the right.
        btns.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL, 10)
        root.Add(btns, 0, wx.EXPAND)
        self.SetSizer(root)
        self.SetMinSize((720, 260))

        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

    def _on_browse_repo(self, _evt: wx.CommandEvent) -> None:
        """
        Pick a local repo folder.
        """
        # Prefer the KiCad project directory (or its Libraries/ folder) so users
        # working with per-project submodules don't jump to some global repo.
        start = ""
        try:
            proj = str(getattr(self, "_project_path", "") or "").strip()
        except Exception:
            proj = ""
        if proj and os.path.isdir(proj):
            libs = os.path.join(proj, "Libraries")
            start = libs if os.path.isdir(libs) else proj
        if not start:
            # Fall back to the currently selected repo path (if any).
            start = (self.repo.GetValue() or "").strip() or str(self._repo_path or "").strip() or str(self._cfg.repo_path or "").strip()
        if start and not os.path.isdir(start):
            start = ""
        if not start:
            try:
                start = os.getcwd()
            except Exception:
                start = ""

        dlg = wx.DirDialog(
            self,
            message="Select local database repo folder",
            defaultPath=start,
            style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST,
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            picked = str(dlg.GetPath() or "").strip()
        finally:
            try:
                dlg.Destroy()
            except Exception:
                pass

        if not picked:
            return

        self._repo_path = picked
        try:
            self.repo.SetValue(picked)
        except Exception:
            pass

        # Load repo-local settings (portable) to avoid cross-project leakage.
        try:
            eff = Config.load_effective(picked)
            if (eff.remote_db_url or "").strip():
                self.remote_url.SetValue(str(eff.remote_db_url or ""))
            if (eff.github_base_branch or "").strip():
                self.base.SetValue(str(eff.github_base_branch or "main"))
            if (eff.dbl_filename or "").strip():
                self.dbl.SetValue(str(eff.dbl_filename or ""))
        except Exception:
            pass

        # Best-effort: if this looks like a database repo, auto-guess DBL filename.
        try:
            cands = sorted(glob.glob(os.path.join(picked, "Database", "*.kicad_dbl")))
            if len(cands) == 1:
                self.dbl.SetValue(os.path.basename(cands[0]))
        except Exception:
            pass

        # Friendly warning if structure doesn't match yet (still allow init flow).
        try:
            if not is_repo_root(picked):
                wx.MessageBox(
                    "Selected folder does not look like a KiCad database repo yet.\n\n"
                    "Expected to find:\n"
                    "- Database/ (with categories.yml or a *.kicad_dbl)\n"
                    "- Symbols/\n"
                    "- Footprints/\n\n"
                    "You can still click “Initialize database repo…” to scaffold missing files.",
                    "Repository settings",
                    wx.OK | wx.ICON_INFORMATION,
                    parent=self,
                )
        except Exception:
            pass

    def _apply_remote_url_best_effort(self, *, repo_path: str, url: str) -> None:
        # Best-effort: apply the remote URL to the local repo's "origin" remote so the
        # rest of the UI (fetch, ls-remote, origin/<branch> comparisons) uses it.
        rp = str(repo_path or "").strip()
        remote = Config.normalize_remote_repo_url(url)
        if not (rp and remote):
            return
        from .git_ops import run_git

        try:
            run_git(["git", "remote", "get-url", "origin"], cwd=rp)
            run_git(["git", "remote", "set-url", "origin", remote], cwd=rp)
        except Exception:
            # If "origin" doesn't exist yet, add it.
            run_git(["git", "remote", "add", "origin", remote], cwd=rp)

    def _on_ok(self, _evt: wx.CommandEvent) -> None:
        rp = (self.repo.GetValue() or "").strip()
        if rp:
            self._repo_path = rp
            self._cfg.repo_path = rp
        url = (self.remote_url.GetValue() or "").strip()
        self._cfg.remote_db_url = url
        owner, repo, branch_from_url = Config.parse_remote_db_url(url)
        # Keep legacy fields populated for existing code paths.
        self._cfg.github_owner = owner
        self._cfg.github_repo = repo
        branch = (self.base.GetValue() or "").strip() or (branch_from_url or "").strip() or "main"
        self._cfg.github_base_branch = branch
        dbl = (self.dbl.GetValue() or "").strip()
        if not dbl:
            dbl = "library.kicad_dbl"
        # Store only a filename; initializer writes to Database/<filename>.
        dbl = os.path.basename(dbl)
        if not dbl.endswith(".kicad_dbl"):
            dbl = dbl + ".kicad_dbl"
        self._cfg.dbl_filename = dbl
        # Save repo-local settings into the database repo (portable across computers).
        try:
            if rp:
                Config.save_repo_settings(
                    rp,
                    remote_db_url=url,
                    github_base_branch=branch,
                    dbl_filename=dbl,
                )
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(f"Could not save per-library settings into this repo:\n\n{exc}", "Repository settings", wx.OK | wx.ICON_WARNING, parent=self)
        try:
            self._cfg.save()
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(f"Could not save settings:\n{exc}", "Repository settings", wx.OK | wx.ICON_ERROR, parent=self)
            return

        try:
            self._apply_remote_url_best_effort(repo_path=str(self._cfg.repo_path or ""), url=url)
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(f"Could not apply remote URL to this repo:\n\n{exc}", "Repository settings", wx.OK | wx.ICON_WARNING, parent=self)
        self.EndModal(wx.ID_OK)

    def _on_init_repo(self, _evt: wx.CommandEvent) -> None:
        """
        Scaffold an existing git repo to support the database + CI workflow.
        Safe-by-default: create missing only, do not overwrite existing files.
        """
        from ..init_db_repo import commit_and_push_init, compute_init_actions, ensure_git_clean_and_origin, init_repo_create_missing_only

        repo_path = str(self._repo_path or self._cfg.repo_path or "").strip()
        if not repo_path:
            wx.MessageBox("Local database path is not set.", "Initialize database repo", wx.OK | wx.ICON_WARNING, parent=self)
            return

        url = (self.remote_url.GetValue() or "").strip()
        # Best effort: ensure origin exists before we require it.
        try:
            if url:
                self._apply_remote_url_best_effort(repo_path=repo_path, url=url)
        except Exception:
            pass

        branch = (self.base.GetValue() or "").strip() or "main"
        dbl = (self.dbl.GetValue() or "").strip() or "library.kicad_dbl"

        try:
            ensure_git_clean_and_origin(repo_path)
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(str(exc), "Initialize database repo", wx.OK | wx.ICON_WARNING, parent=self)
            return

        # Determine what would be created (missing only).
        try:
            actions = compute_init_actions(repo_path=repo_path, base_branch=branch, dbl_filename=dbl)
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(f"Could not compute initialization actions:\n\n{exc}", "Initialize database repo", wx.OK | wx.ICON_ERROR, parent=self)
            return

        missing: list[str] = []
        skipped: list[str] = []
        for rel, _txt in actions:
            ap = os.path.join(repo_path, rel)
            if os.path.exists(ap):
                skipped.append(rel)
            else:
                missing.append(rel)
        if not missing:
            wx.MessageBox("Nothing to initialize (all scaffold files already exist).", "Initialize database repo", wx.OK | wx.ICON_INFORMATION, parent=self)
            return

        default_msg = "chore: initialize database repo scaffolding"
        dlg = _InitRepoConfirmDialog(self, missing=missing, skipped=skipped, default_commit_message=default_msg)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            commit_msg = dlg.get_commit_message()
        finally:
            dlg.Destroy()

        try:
            res = init_repo_create_missing_only(repo_path=repo_path, base_branch=branch, dbl_filename=dbl)
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(f"Initialization failed:\n\n{exc}", "Initialize database repo", wx.OK | wx.ICON_ERROR, parent=self)
            return

        try:
            commit_and_push_init(
                repo_path=repo_path,
                commit_message=commit_msg,
                base_branch=branch,
                paths=list(res.created or []),
            )
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(f"Created scaffold files but could not commit/push:\n\n{exc}", "Initialize database repo", wx.OK | wx.ICON_WARNING, parent=self)
            return

        wx.MessageBox(
            f"Initialized repository.\n\nCreated {len(res.created)} file(s).\nSkipped {len(res.skipped_existing)} existing file(s).",
            "Initialize database repo",
            wx.OK | wx.ICON_INFORMATION,
            parent=self,
        )

        # Notify owner window to refresh status (best effort).
        try:
            parent = self.GetParent()
            if parent and hasattr(parent, "_refresh_sync_status"):
                parent._refresh_sync_status()  # type: ignore[misc]
            if parent and hasattr(parent, "_refresh_assets_status"):
                parent._refresh_assets_status()  # type: ignore[misc]
            if parent and hasattr(parent, "_reload_category_statuses"):
                parent._reload_category_statuses()  # type: ignore[misc]
            if parent and hasattr(parent, "_refresh_categories_status_icon"):
                parent._refresh_categories_status_icon()  # type: ignore[misc]
        except Exception:
            pass

    def _on_update_repo_tools(self, _evt: wx.CommandEvent) -> None:
        """
        Update scaffold-managed workflows + tools in an existing database repo.
        This overwrites those files when they differ from the current plugin templates.
        """
        from ..init_db_repo import commit_and_push_init, compute_update_actions, ensure_git_clean_and_origin, update_repo_scaffold_tools

        repo_path = str(self._repo_path or self._cfg.repo_path or "").strip()
        if not repo_path:
            wx.MessageBox("Local database path is not set.", "Update repo tools", wx.OK | wx.ICON_WARNING, parent=self)
            return

        branch = (self.base.GetValue() or "").strip() or "main"

        try:
            ensure_git_clean_and_origin(repo_path)
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(str(exc), "Update repo tools", wx.OK | wx.ICON_WARNING, parent=self)
            return

        # Determine which files would be updated/created.
        actions = []
        try:
            actions = compute_update_actions(repo_path=repo_path, base_branch=branch)
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(f"Could not compute update actions:\n\n{exc}", "Update repo tools", wx.OK | wx.ICON_ERROR, parent=self)
            return

        would_create: list[str] = []
        would_update: list[str] = []
        skipped_same: list[str] = []
        for rel, txt in actions:
            ap = os.path.join(repo_path, rel)
            if not os.path.exists(ap):
                would_create.append(rel)
                continue
            try:
                with open(ap, "r", encoding="utf-8", errors="replace") as f:
                    cur = f.read()
            except Exception:
                cur = None
            if cur == txt:
                skipped_same.append(rel)
            else:
                would_update.append(rel)

        if not would_create and not would_update:
            wx.MessageBox("Repo tools are already up to date.", "Update repo tools", wx.OK | wx.ICON_INFORMATION, parent=self)
            return

        default_msg = "chore: update database repo tools"
        dlg = _UpdateRepoToolsConfirmDialog(self, created=would_create, updated=would_update, skipped=skipped_same, default_commit_message=default_msg)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            commit_msg = dlg.get_commit_message()
        finally:
            dlg.Destroy()

        try:
            res = update_repo_scaffold_tools(repo_path=repo_path, base_branch=branch)
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(f"Update failed:\n\n{exc}", "Update repo tools", wx.OK | wx.ICON_ERROR, parent=self)
            return

        # Commit and push updates (created + updated files).
        try:
            paths = list(res.created or []) + list(res.updated or [])
            commit_and_push_init(
                repo_path=repo_path,
                commit_message=commit_msg or default_msg,
                base_branch=branch,
                paths=paths,
            )
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(f"Updated files but could not commit/push:\n\n{exc}", "Update repo tools", wx.OK | wx.ICON_WARNING, parent=self)
            return

        wx.MessageBox(
            f"Updated repository tooling.\n\nCreated {len(res.created)} file(s).\nUpdated {len(res.updated)} file(s).\nUnchanged {len(res.skipped_same)} file(s).",
            "Update repo tools",
            wx.OK | wx.ICON_INFORMATION,
            parent=self,
        )


class _InitRepoConfirmDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, *, missing: list[str], skipped: list[str], default_commit_message: str) -> None:
        super().__init__(parent, title="Initialize database repo", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._missing = list(missing or [])
        self._skipped = list(skipped or [])

        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(
            wx.StaticText(
                self,
                label=(
                    "This will scaffold the selected git repository so it can be used as a KiCad database repo.\n"
                    "Safety: existing files will NOT be overwritten (create-missing-only).\n"
                    "It will then commit and push the created files to origin."
                ),
            ),
            0,
            wx.ALL | wx.EXPAND,
            10,
        )

        root.Add(wx.StaticText(self, label="Files to create:"), 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        self._files = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 220))
        self._files.SetValue("\n".join(self._missing))
        root.Add(self._files, 1, wx.ALL | wx.EXPAND, 10)

        if self._skipped:
            root.Add(wx.StaticText(self, label=f"Existing files to keep (skipped): {len(self._skipped)}"), 0, wx.LEFT | wx.RIGHT, 10)

        msg_row = wx.BoxSizer(wx.HORIZONTAL)
        msg_row.Add(wx.StaticText(self, label="Commit message"), 0, wx.ALIGN_CENTER_VERTICAL)
        msg_row.AddSpacer(10)
        self._msg = wx.TextCtrl(self, value=str(default_commit_message or ""))
        msg_row.Add(self._msg, 1, wx.EXPAND)
        root.Add(msg_row, 0, wx.ALL | wx.EXPAND, 10)

        root.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.ALIGN_RIGHT, 10)
        self.SetSizer(root)
        self.SetMinSize((760, 520))

    def get_commit_message(self) -> str:
        try:
            return str(self._msg.GetValue() or "").strip()
        except Exception:
            return ""


class _UpdateRepoToolsConfirmDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, *, created: list[str], updated: list[str], skipped: list[str], default_commit_message: str) -> None:
        super().__init__(parent, title="Update repo tools", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._created = list(created or [])
        self._updated = list(updated or [])
        self._skipped = list(skipped or [])

        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(
            wx.StaticText(
                self,
                label=(
                    "This will update scaffold-managed workflows + tools in the selected database repo.\n"
                    "Safety: it will overwrite those specific files only when they differ from the current templates.\n"
                    "It will then commit and push the changes to origin."
                ),
            ),
            0,
            wx.ALL | wx.EXPAND,
            10,
        )

        def _block(title: str, items: list[str]) -> None:
            if not items:
                return
            root.Add(wx.StaticText(self, label=title), 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
            box = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 120))
            box.SetValue("\n".join(items))
            root.Add(box, 0, wx.ALL | wx.EXPAND, 10)

        _block(f"Files to update (overwrite): {len(self._updated)}", self._updated)
        _block(f"Files to create: {len(self._created)}", self._created)
        if self._skipped:
            root.Add(wx.StaticText(self, label=f"Unchanged files (skipped): {len(self._skipped)}"), 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        msg_row = wx.BoxSizer(wx.HORIZONTAL)
        msg_row.Add(wx.StaticText(self, label="Commit message"), 0, wx.ALIGN_CENTER_VERTICAL)
        msg_row.AddSpacer(10)
        self._msg = wx.TextCtrl(self, value=str(default_commit_message or ""))
        msg_row.Add(self._msg, 1, wx.EXPAND)
        root.Add(msg_row, 0, wx.ALL | wx.EXPAND, 10)

        root.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.ALIGN_RIGHT, 10)
        self.SetSizer(root)
        self.SetMinSize((820, 560))

    def get_commit_message(self) -> str:
        try:
            return str(self._msg.GetValue() or "").strip()
        except Exception:
            return ""

class ComponentDialogBase(wx.Dialog):
    def __init__(
        self,
        parent: wx.Window,
        *,
        title: str,
        repo_path: str,
        category: Category,
        headers: list[str],
        row: dict[str, str],
        existing_rows: list[dict[str, str]],
        editing_ipn: str | None = None,
        allow_copy_from_existing: bool = False,
    ):
        super().__init__(
            parent,
            title=title,
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX | wx.MINIMIZE_BOX,
        )
        self._repo_path = repo_path
        self._category = category
        self._headers = headers
        self._existing_rows = existing_rows
        self._editing_ipn = editing_ipn
        self._allow_copy_from_existing = bool(allow_copy_from_existing)
        self._symbols: list[str] = []
        self._footprints: list[str] = []

        root = wx.BoxSizer(wx.VERTICAL)

        # Info header (ui.py-like)
        hdr = wx.StaticText(self, label=title)
        root.Add(hdr, 0, wx.ALL | wx.EXPAND, 10)

        if self._allow_copy_from_existing:
            tmpl_row = wx.BoxSizer(wx.HORIZONTAL)
            tmpl_btn = wx.Button(self, label="Copy from existing…")
            tmpl_btn.Bind(wx.EVT_BUTTON, self._on_copy_from_existing)
            tmpl_hint = wx.StaticText(self, label="Prefill fields from an existing part in this category.")
            tmpl_row.Add(tmpl_btn, 0, wx.ALL, 6)
            tmpl_row.Add(tmpl_hint, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
            tmpl_row.AddStretchSpacer(1)
            root.Add(tmpl_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 4)

        self._status = wx.StaticText(self, label="")
        root.Add(self._status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        # Assets are loaded once here to avoid per-dialog duplication.
        self._symbols, self._footprints = self._load_assets_safely()

        self._form = ComponentFormPanel(
            self,
            repo_path=repo_path,
            headers=headers,
            row=row,
            symbols=self._symbols,
            footprints=self._footprints,
            on_create_footprint=self._on_add_footprint,
        )
        root.Add(self._form, 1, wx.ALL | wx.EXPAND, 8)

        root.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.SetSizer(root)
        self.SetMinSize((1200, 750))
        self.SetSize((1400, 900))

        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

    def _load_assets_safely(self) -> tuple[list[str], list[str]]:
        from .services import resolve_asset_lists

        try:
            return resolve_asset_lists(self._repo_path)
        except Exception as exc:  # noqa: BLE001
            self._status.SetLabel(f"Could not load symbols/footprints: {exc}")
            return [], []

    def _on_refresh_assets(self, _evt: wx.CommandEvent) -> None:
        self._symbols, self._footprints = self._load_assets_safely()
        self._form.set_symbol_choices(self._symbols)
        self._form.set_footprint_choices(self._footprints)
        self._status.SetLabel("Symbols/footprints list refreshed.")

    def _on_copy_from_existing(self, _evt: wx.CommandEvent) -> None:
        # Reuse the component browser UI for picking an existing row.
        from .browse_window import ComponentPickerDialog

        dlg = ComponentPickerDialog(self, repo_path=self._repo_path, category=self._category, title=f"Copy from: {self._category.display_name}")
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            picked = dlg.get_selected_row() or {}
        finally:
            dlg.Destroy()
        if not picked:
            return
        cur = self._form.get_row()
        merged: dict[str, str] = dict(cur)
        for h in self._headers:
            if h == "IPN":
                continue
            merged[h] = str(picked.get(h, "") or "")
        self._form.set_row_values(merged)
        self._status.SetLabel("Fields prefilled from existing part.")

    def _on_add_footprint(self) -> str:
        """
        Called by the shared form when the user clicks "Add footprint...".

        Strategy:
        - Open the footprint generator as a non-modal frame.
        - Refresh footprint list when that frame closes.
        - Keep this dialog alive and stable; no widget rebuild while modal children exist.
        """
        try:
            from kicad_footprint_generator.wx_gui import FootprintGeneratorDialog  # type: ignore
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(
                "Bundled footprint generator is unavailable.\n\n"
                "This UI is configured to use only the bundled generator shipped with the IPC plugin bundle.\n\n"
                f"Import error:\n{exc}",
                "Add footprint",
                wx.OK | wx.ICON_WARNING,
            )
            return ""

        frame = FootprintGeneratorDialog(self, self._repo_path)

        def on_gen_close(evt: wx.CloseEvent) -> None:
            try:
                evt.Skip()
            finally:
                if is_window_alive(self):
                    self._on_refresh_assets(wx.CommandEvent())

        frame.Bind(wx.EVT_CLOSE, on_gen_close)
        frame.Show()

        wx.MessageBox(
            "Footprint generator opened.\n\nAfter generating footprints, close it and use "
            "'Browse...' or pick from the dropdown.",
            "Add footprint",
            wx.OK | wx.ICON_INFORMATION,
        )
        return ""

    def _on_ok(self, _evt: wx.CommandEvent) -> None:
        row = self._form.get_row()
        errs = validate_row(
            self._headers,
            row,
            editing_ipn=self._editing_ipn,
            existing_rows=self._existing_rows,
        )
        if errs:
            wx.MessageBox("\n".join(errs), self.GetTitle(), wx.OK | wx.ICON_WARNING, parent=self)
            return
        self._result_row = row
        self.EndModal(wx.ID_OK)

    def get_row(self) -> dict[str, str]:
        return dict(getattr(self, "_result_row", {}))


class AddEntryDialog(ComponentDialogBase):
    def __init__(self, parent: wx.Window, repo_path: str, category: Category, headers: list[str], existing_rows: list[dict[str, str]]):
        super().__init__(
            parent,
            title=f"Add component - {category.display_name}",
            repo_path=repo_path,
            category=category,
            headers=headers,
            row={h: "" for h in headers},
            existing_rows=existing_rows,
            editing_ipn=None,
            allow_copy_from_existing=True,
        )


class EditEntryDialog(ComponentDialogBase):
    def __init__(
        self,
        parent: wx.Window,
        repo_path: str,
        category: Category,
        headers: list[str],
        row: dict[str, str],
        existing_rows: list[dict[str, str]],
    ):
        super().__init__(
            parent,
            title=f"Edit component - {category.display_name}",
            repo_path=repo_path,
            category=category,
            headers=headers,
            row=row,
            existing_rows=existing_rows,
            editing_ipn=str((row or {}).get("IPN", "") or "").strip(),
            allow_copy_from_existing=False,
        )
