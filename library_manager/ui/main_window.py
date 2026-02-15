from __future__ import annotations

import datetime as _dt
import csv
import difflib
import io
import importlib
import os
import re as _re
import sys
import traceback
import time

import wx
import wx.dataview as dv
import wx.grid as gridlib

from ..config import Config
from ..repo import Category, list_categories, is_repo_root
from .async_ui import UiRepeater, WindowTaskRunner, is_window_alive
from .browse_window import BrowseDialog
from .dialogs import RepoSettingsDialog
from .git_ops import (
    git_ls_remote_head_sha,
    git_diff_name_status,
    git_fetch_head_age_seconds,
    git_fetch_head_mtime,
    format_age_minutes,
    fetch_stale_threshold_seconds,
    git_last_updated_epoch_by_path,
    git_log_last_commits_for_path,
    git_show_commit_for_path,
    git_status_entries,
    git_commit_and_push_assets,
    suggest_assets_commit_message,
    git_sync_ff_only,
    git_sync_status,
    paths_changed_under,
    run_git,
)
from .icons import make_status_bitmap
from .pending import (
    PENDING,
    drop_applied_pending_if_already_synced,
    pending_tag_for_category,
    reconcile_pending_against_local_csv,
    update_pending_states_after_fetch,
)
from .services import category_title
from .widgets import SearchPickerDialog
from .assets.status import local_asset_paths
from .window_title import with_library_suffix


class MainDialog(wx.Frame):
    def __init__(self, parent: wx.Window | None, repo_path: str, project_path: str = ""):
        super().__init__(
            parent,
            title=with_library_suffix("KiCad Library Manager", repo_path),
            style=wx.DEFAULT_FRAME_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )
        self._repo_path = repo_path
        self._project_path = str(project_path or "")
        self._cfg = Config.load_effective(repo_path)
        self._categories: list[Category] = []
        self._tasks = WindowTaskRunner(self)
        self._did_autosize_cat_cols = False
        self._remote_cat_updated_ts_by_path: dict[str, int] = {}
        self._remote_cat_updated_loading = False
        # Column 0 is icon; default sort by Category ascending.
        self._cat_sort_col_idx = 1
        self._cat_sort_asc = True

        # Background refresh loop: checks remote main SHA; fetches only when changed.
        self._last_remote_sha: str | None = None
        self._last_remote_check_ts = 0.0
        self._remote_poll_s = 2.0
        self._remote_backoff_s = self._remote_poll_s
        self._remote_poll_inflight = False
        self._remote_poll_repeater: UiRepeater | None = None
        self._asset_index_prefetch_started = False
        # While a modal settings dialog is open, pause background UI activity
        # so it can't steal focus or show message boxes behind the dialog.
        self._modal_settings_open = False

        self._bmp_green = make_status_bitmap(wx.Colour(46, 160, 67))
        self._bmp_red = make_status_bitmap(wx.Colour(220, 53, 69))
        self._bmp_yellow = make_status_bitmap(wx.Colour(255, 193, 7))
        self._bmp_blue = make_status_bitmap(wx.Colour(13, 110, 253))
        self._bmp_gray = make_status_bitmap(wx.Colour(160, 160, 160))

        vbox = wx.BoxSizer(wx.VERTICAL)

        # Setup banner shown when repo path is not initialized yet.
        self._setup_mode = False
        self._setup_banner = wx.StaticText(self, label="")
        try:
            self._setup_banner.SetForegroundColour(wx.Colour(220, 53, 69))
        except Exception:
            pass
        self._setup_banner.Hide()
        vbox.Add(self._setup_banner, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 8)

        top = wx.BoxSizer(wx.HORIZONTAL)
        # Gray means "unknown/stale" until we fetch at least once.
        self.sync_icon = wx.StaticBitmap(self, bitmap=self._bmp_gray)
        self.sync_label = wx.StaticText(self, label="Library status: unknown")
        top.Add(self.sync_icon, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        top.Add(self.sync_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        top.AddStretchSpacer(1)
        self.refresh_btn = wx.Button(self, label="↓  Fetch remote")
        self.refresh_btn.Bind(wx.EVT_BUTTON, self._on_refresh_status)
        top.Add(self.refresh_btn, 0, wx.ALL, 6)
        self.sync_btn = wx.Button(self, label="↻  Sync library")
        self.sync_btn.Bind(wx.EVT_BUTTON, self._on_sync)
        top.Add(self.sync_btn, 0, wx.ALL, 6)
        vbox.Add(top, 0, wx.EXPAND)

        assets = wx.BoxSizer(wx.HORIZONTAL)
        self.assets_icon = wx.StaticBitmap(self, bitmap=self._bmp_gray)
        self.assets_label = wx.StaticText(self, label="Local assets (uncommitted): unknown — Remote assets: unknown")
        assets.Add(self.assets_icon, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        assets.Add(self.assets_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        assets.AddStretchSpacer(1)
        vbox.Add(assets, 0, wx.EXPAND)

        mid_splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)
        self._mid_splitter = mid_splitter
        cat_panel = wx.Panel(mid_splitter)
        hist_panel = wx.Panel(mid_splitter)

        self.cat_list = wx.ListCtrl(cat_panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.cat_img = wx.ImageList(12, 12)
        self.cat_img_green = self.cat_img.Add(self._bmp_green)
        self.cat_img_red = self.cat_img.Add(self._bmp_red)
        self.cat_img_yellow = self.cat_img.Add(self._bmp_yellow)
        self.cat_img_blue = self.cat_img.Add(self._bmp_blue)
        self.cat_list.AssignImageList(self.cat_img, wx.IMAGE_LIST_SMALL)
        self.cat_list.InsertColumn(0, "")
        self.cat_list.InsertColumn(1, "Category")
        self.cat_list.InsertColumn(2, "Status vs remote")
        self.cat_list.InsertColumn(3, "Last updated (remote)")
        self.cat_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_category_dclick)
        self.cat_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_category_selected)
        self.cat_list.Bind(wx.EVT_LIST_COL_CLICK, self._on_cat_col_click)

        cat_s = wx.BoxSizer(wx.VERTICAL)
        cat_s.Add(self.cat_list, 1, wx.EXPAND)
        cat_panel.SetSizer(cat_s)

        self._hist_title = wx.StaticText(hist_panel, label="History")
        self._hist_list = dv.DataViewListCtrl(hist_panel, style=dv.DV_ROW_LINES | dv.DV_VERT_RULES)
        self._hist_list.AppendTextColumn("Date", width=160, mode=dv.DATAVIEW_CELL_INERT)
        self._hist_list.AppendTextColumn("Author", width=140, mode=dv.DATAVIEW_CELL_INERT)
        self._hist_list.AppendTextColumn("Subject", width=360, mode=dv.DATAVIEW_CELL_INERT)
        self._hist_show_btn = wx.Button(hist_panel, label="Show diff")
        self._hist_show_btn.Bind(wx.EVT_BUTTON, self._on_hist_show_diff)
        self._hist_show_btn.Enable(False)
        self._hist_list.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self._on_hist_selection_changed)
        self._hist_list.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, self._on_hist_item_activated)

        hist_s = wx.BoxSizer(wx.VERTICAL)
        hist_s.Add(self._hist_title, 0, wx.ALL, 6)
        hist_s.Add(self._hist_list, 1, wx.ALL | wx.EXPAND, 6)
        hist_s.Add(self._hist_show_btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        hist_panel.SetSizer(hist_s)

        mid_splitter.SplitVertically(cat_panel, hist_panel, sashPosition=-520)
        mid_splitter.SetMinimumPaneSize(320)
        vbox.Add(mid_splitter, 1, wx.ALL | wx.EXPAND, 8)

        vbox.Add(wx.StaticText(self, label="Output"), 0, wx.LEFT | wx.RIGHT, 8)
        self.log = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 140))
        vbox.Add(self.log, 0, wx.ALL | wx.EXPAND, 8)

        bottom = wx.BoxSizer(wx.HORIZONTAL)
        self.settings_btn = wx.Button(self, label="Settings")
        self.settings_btn.Bind(wx.EVT_BUTTON, self._on_settings)
        bottom.Add(self.settings_btn, 0, wx.ALL, 8)

        self.categories_btn = wx.Button(self, label="Manage categories")
        self.categories_btn.Bind(wx.EVT_BUTTON, self._on_manage_categories)
        self._set_button_bitmap(self.categories_btn, self._bmp_gray)
        bottom.Add(self.categories_btn, 0, wx.ALL, 8)

        self.create_fp_btn = wx.Button(self, label="Create footprint")
        self.create_fp_btn.Bind(wx.EVT_BUTTON, self._on_create_footprint)
        bottom.Add(self.create_fp_btn, 0, wx.ALL, 8)

        self.browse_fp_btn = wx.Button(self, label="Browse footprints")
        self.browse_fp_btn.Bind(wx.EVT_BUTTON, self._on_browse_footprints)
        self._set_button_bitmap(self.browse_fp_btn, self._bmp_gray)
        bottom.Add(self.browse_fp_btn, 0, wx.ALL, 8)

        self.browse_sym_btn = wx.Button(self, label="Browse symbols")
        self.browse_sym_btn.Bind(wx.EVT_BUTTON, self._on_browse_symbols)
        self._set_button_bitmap(self.browse_sym_btn, self._bmp_gray)
        bottom.Add(self.browse_sym_btn, 0, wx.ALL, 8)

        bottom.AddStretchSpacer(1)
        close_btn = wx.Button(self, label="Close")
        # IMPORTANT: do not just Hide() the main window.
        # This plugin runs as an external process; hiding would keep it alive in the background,
        # preventing re-launch when single-instance locking is enabled.
        close_btn.Bind(wx.EVT_BUTTON, lambda _e: self.Close())
        bottom.Add(close_btn, 0, wx.ALL, 8)
        vbox.Add(bottom, 0, wx.EXPAND)

        self.SetSizer(vbox)
        self.Layout()
        # Make the main window comfortably large by default (most workflows use it all day).
        self.SetMinSize((1300, 850))
        self.SetSize((1750, 1050))

        # Initialize UI based on whether this repo looks initialized.
        self._apply_repo_state()
        # IMPORTANT:
        # KiCad shutdown should always be able to terminate its process. Some KiCad/wx builds
        # issue veto-able close events during shutdown; if we veto those, KiCad can hang.
        # Therefore, we never veto EVT_CLOSE here.
        self.Bind(wx.EVT_CLOSE, self._on_close_event)
        try:
            self.Bind(wx.EVT_SHOW, self._on_show_event)
        except Exception:
            pass
        try:
            self.Bind(wx.EVT_QUERY_END_SESSION, self._on_end_session)
            self.Bind(wx.EVT_END_SESSION, self._on_end_session)
        except Exception:
            # Not all wx ports expose these consistently.
            pass

        # History panel state
        self._hist_rows: list[dict[str, str]] = []
        self._hist_for_path: str | None = None
        self._set_history_rows([])

    def _repo_ready(self) -> tuple[bool, str]:
        """
        Return (ready, reason). "Ready" means we can safely run background git polling
        and populate the UI without crashing/hanging during initial setup.
        """
        rp = str(getattr(self, "_repo_path", "") or "").strip()
        if not rp:
            return (False, "No local database path is set. Click Settings… to select a folder.")
        if not os.path.isdir(rp):
            return (False, f"Local database path does not exist:\n{rp}\n\nClick Settings… to select a folder.")
        if not is_repo_root(rp):
            return (
                False,
                "Selected folder is not initialized as a KiCad database repo yet.\n\n"
                "Next step: click Settings… → Initialize database repo…",
            )
        # Require a git worktree too; most features assume git operations.
        try:
            run_git(["git", "rev-parse", "--is-inside-work-tree"], cwd=rp)
        except Exception:
            return (
                False,
                "Selected folder does not look like a git repository.\n\n"
                "Initialize expects an existing git repo with an origin remote.",
            )
        return (True, "")

    def _enter_setup_mode(self, reason: str) -> None:
        if bool(getattr(self, "_setup_mode", False)):
            try:
                self._setup_banner.SetLabel(str(reason or ""))
                self._setup_banner.Show()
                self.Layout()
            except Exception:
                pass
            return
        self._setup_mode = True
        # Stop background activity that can spam errors during setup.
        try:
            self._stop_remote_polling()
        except Exception:
            pass
        try:
            self._tasks.cancel_pending()
        except Exception:
            pass

        try:
            self._setup_banner.SetLabel(str(reason or ""))
            self._setup_banner.Show()
        except Exception:
            pass

        # Disable actions that assume a fully initialized repo.
        for btn_name in (
            "refresh_btn",
            "sync_btn",
            "categories_btn",
            "create_fp_btn",
            "browse_fp_btn",
            "browse_sym_btn",
        ):
            try:
                b = getattr(self, btn_name, None)
                if b:
                    b.Enable(False)
            except Exception:
                pass

        # Clear/disable category/history views.
        try:
            self.cat_list.Freeze()
        except Exception:
            pass
        try:
            self.cat_list.DeleteAllItems()
        except Exception:
            pass
        try:
            self.cat_list.Thaw()
        except Exception:
            pass
        try:
            self._hist_title.SetLabel("History")
        except Exception:
            pass
        try:
            self._set_history_rows([])
        except Exception:
            pass

        # Set neutral status text.
        try:
            self.sync_icon.SetBitmap(self._bmp_gray)
            self.sync_label.SetLabel("Library status: not initialized")
        except Exception:
            pass
        try:
            self.assets_icon.SetBitmap(self._bmp_gray)
            self.assets_label.SetLabel("Assets: unavailable until repository is initialized")
        except Exception:
            pass
        try:
            self.Layout()
        except Exception:
            pass

    def _leave_setup_mode(self) -> None:
        if not bool(getattr(self, "_setup_mode", False)):
            return
        self._setup_mode = False
        try:
            self._setup_banner.Hide()
        except Exception:
            pass
        # Re-enable buttons.
        for btn_name in (
            "refresh_btn",
            "sync_btn",
            "categories_btn",
            "create_fp_btn",
            "browse_fp_btn",
            "browse_sym_btn",
        ):
            try:
                b = getattr(self, btn_name, None)
                if b:
                    b.Enable(True)
            except Exception:
                pass
        try:
            self.Layout()
        except Exception:
            pass

    def _apply_repo_state(self) -> None:
        """
        Refresh UI + background loops depending on whether repo is initialized.
        """
        try:
            self.SetTitle(with_library_suffix("KiCad Library Manager", self._repo_path, cfg=getattr(self, "_cfg", None)))
        except Exception:
            pass
        ready, reason = self._repo_ready()
        if not ready:
            self._enter_setup_mode(reason)
            return

        self._leave_setup_mode()
        self._refresh_sync_status()
        self._refresh_assets_status()
        self._refresh_remote_cat_updated_times_async()
        self._reload_category_statuses()
        self._refresh_categories_status_icon()
        self._start_asset_index_prefetch_best_effort()
        self._start_remote_polling()

    def _set_button_bitmap(self, btn: wx.Button, bmp: wx.Bitmap | None) -> None:
        """
        Best-effort: show a small status icon *inside* a standard button.
        """
        if not btn:
            return
        if bmp is None or not getattr(bmp, "IsOk", lambda: False)():
            return
        try:
            # Ensure we always have visible spacing between icon and text across platforms.
            # Some ports (notably GTK) ignore/limit SetBitmapMargins; leading NBSPs are reliable.
            try:
                if not hasattr(btn, "_kicad_lm_orig_label"):
                    setattr(btn, "_kicad_lm_orig_label", btn.GetLabel())
                orig = str(getattr(btn, "_kicad_lm_orig_label", "") or "")
                pad = "\u00A0" * 3  # ~ "3 spaces" but never collapsed
                want = pad + orig if orig and not orig.startswith(pad) else (orig or btn.GetLabel())
                if want and btn.GetLabel() != want:
                    btn.SetLabel(want)
            except Exception:
                pass
            try:
                # Keep the margin too; may work on some platforms/themes.
                btn.SetBitmapMargins((12, 0))
            except Exception:
                pass
            try:
                btn.SetBitmapPosition(wx.LEFT)
            except Exception:
                pass
            btn.SetBitmap(bmp)
        except Exception:
            return

    def _start_asset_index_prefetch_best_effort(self) -> None:
        """
        Prefetch footprint/symbol indexes in background so opening browsers doesn't hitch.
        """
        if getattr(self, "_asset_index_prefetch_started", False):
            return
        self._asset_index_prefetch_started = True

        def _do() -> None:
            rp = str(getattr(self, "_repo_path", "") or "")
            if not rp:
                return
            # These ensure_started() calls spawn their own background threads.
            try:
                from .footprints.libcache import FP_LIBCACHE

                FP_LIBCACHE.ensure_started(rp)
            except Exception:
                pass
            try:
                from .symbols.libcache import SYMBOL_LIBCACHE

                SYMBOL_LIBCACHE.ensure_started(rp)
            except Exception:
                pass

            # Also prefetch symbol Description/Datasheet metadata in background so the
            # symbol browser doesn't block on first open when populating descriptions.
            try:
                import threading
                import os

                def _prefetch_sym_meta() -> None:
                    try:
                        # Wait briefly for index to load.
                        for _ in range(80):
                            st = SYMBOL_LIBCACHE.snapshot(rp)
                            if bool(st.get("loaded")) and not bool(st.get("loading")):
                                break
                            import time as _time

                            _time.sleep(0.1)
                        st = SYMBOL_LIBCACHE.snapshot(rp)
                        sym_files = dict(st.get("sym_lib_files") or {})
                        if not sym_files:
                            return
                        # Load meta per lib (this scans the file but we are in a background thread).
                        # Prioritize repo-local libs first, then global/project libs.
                        root = os.path.abspath(os.path.join(rp, "Symbols")) + os.sep
                        local_first: list[str] = []
                        global_after: list[str] = []
                        for lib, p in sym_files.items():
                            nick = str(lib or "").strip()
                            if not nick:
                                continue
                            try:
                                ap = os.path.abspath(str(p or ""))
                            except Exception:
                                ap = str(p or "")
                            if ap.startswith(root):
                                local_first.append(nick)
                            else:
                                global_after.append(nick)
                        libs = sorted(set(local_first)) + sorted(set([x for x in global_after if x not in set(local_first)]))
                        # IMPORTANT: even in a background thread, heavy Python parsing can
                        # momentarily starve the wx UI thread due to the GIL. Throttle the
                        # work so the main window stays smooth.
                        import time as _time
                        batch = 0
                        for lib in libs:
                            if not is_window_alive(self):
                                return
                            try:
                                SYMBOL_LIBCACHE.ensure_meta_loaded(rp, lib, wait_s=0.0)
                            except Exception:
                                continue
                            # Short yield between libs; longer yield every few libs.
                            batch += 1
                            try:
                                _time.sleep(0.01)
                            except Exception:
                                pass
                            if batch % 8 == 0:
                                try:
                                    _time.sleep(0.08)
                                except Exception:
                                    pass
                    except Exception:
                        return

                threading.Thread(target=_prefetch_sym_meta, daemon=True).start()
            except Exception:
                pass

        try:
            wx.CallAfter(_do)
        except Exception:
            _do()

    def _start_remote_polling(self) -> None:
        if getattr(self, "_remote_poll_repeater", None):
            return
        # Fast UI tick; remote checks are throttled inside _on_remote_poll_tick.
        self._remote_poll_repeater = UiRepeater(self, interval_ms=1000, callback=self._on_remote_poll_tick)

    def _stop_remote_polling(self) -> None:
        try:
            rep = getattr(self, "_remote_poll_repeater", None)
        except Exception:
            rep = None
        if rep:
            try:
                rep.stop()
            except Exception:
                pass
        self._remote_poll_repeater = None

    def _on_show_event(self, evt: wx.ShowEvent) -> None:
        try:
            if evt.IsShown():
                # Don't start polling while repo is in setup mode.
                if not bool(getattr(self, "_setup_mode", False)):
                    self._start_remote_polling()
            else:
                self._stop_remote_polling()
        finally:
            try:
                evt.Skip()
            except Exception:
                pass

    def _on_remote_poll_tick(self) -> None:
        """
        Legacy ui.py behavior:
        - every ~2s, run `git ls-remote --heads origin main` (no GitHub API)
        - if SHA changed, trigger a full `git fetch origin main`
        - backoff exponentially on errors up to 60s
        """
        if bool(getattr(self, "_setup_mode", False)) or bool(getattr(self, "_modal_settings_open", False)):
            return
        if not is_window_alive(self):
            return
        try:
            if not self.IsShown():
                return
        except Exception:
            pass
        if self._remote_poll_inflight:
            return
        now = time.time()
        if (now - float(self._last_remote_check_ts or 0.0)) < float(self._remote_backoff_s or 0.0):
            return
        self._last_remote_check_ts = now
        self._remote_poll_inflight = True

        def work() -> str:
            # Check remote head SHA without GitHub API calls.
            try:
                branch = (self._cfg.github_base_branch or "main").strip() or "main"
            except Exception:
                branch = "main"
            return git_ls_remote_head_sha(self._repo_path, remote="origin", branch=branch, timeout_s=3.0)

        def done(sha: str | None, err: Exception | None) -> None:
            self._remote_poll_inflight = False
            if not is_window_alive(self):
                return
            if bool(getattr(self, "_modal_settings_open", False)):
                return
            if err or not sha:
                try:
                    self._remote_backoff_s = min(float(self._remote_backoff_s) * 2.0, 60.0)
                except Exception:
                    self._remote_backoff_s = 60.0
                return
            self._remote_backoff_s = self._remote_poll_s
            if sha != self._last_remote_sha:
                self._last_remote_sha = sha
                self._on_refresh_status(None)

        self._tasks.run(work, done)

    def _on_close_event(self, evt: wx.CloseEvent) -> None:
        # Never veto: allow KiCad to exit cleanly.
        self._destroy_for_shutdown_best_effort()
        try:
            evt.Skip()
        except Exception:
            pass

    def _on_end_session(self, evt: wx.CloseEvent) -> None:
        """
        Application is ending session (shutdown/logout/close). Do not veto.
        """
        self._destroy_for_shutdown_best_effort()
        try:
            evt.Skip()
        except Exception:
            pass

    def _destroy_for_shutdown_best_effort(self) -> None:
        """
        Ensure all child windows are destroyed and background callbacks are cancelled.
        """
        try:
            self._tasks.cancel_pending()
        except Exception:
            pass
        for attr in ("_fpgen_win", "_browse_fp_win", "_browse_sym_win", "_browse_cat_win"):
            try:
                w = getattr(self, attr, None)
            except Exception:
                w = None
            if w:
                try:
                    w.Destroy()
                except Exception:
                    pass
                try:
                    setattr(self, attr, None)
                except Exception:
                    pass
        try:
            self.Destroy()
        except Exception:
            pass

    def _append_log(self, text: str) -> None:
        if not text.endswith("\n"):
            text += "\n"
        self.log.AppendText(text)

    def _refresh_sync_status(self) -> None:
        try:
            st = git_sync_status(self._repo_path)
            stale = bool(st.get("stale"))
            dirty = bool(st.get("dirty"))
            if stale:
                age = st.get("age")
                suffix = f" (last fetch {format_age_minutes(age)})" if age is not None else ""
                self.sync_icon.SetBitmap(self._bmp_gray)
                self.sync_label.SetLabel("Library status: unknown / stale — click Fetch remote" + suffix)
            else:
                # Aggregate view:
                # - red if ANY category differs from origin/<branch> (or repo behind)
                # - else blue if ANY pending is applied_remote
                # - else yellow if local dirty OR pending exists
                # - else green
                br = (self._cfg.github_base_branch or "main").strip() or "main"

                try:
                    behind = int(st.get("behind") or 0)
                except Exception:
                    behind = 0
                try:
                    dirty = bool(st.get("dirty"))
                except Exception:
                    dirty = False

                any_pending = False
                any_applied = False
                try:
                    for cat_name, _items in (PENDING.items_by_category() or {}).items():
                        has_pend, applied = pending_tag_for_category(str(cat_name))
                        if has_pend:
                            any_pending = True
                        if applied:
                            any_applied = True
                except Exception:
                    pass

                any_red = False
                # If we have pending requests, do not show "out of date" just because
                # origin/<branch> advanced (request commit + CI commits). Legacy behavior
                # is yellow (pending) -> blue (applied remote) -> green (after sync).
                if behind > 0 and not (any_pending or any_applied):
                    any_red = True
                else:
                    # If not behind, check per-category diffs vs remote tracking branch.
                    try:
                        for cat in (self._categories or list_categories(self._repo_path)):
                            # If this category has ANY pending request, ignore its diff vs remote
                            # when computing global "out of date" status; we should show
                            # yellow/blue instead (legacy behavior: pending beats red).
                            try:
                                has_pend, _ap = pending_tag_for_category(str(getattr(cat, "display_name", "") or ""))
                            except Exception:
                                has_pend = False
                            if has_pend:
                                continue
                            changed = git_diff_name_status(self._repo_path, "HEAD", f"origin/{br}", [f"Database/{cat.filename}"])
                            if changed:
                                any_red = True
                                break
                    except Exception:
                        # If we can't compare, fall back to git_sync_status' out-of-date.
                        any_red = not bool(st.get("up_to_date"))

                if any_red:
                    self.sync_icon.SetBitmap(self._bmp_red)
                    if behind > 0:
                        self.sync_label.SetLabel(f"Library status: out of date (behind {behind})")
                    else:
                        self.sync_label.SetLabel("Library status: out of date")
                elif any_applied:
                    # If we're not behind, we're already synced; drop any applied_remote items
                    # so we don't show "sync needed" while up-to-date.
                    if behind <= 0 and bool(st.get("up_to_date")):
                        try:
                            for cat_name, _items in (PENDING.items_by_category() or {}).items():
                                try:
                                    drop_applied_pending_if_already_synced(self._repo_path, category_name=str(cat_name))
                                except Exception:
                                    continue
                        except Exception:
                            pass
                        any_pending = False
                        any_applied = False
                        try:
                            for cat_name, _items in (PENDING.items_by_category() or {}).items():
                                has_pend, applied2 = pending_tag_for_category(str(cat_name))
                                if has_pend:
                                    any_pending = True
                                if applied2:
                                    any_applied = True
                        except Exception:
                            pass
                        if not any_applied:
                            if dirty or any_pending:
                                self.sync_icon.SetBitmap(self._bmp_yellow)
                                self.sync_label.SetLabel("Library status: pending changes")
                            else:
                                self.sync_icon.SetBitmap(self._bmp_green)
                                self.sync_label.SetLabel(f"Library status: synchronized with origin/{br}")
                            self.sync_label.Wrap(max(200, self.GetClientSize().width - 80))
                            self.Layout()
                            return
                    self.sync_icon.SetBitmap(self._bmp_blue)
                    self.sync_label.SetLabel("Library status: sync needed")
                elif dirty or any_pending:
                    self.sync_icon.SetBitmap(self._bmp_yellow)
                    self.sync_label.SetLabel("Library status: pending changes")
                else:
                    self.sync_icon.SetBitmap(self._bmp_green)
                    self.sync_label.SetLabel(f"Library status: synchronized with origin/{br}")
            self.sync_label.Wrap(max(200, self.GetClientSize().width - 80))
            self.Layout()
        except Exception as exc:  # noqa: BLE001
            self.sync_icon.SetBitmap(self._bmp_gray)
            self.sync_label.SetLabel(f"Library status: unavailable ({exc})")

    def _refresh_assets_status(self) -> None:
        try:
            entries = git_status_entries(self._repo_path)
        except Exception:
            entries = []

        # Include untracked/ignored KiCad asset files not present in HEAD (robust "new asset" detection).
        local_all = sorted(local_asset_paths(self._repo_path, ["Symbols", "Footprints"]))
        local_fp = sorted(local_asset_paths(self._repo_path, ["Footprints"]))
        local_sym = sorted(local_asset_paths(self._repo_path, ["Symbols"]))

        age = git_fetch_head_age_seconds(self._repo_path)
        stale = (age is None) or (age > fetch_stale_threshold_seconds(self._repo_path))
        if stale:
            suffix = f" (last fetch {format_age_minutes(age)})" if age is not None else ""
            self.assets_icon.SetBitmap(self._bmp_yellow if local_all else self._bmp_red)
            self.assets_label.SetLabel(
                f"Local assets (uncommitted): {len(local_all)} changed — "
                f"Remote assets: unknown / stale — click Fetch remote{suffix}"
            )
            self._set_button_bitmap(self.browse_fp_btn, self._bmp_yellow if local_fp else self._bmp_gray)
            self._set_button_bitmap(self.browse_sym_btn, self._bmp_yellow if local_sym else self._bmp_gray)
            return

        def work() -> list[tuple[str, str]]:
            br = (self._cfg.github_base_branch or "main").strip() or "main"
            return git_diff_name_status(self._repo_path, "HEAD", f"origin/{br}", ["Symbols", "Footprints"])

        def done(res: list[tuple[str, str]] | None, err: Exception | None) -> None:
            if not is_window_alive(self):
                return
            if err is not None:
                self.assets_icon.SetBitmap(self._bmp_yellow if local_all else self._bmp_red)
                self.assets_label.SetLabel(
                    f"Local assets (uncommitted): {len(local_all)} changed — "
                    "Remote assets: unavailable"
                )
                return

            remote = list(res or [])
            remote_fp = [x for x in remote if x[1].startswith("Footprints/")]
            remote_sym = [x for x in remote if x[1].startswith("Symbols/")]

            if remote:
                self.assets_icon.SetBitmap(self._bmp_red)
            elif local_all:
                self.assets_icon.SetBitmap(self._bmp_yellow)
            else:
                self.assets_icon.SetBitmap(self._bmp_green)

            self.assets_label.SetLabel(
                f"Local assets (uncommitted): {len(local_all)} changed — "
                f"Remote assets: {len(remote)} changed"
            )
            self.assets_label.Wrap(max(200, self.GetClientSize().width - 80))

            if remote_fp:
                self._set_button_bitmap(self.browse_fp_btn, self._bmp_red)
            elif local_fp:
                self._set_button_bitmap(self.browse_fp_btn, self._bmp_yellow)
            else:
                self._set_button_bitmap(self.browse_fp_btn, self._bmp_green)

            if remote_sym:
                self._set_button_bitmap(self.browse_sym_btn, self._bmp_red)
            elif local_sym:
                self._set_button_bitmap(self.browse_sym_btn, self._bmp_yellow)
            else:
                self._set_button_bitmap(self.browse_sym_btn, self._bmp_green)

            self.Layout()

        self._tasks.run(work, done)

    def _refresh_categories_status_icon(self) -> None:
        """
        Update the icon next to `Manage categories…` to reflect *category* state only:
        - red: out of date vs origin/<branch> (ignoring pending category add/delete requests)
        - blue: at least one applied_remote pending category request (sync needed)
        - yellow: pending category request(s) submitted
        - green: categories up to date and no pending category requests
        - gray/red: stale/unknown
        """
        bmp = self._bmp_gray
        try:
            st = git_sync_status(self._repo_path)
        except Exception:
            st = {"stale": True}

        stale = bool(st.get("stale"))
        if stale:
            # Unknown without a recent fetch.
            bmp = self._bmp_gray
            try:
                self._set_button_bitmap(self.categories_btn, bmp)
            except Exception:
                pass
            return

        # Pending requests for any category (components + category add/delete):
        # worst-case blue > yellow, and pending beats red.
        any_pending = False
        any_applied = False
        try:
            for cat_name, items in (PENDING.items_by_category() or {}).items():
                name = str(cat_name or "").strip()
                if not name:
                    continue
                pend = list(items or [])
                if not pend:
                    continue
                any_pending = True
                if any(str(p.get("state") or "") == "applied_remote" for p in pend):
                    any_applied = True
        except Exception:
            pass

        # If any applied_remote, this is "sync needed" regardless of diffs.
        if any_applied:
            bmp = self._bmp_blue
            try:
                self._set_button_bitmap(self.categories_btn, bmp)
            except Exception:
                pass
            return
        if any_pending:
            bmp = self._bmp_yellow
            try:
                self._set_button_bitmap(self.categories_btn, bmp)
            except Exception:
                pass
            return

        # Otherwise compute remote diffs for Database CSVs, ignoring categories with ANY pending.
        try:
            br = (self._cfg.github_base_branch or "main").strip() or "main"
        except Exception:
            br = "main"
        any_red = False
        try:
            for cat in (self._categories or list_categories(self._repo_path)):
                name = str(getattr(cat, "display_name", "") or "").strip()
                if name:
                    try:
                        if PENDING.has_any(name):
                            continue
                    except Exception:
                        pass
                changed = git_diff_name_status(self._repo_path, "HEAD", f"origin/{br}", [f"Database/{cat.filename}"])
                if changed:
                    any_red = True
                    break
        except Exception:
            any_red = False

        bmp = self._bmp_red if any_red else self._bmp_green
        try:
            self._set_button_bitmap(self.categories_btn, bmp)
        except Exception:
            pass

    def _on_refresh_status(self, _evt: wx.CommandEvent | None) -> None:
        br = (self._cfg.github_base_branch or "main").strip() or "main"
        self._append_log("Fetching remote (background)...")
        try:
            self.sync_label.SetLabel("Fetching remote...")
        except Exception:
            pass
        try:
            self.refresh_btn.Enable(False)
            self.sync_btn.Enable(False)
        except Exception:
            pass

        def work() -> bool:
            run_git(["git", "fetch", "origin", br, "--quiet"], cwd=self._repo_path)
            return True

        def done(_res: bool | None, err: Exception | None) -> None:
            if not is_window_alive(self):
                return
            try:
                self.refresh_btn.Enable(True)
                self.sync_btn.Enable(True)
            except Exception:
                pass
            if err:
                self._append_log(f"Fetch remote failed: {err}")
                try:
                    wx.MessageBox(str(err), "Fetch remote failed", wx.OK | wx.ICON_WARNING)
                except Exception:
                    pass
            else:
                self._append_log(f"Fetched origin/{br} from remote.")
                # Update pending request states (yellow -> blue transitions) based on request file presence on origin/<branch>.
                try:
                    fm = git_fetch_head_mtime(self._repo_path)
                except Exception:
                    fm = None
                if fm is not None:
                    try:
                        for cat_name, _items in (PENDING.items_by_category() or {}).items():
                            try:
                                update_pending_states_after_fetch(self._repo_path, category_name=str(cat_name), branch=br, fetch_mtime=float(fm))
                            except Exception:
                                continue
                    except Exception:
                        pass
            self._refresh_sync_status()
            self._refresh_assets_status()
            self._reload_category_statuses()
            self._refresh_remote_cat_updated_times_async()
            self._refresh_categories_status_icon()
            self._refresh_selected_category_history_async()

        self._tasks.run(work, done)

    def _on_sync(self, _evt: wx.CommandEvent) -> None:
        self._append_log("Syncing library (background)...")
        try:
            self.sync_label.SetLabel("Syncing library...")
        except Exception:
            pass
        try:
            self.refresh_btn.Enable(False)
            self.sync_btn.Enable(False)
        except Exception:
            pass

        def work() -> str:
            br = (self._cfg.github_base_branch or "main").strip() or "main"
            entries = git_status_entries(self._repo_path)
            assets = paths_changed_under(entries, ["Symbols", "Footprints"])
            others = [p for _st, p in entries if p not in set(assets)]
            if others:
                preview = "\n".join(f"- {p}" for p in others[:20])
                raise RuntimeError(
                    "Local changes exist outside Symbols/ and Footprints/.\n"
                    "Please commit or revert them manually before syncing.\n\n" + preview
                )
            # If there are asset changes, we publish them first (legacy ui.py behavior).
            if assets:
                default = suggest_assets_commit_message(entries)
                # Prompt must happen on UI thread; we signal via exception and handle below.
                raise RuntimeError(f"__NEEDS_ASSET_PUBLISH__\n{default}")
            return git_sync_ff_only(self._repo_path, branch=br)

        def done(res: str | None, err: Exception | None) -> None:
            if not is_window_alive(self):
                return
            try:
                self.refresh_btn.Enable(True)
                self.sync_btn.Enable(True)
            except Exception:
                pass
            if err:
                # Special path: asset publish prompt.
                msg = str(err)
                if msg.startswith("__NEEDS_ASSET_PUBLISH__"):
                    default = msg.split("\n", 1)[1].strip() if "\n" in msg else "assets: update symbols/footprints"
                    cm = None
                    try:
                        from .requests import prompt_commit_message

                        cm = prompt_commit_message(self, default=default)
                    except Exception:
                        cm = None
                    if not cm:
                        self._append_log("Sync cancelled (assets not published).")
                        return

                    br = (self._cfg.github_base_branch or "main").strip() or "main"

                    def work_publish_and_sync() -> tuple[str, str]:
                        pub = git_commit_and_push_assets(self._repo_path, commit_message=cm, prefixes=["Symbols", "Footprints"], branch=br)
                        sync = git_sync_ff_only(self._repo_path, branch=br)
                        return (pub, sync)

                    def done_publish_and_sync(res2: tuple[str, str] | None, err2: Exception | None) -> None:
                        if not is_window_alive(self):
                            return
                        if err2:
                            self._append_log(f"Sync failed: {err2}")
                            wx.MessageBox(str(err2), "Sync failed", wx.OK | wx.ICON_WARNING)
                            return
                        pub_txt, sync_txt = res2 or ("", "")
                        if pub_txt:
                            self._append_log(pub_txt)
                        if sync_txt:
                            self._append_log(sync_txt)
                        # Clear pending items that have landed locally after sync.
                        try:
                            import csv as _csv

                            for cat in (self._categories or list_categories(self._repo_path)):
                                try:
                                    cat_name = str(getattr(cat, "display_name", "") or "").strip()
                                except Exception:
                                    continue
                                if not cat_name:
                                    continue
                                if not PENDING.has_any(cat_name):
                                    continue
                                local_by_ipn: dict[str, dict[str, str]] = {}
                                try:
                                    with open(cat.csv_path, "r", encoding="utf-8", newline="") as f:
                                        rdr = _csv.DictReader(f)
                                        for rr in rdr:
                                            ipn = str((rr or {}).get("IPN", "") or "").strip()
                                            if ipn:
                                                local_by_ipn[ipn] = dict(rr)
                                except Exception:
                                    local_by_ipn = {}
                                # Best-effort: resolve pending adds against local CSV (assign resolved_ipn)
                                # so reconcile can clear them after sync.
                                try:
                                    pend = PENDING.list_for(cat_name)
                                    pending_add = [p for p in (pend or []) if str(p.get("action") or "").strip() == "add"]
                                    if pending_add and local_by_ipn:
                                        match_keys = ["MPN", "Manufacturer", "Value", "Footprint", "Symbol", "Description"]

                                        def _fields_match(rr: dict[str, str], fields: dict[str, str]) -> bool:
                                            for k in match_keys:
                                                fv = str(fields.get(k, "") or "").strip()
                                                if not fv:
                                                    continue
                                                if str(rr.get(k, "") or "").strip() != fv:
                                                    return False
                                            return True

                                        changed = False
                                        updated_items: list[dict] = []
                                        for p in (pend or []):
                                            if str(p.get("action") or "").strip() != "add":
                                                updated_items.append(p)
                                                continue
                                            if str(p.get("resolved_ipn") or "").strip():
                                                updated_items.append(p)
                                                continue
                                            fields = dict(p.get("fields") or {})
                                            cands: list[str] = []
                                            for ipn, rr in local_by_ipn.items():
                                                try:
                                                    if _fields_match(rr, fields):
                                                        cands.append(str(ipn))
                                                except Exception:
                                                    continue
                                            if len(cands) == 1:
                                                pp = dict(p)
                                                pp["resolved_ipn"] = cands[0]
                                                updated_items.append(pp)
                                                changed = True
                                            else:
                                                updated_items.append(p)
                                        if changed:
                                            PENDING.set_items(cat_name, updated_items)
                                except Exception:
                                    pass
                                try:
                                    reconcile_pending_against_local_csv(self._repo_path, category_name=cat_name, local_by_ipn=local_by_ipn)
                                except Exception:
                                    continue
                        except Exception:
                            pass
                        # Also clear category-level pending (category_add/category_delete) once reflected locally.
                        try:
                            import os as _os

                            for cat_name, items in (PENDING.items_by_category() or {}).items():
                                pend = list(items or [])
                                if not pend:
                                    continue
                                kept: list[dict] = []
                                fn = f"db-{str(cat_name)}.csv"
                                p_csv = _os.path.join(self._repo_path, "Database", fn)
                                exists_local = _os.path.exists(p_csv)
                                for p in pend:
                                    act = str(p.get("action") or "").strip()
                                    if act not in ("category_add", "category_delete"):
                                        kept.append(p)
                                        continue
                                    # Only drop once CI applied it (applied_remote) AND the request file is gone from HEAD.
                                    st = str(p.get("state") or "").strip()
                                    rp = str(p.get("req_path") or "").strip()
                                    if st == "applied_remote" and rp:
                                        try:
                                            if not git_object_exists(self._repo_path, f"HEAD:{rp}"):
                                                # For add: category file should now exist; for delete: it should not.
                                                if (act == "category_add" and exists_local) or (act == "category_delete" and not exists_local):
                                                    continue
                                        except Exception:
                                            pass
                                    kept.append(p)
                                if kept != pend:
                                    PENDING.set_items(str(cat_name), kept)
                        except Exception:
                            pass
                        self._refresh_sync_status()
                        self._refresh_assets_status()
                        self._reload_category_statuses()
                        self._refresh_remote_cat_updated_times_async()
                        self._refresh_categories_status_icon()

                    self._tasks.run(work_publish_and_sync, done_publish_and_sync)
                    return

                self._append_log(f"Sync failed: {err}")
                wx.MessageBox(str(err), "Sync failed", wx.OK | wx.ICON_WARNING)
                return
            self._append_log(str(res or "Sync completed."))
            # Clear pending items that have landed locally after sync.
            try:
                import csv as _csv

                for cat in (self._categories or list_categories(self._repo_path)):
                    try:
                        cat_name = str(getattr(cat, "display_name", "") or "").strip()
                    except Exception:
                        continue
                    if not cat_name:
                        continue
                    if not PENDING.has_any(cat_name):
                        continue
                    local_by_ipn: dict[str, dict[str, str]] = {}
                    try:
                        with open(cat.csv_path, "r", encoding="utf-8", newline="") as f:
                            rdr = _csv.DictReader(f)
                            for rr in rdr:
                                ipn = str((rr or {}).get("IPN", "") or "").strip()
                                if ipn:
                                    local_by_ipn[ipn] = dict(rr)
                    except Exception:
                        local_by_ipn = {}
                    # Best-effort: resolve pending adds against local CSV (assign resolved_ipn)
                    # so reconcile can clear them after sync.
                    try:
                        pend = PENDING.list_for(cat_name)
                        pending_add = [p for p in (pend or []) if str(p.get("action") or "").strip() == "add"]
                        if pending_add and local_by_ipn:
                            match_keys = ["MPN", "Manufacturer", "Value", "Footprint", "Symbol", "Description"]

                            def _fields_match(rr: dict[str, str], fields: dict[str, str]) -> bool:
                                for k in match_keys:
                                    fv = str(fields.get(k, "") or "").strip()
                                    if not fv:
                                        continue
                                    if str(rr.get(k, "") or "").strip() != fv:
                                        return False
                                return True

                            changed = False
                            updated_items: list[dict] = []
                            for p in (pend or []):
                                if str(p.get("action") or "").strip() != "add":
                                    updated_items.append(p)
                                    continue
                                if str(p.get("resolved_ipn") or "").strip():
                                    updated_items.append(p)
                                    continue
                                fields = dict(p.get("fields") or {})
                                cands: list[str] = []
                                for ipn, rr in local_by_ipn.items():
                                    try:
                                        if _fields_match(rr, fields):
                                            cands.append(str(ipn))
                                    except Exception:
                                        continue
                                if len(cands) == 1:
                                    pp = dict(p)
                                    pp["resolved_ipn"] = cands[0]
                                    updated_items.append(pp)
                                    changed = True
                                else:
                                    updated_items.append(p)
                            if changed:
                                PENDING.set_items(cat_name, updated_items)
                    except Exception:
                        pass
                    try:
                        reconcile_pending_against_local_csv(self._repo_path, category_name=cat_name, local_by_ipn=local_by_ipn)
                    except Exception:
                        continue
            except Exception:
                pass
            # Also clear category-level pending (category_add/category_delete) once reflected locally.
            try:
                import os as _os

                for cat_name, items in (PENDING.items_by_category() or {}).items():
                    pend = list(items or [])
                    if not pend:
                        continue
                    kept: list[dict] = []
                    fn = f"db-{str(cat_name)}.csv"
                    p_csv = _os.path.join(self._repo_path, "Database", fn)
                    exists_local = _os.path.exists(p_csv)
                    for p in pend:
                        act = str(p.get("action") or "").strip()
                        if act not in ("category_add", "category_delete"):
                            kept.append(p)
                            continue
                        st = str(p.get("state") or "").strip()
                        rp = str(p.get("req_path") or "").strip()
                        if st == "applied_remote" and rp:
                            try:
                                if not git_object_exists(self._repo_path, f"HEAD:{rp}"):
                                    if (act == "category_add" and exists_local) or (act == "category_delete" and not exists_local):
                                        continue
                            except Exception:
                                pass
                        kept.append(p)
                    if kept != pend:
                        PENDING.set_items(str(cat_name), kept)
            except Exception:
                pass
            self._refresh_sync_status()
            self._refresh_assets_status()
            self._reload_category_statuses()
            self._refresh_remote_cat_updated_times_async()
            self._refresh_categories_status_icon()

        self._tasks.run(work, done)

    def _on_settings(self, _evt: wx.CommandEvent) -> None:
        # Pause background polling while settings are open to avoid re-entrancy
        # and focus-stealing dialogs being shown behind this modal.
        try:
            self._modal_settings_open = True
        except Exception:
            pass
        try:
            self._stop_remote_polling()
        except Exception:
            pass
        try:
            dlg = RepoSettingsDialog(self, self._cfg, repo_path=self._repo_path, project_path=self._project_path)
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            try:
                self._append_log("Settings dialog crashed while opening:\n" + tb)
            except Exception:
                pass
            try:
                wx.MessageBox(
                    "Settings crashed while opening:\n\n" + str(exc) + "\n\n" + tb,
                    "KiCad Library Manager",
                    wx.OK | wx.ICON_ERROR,
                )
            except Exception:
                pass
            return

        try:
            res = dlg.ShowModal()
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            try:
                self._append_log("Settings dialog crashed:\n" + tb)
            except Exception:
                pass
            try:
                wx.MessageBox(
                    "Settings crashed:\n\n" + str(exc) + "\n\n" + tb,
                    "KiCad Library Manager",
                    wx.OK | wx.ICON_ERROR,
                )
            except Exception:
                pass
            return
        finally:
            try:
                dlg.Destroy()
            except Exception:
                pass
            try:
                self._modal_settings_open = False
            except Exception:
                pass
            # Resume polling if appropriate.
            try:
                if self.IsShown() and not bool(getattr(self, "_setup_mode", False)):
                    self._start_remote_polling()
            except Exception:
                pass
        if res == wx.ID_OK:
            # Settings may affect origin URL/branch; refresh UI immediately.
            # First, reload global config to capture the latest local repo path choice.
            try:
                cfg_global = Config.load()
                new_rp = str(getattr(cfg_global, "repo_path", "") or "").strip()
            except Exception:
                new_rp = ""
            if new_rp and new_rp != str(getattr(self, "_repo_path", "") or "").strip():
                self._repo_path = new_rp
                try:
                    self._last_remote_sha = None
                except Exception:
                    pass
            # Then load effective config for the selected repo (applies per-library settings).
            try:
                self._cfg = Config.load_effective(self._repo_path)
            except Exception:
                pass
            # Re-evaluate whether repo is initialized; avoid crashes/freezes while setting up.
            try:
                self._apply_repo_state()
            except Exception:
                pass

    def _on_manage_categories(self, _evt: wx.CommandEvent) -> None:
        try:
            from .manage_categories_dialog import ManageCategoriesDialog

            dlg = ManageCategoriesDialog(self, self._repo_path)
            try:
                dlg.ShowModal()
            finally:
                dlg.Destroy()
            self._refresh_sync_status()
            self._reload_category_statuses()
            self._refresh_categories_status_icon()
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(f"Manage categories crashed:\n\n{exc}", "KiCad Library Manager", wx.OK | wx.ICON_ERROR)

    def _on_create_footprint(self, _evt: wx.CommandEvent) -> None:
        """
        Use the bundled generator from the IPC plugin bundle (no fallback).
        """
        try:
            try:
                importlib.invalidate_caches()
            except Exception:
                pass
            try:
                from kicad_footprint_generator.wx_gui import FootprintGeneratorDialog  # type: ignore
            except Exception:
                wx.MessageBox(
                    "Bundled footprint generator is unavailable.\n\n"
                    "This UI is configured to use only the bundled generator shipped with the IPC plugin bundle.\n\n"
                    "Fix:\n"
                    "- Install/symlink the plugin bundle folder `kicad_plugin/` into KiCad's IPC plugins directory\n"
                    "  (see README.md)\n"
                    "- Restart pcbnew\n\n"
                    f"Import error:\n{exc}",
                    "Create footprint",
                    wx.OK | wx.ICON_WARNING,
                )
                return

            # Open as a modal child dialog (like Settings / Manage categories).
            try:
                self._modal_settings_open = True
            except Exception:
                pass
            try:
                self._stop_remote_polling()
            except Exception:
                pass
            win = FootprintGeneratorDialog(self, self._repo_path)
            self._fpgen_win = win
            try:
                win.ShowModal()
            finally:
                try:
                    if getattr(self, "_fpgen_win", None) is win:
                        self._fpgen_win = None
                except Exception:
                    pass
                try:
                    win.Destroy()
                except Exception:
                    pass
                try:
                    self._modal_settings_open = False
                except Exception:
                    pass
                try:
                    if self.IsShown() and not bool(getattr(self, "_setup_mode", False)):
                        self._start_remote_polling()
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(f"Create footprint crashed:\n\n{exc}", "KiCad Library Manager", wx.OK | wx.ICON_ERROR)

    def _on_browse_footprints(self, _evt: wx.CommandEvent) -> None:
        try:
            from .footprints.browser_dialog import FootprintBrowserDialog

            dlg = FootprintBrowserDialog(self, self._repo_path)
            self._browse_fp_win = dlg
            try:
                self._modal_settings_open = True
            except Exception:
                pass
            try:
                self._stop_remote_polling()
            except Exception:
                pass
            try:
                dlg.ShowModal()
            finally:
                try:
                    if getattr(self, "_browse_fp_win", None) is dlg:
                        self._browse_fp_win = None
                except Exception:
                    pass
                try:
                    dlg.Destroy()
                except Exception:
                    pass
                try:
                    self._modal_settings_open = False
                except Exception:
                    pass
                try:
                    if self.IsShown() and not bool(getattr(self, "_setup_mode", False)):
                        self._start_remote_polling()
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(f"Browse footprints crashed:\n\n{exc}", "KiCad Library Manager", wx.OK | wx.ICON_ERROR)

    def _on_browse_symbols(self, _evt: wx.CommandEvent) -> None:
        try:
            from .symbols.browser_dialog import SymbolBrowserDialog

            dlg = SymbolBrowserDialog(self, self._repo_path)
            self._browse_sym_win = dlg
            try:
                self._modal_settings_open = True
            except Exception:
                pass
            try:
                self._stop_remote_polling()
            except Exception:
                pass
            try:
                dlg.ShowModal()
            finally:
                try:
                    if getattr(self, "_browse_sym_win", None) is dlg:
                        self._browse_sym_win = None
                except Exception:
                    pass
                try:
                    dlg.Destroy()
                except Exception:
                    pass
                try:
                    self._modal_settings_open = False
                except Exception:
                    pass
                try:
                    if self.IsShown() and not bool(getattr(self, "_setup_mode", False)):
                        self._start_remote_polling()
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(f"Browse symbols crashed:\n\n{exc}", "KiCad Library Manager", wx.OK | wx.ICON_ERROR)

    def _on_category_dclick(self, _evt: wx.CommandEvent) -> None:
        idx = self.cat_list.GetFirstSelected()
        items = getattr(self, "_cat_row_items", None) or []
        if idx < 0 or idx >= len(items):
            return
        cat = items[idx]
        # Pending category placeholder rows may not exist locally yet.
        try:
            p = str(getattr(cat, "csv_path", "") or "").strip()
        except Exception:
            p = ""
        if p and not os.path.exists(p):
            wx.MessageBox(
                "This category is a pending request and does not exist locally yet.\n\n"
                "Fetch remote to see when it is applied, then Sync library to pull it locally.",
                "Pending category",
                wx.OK | wx.ICON_INFORMATION,
            )
            return
        try:
            # Open as a modal child dialog (subwindow) like Settings / Manage categories.
            try:
                self._modal_settings_open = True
            except Exception:
                pass
            try:
                self._stop_remote_polling()
            except Exception:
                pass
            frm = BrowseDialog(self, self._repo_path, cat)
            self._browse_cat_win = frm
            try:
                frm.ShowModal()
            finally:
                try:
                    if getattr(self, "_browse_cat_win", None) is frm:
                        self._browse_cat_win = None
                except Exception:
                    pass
                try:
                    frm.Destroy()
                except Exception:
                    pass
                try:
                    self._modal_settings_open = False
                except Exception:
                    pass
                try:
                    if self.IsShown() and not bool(getattr(self, "_setup_mode", False)):
                        self._start_remote_polling()
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(
                "Browse category crashed:\n\n" + str(exc) + "\n\n" + traceback.format_exc(),
                "KiCad Library Manager",
                wx.OK | wx.ICON_ERROR,
            )

    def _on_cat_col_click(self, evt: wx.ListEvent) -> None:
        try:
            col = int(evt.GetColumn())
        except Exception:
            col = -1
        if col <= 0:
            return
        if int(getattr(self, "_cat_sort_col_idx", 1) or 1) == col:
            self._cat_sort_asc = not bool(getattr(self, "_cat_sort_asc", True))
        else:
            self._cat_sort_col_idx = col
            self._cat_sort_asc = True
        self._reload_category_statuses()

    def _refresh_remote_cat_updated_times_async(self) -> None:
        """
        Compute per-category last updated time from cached origin/<branch> (background).
        Mirrors ui.py logic: only meaningful when FETCH_HEAD is fresh enough.
        """
        if bool(getattr(self, "_remote_cat_updated_loading", False)):
            return
        age = git_fetch_head_age_seconds(self._repo_path)
        stale = (age is None) or (age > fetch_stale_threshold_seconds(self._repo_path))
        if stale:
            self._remote_cat_updated_ts_by_path = {}
            return

        paths: list[str] = []
        for cat in (self._categories or list_categories(self._repo_path)):
            fn = (getattr(cat, "filename", "") or "").strip()
            if fn:
                paths.append(f"Database/{fn}")
        paths = sorted(set([p for p in paths if p]))
        if not paths:
            self._remote_cat_updated_ts_by_path = {}
            return

        self._remote_cat_updated_loading = True

        def work() -> dict[str, int]:
            br = (self._cfg.github_base_branch or "main").strip() or "main"
            return git_last_updated_epoch_by_path(self._repo_path, paths, ref=f"origin/{br}")

        def done(res: dict[str, int] | None, err: Exception | None) -> None:
            self._remote_cat_updated_loading = False
            if not is_window_alive(self):
                return
            if err or not isinstance(res, dict):
                return
            try:
                self._remote_cat_updated_ts_by_path = dict(res or {})
            except Exception:
                self._remote_cat_updated_ts_by_path = {}
            self._reload_category_statuses()

        self._tasks.run(work, done)

    def _reload_category_statuses(self) -> None:
        # Preserve selection and scroll position across reloads (ui.py behavior).
        sel_idx = self.cat_list.GetFirstSelected()
        selected_name = None
        if sel_idx != -1:
            selected_name = self.cat_list.GetItemText(sel_idx, 1)
        try:
            top_idx = int(self.cat_list.GetTopItem())
        except Exception:
            top_idx = 0
        try:
            per_page = int(self.cat_list.GetCountPerPage())
        except Exception:
            per_page = 0
        was_visible_min = top_idx
        was_visible_max = (top_idx + max(per_page - 1, 0)) if per_page else top_idx

        self._categories = list_categories(self._repo_path)
        self.cat_list.Freeze()
        self.cat_list.DeleteAllItems()

        def _fmt_remote_ts(ts: int | None) -> str:
            if not ts:
                return ""
            try:
                return _dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
            except Exception:
                return ""

        rows: list[dict] = []
        existing_names: set[str] = set()
        for cat in self._categories:
            try:
                existing_names.add(str(getattr(cat, "display_name", "") or "").strip())
            except Exception:
                pass
            status_ok = True
            status_text = "up to date"
            try:
                br = (self._cfg.github_base_branch or "main").strip() or "main"
                changed = git_diff_name_status(self._repo_path, "HEAD", f"origin/{br}", [f"Database/{cat.filename}"])
                if changed:
                    status_ok = False
                    status_text = "out of date"
            except Exception:
                status_ok = False
                status_text = "unknown"

            updated_ts = None
            try:
                p = f"Database/{cat.filename}"
                updated_ts = int(self._remote_cat_updated_ts_by_path.get(p) or 0) or None
            except Exception:
                updated_ts = None

            rows.append(
                {
                    "item": cat,
                    "cat": category_title(cat),
                    "status": status_text,
                    "updated": _fmt_remote_ts(updated_ts),
                    "updated_ts": updated_ts,
                    "img": self.cat_img_green if status_ok else self.cat_img_red,
                }
            )
            # Pending overrides green/red at the category level (legacy behavior).
            try:
                has_pend, applied = pending_tag_for_category(cat.display_name)
            except Exception:
                has_pend, applied = (False, False)
            if has_pend:
                rows[-1]["img"] = self.cat_img_blue if applied else self.cat_img_yellow
                # If there is a category-level request (add/delete), the remote comparison
                # isn't meaningful; show pending/sync-needed directly.
                try:
                    pend_items = PENDING.list_for(cat.display_name)
                    has_cat_req = any(str(p.get("action") or "") in {"category_add", "category_delete"} for p in (pend_items or []))
                except Exception:
                    has_cat_req = False
                tag = "sync needed" if applied else "pending"
                rows[-1]["status"] = tag if has_cat_req else f"{status_text} ({tag})"

        # Pending category adds/deletes that are not yet present locally (show placeholders like ui.py).
        try:
            for cat_name, pend in sorted((PENDING.items_by_category() or {}).items(), key=lambda kv: str(kv[0] or "").lower()):
                name = str(cat_name or "").strip()
                if not name or name in existing_names:
                    continue
                items = list(pend or [])
                if not items:
                    continue
                if not any(str(p.get("action") or "") in {"category_add", "category_delete"} for p in items):
                    continue
                applied = any(str(p.get("state") or "") == "applied_remote" for p in items)
                img = self.cat_img_blue if applied else self.cat_img_yellow
                # Keep wording consistent with existing categories: pending -> sync needed.
                status_txt = "sync needed" if applied else "pending"
                # Create a placeholder Category-like object.
                fn = f"db-{name}.csv"
                try:
                    csv_path = os.path.join(self._repo_path, "Database", fn)
                except Exception:
                    csv_path = ""
                placeholder = Category(filename=fn, display_name=name, csv_path=csv_path)
                rows.append(
                    {
                        "item": placeholder,
                        "cat": name,
                        "status": status_txt,
                        "updated": "",
                        "updated_ts": None,
                        "img": img,
                    }
                )
        except Exception:
            pass

        # Sort rows (ported from ui.py)
        _num_re = _re.compile(r"(\d+)")

        def _nat_key(s: str) -> list[object]:
            parts = _num_re.split((s or "").strip().lower())
            outk: list[object] = []
            for p in parts:
                if not p:
                    continue
                if p.isdigit():
                    try:
                        outk.append(int(p))
                    except Exception:
                        outk.append(p)
                else:
                    outk.append(p)
            return outk

        col = int(getattr(self, "_cat_sort_col_idx", 1) or 1)
        asc = bool(getattr(self, "_cat_sort_asc", True))

        def _row_key(r: dict) -> tuple[int, list[object]]:
            if col == 1:
                v = str(r.get("cat") or "")
            elif col == 2:
                v = str(r.get("status") or "")
            elif col == 3:
                ts = r.get("updated_ts")
                try:
                    ts_i = int(ts) if ts else 0
                except Exception:
                    ts_i = 0
                return (1 if not ts_i else 0, [ts_i])
            else:
                v = str(r.get("cat") or "")
            v = v.strip()
            return (1 if not v else 0, _nat_key(v))

        try:
            rows.sort(key=_row_key, reverse=not asc)
        except Exception:
            rows.sort(key=lambda r: str(r.get("cat") or "").lower())

        reselect_idx = None
        row_items: list[Category] = []
        for r in rows:
            row_items.append(r["item"])
            row = self.cat_list.InsertItem(self.cat_list.GetItemCount(), "", r["img"])
            self.cat_list.SetItem(row, 1, str(r.get("cat") or ""))
            self.cat_list.SetItem(row, 2, str(r.get("status") or ""))
            self.cat_list.SetItem(row, 3, str(r.get("updated") or ""))
            if selected_name and str(r.get("cat") or "") == selected_name:
                reselect_idx = row

        if reselect_idx is not None:
            self.cat_list.Select(reselect_idx)
            if not (was_visible_min <= reselect_idx <= was_visible_max):
                self.cat_list.EnsureVisible(reselect_idx)

        if not self._did_autosize_cat_cols:
            self.cat_list.SetColumnWidth(0, 30)
            self.cat_list.SetColumnWidth(1, wx.LIST_AUTOSIZE_USEHEADER)
            self.cat_list.SetColumnWidth(2, wx.LIST_AUTOSIZE_USEHEADER)
            self.cat_list.SetColumnWidth(3, wx.LIST_AUTOSIZE_USEHEADER)
            self._did_autosize_cat_cols = True

        if self.cat_list.GetItemCount() > 0 and top_idx >= 0:
            self.cat_list.EnsureVisible(min(top_idx, self.cat_list.GetItemCount() - 1))

        self._cat_row_items = row_items
        self.cat_list.Thaw()
        self._refresh_selected_category_history_async()

    def _selected_category(self) -> Category | None:
        try:
            idx = int(self.cat_list.GetFirstSelected())
        except Exception:
            idx = -1
        if idx < 0:
            return None
        items = getattr(self, "_cat_row_items", None) or []
        if 0 <= idx < len(items):
            return items[idx]
        return None

    def _on_category_selected(self, _evt: wx.ListEvent) -> None:
        self._refresh_selected_category_history_async()

    def _refresh_selected_category_history_async(self) -> None:
        cat = self._selected_category()
        if not cat:
            self._hist_for_path = None
            self._hist_title.SetLabel("History")
            self._set_history_rows([])
            return
        rel_path = f"Database/{cat.filename}"
        self._hist_for_path = rel_path
        self._hist_title.SetLabel(f"History: {category_title(cat)}")

        try:
            br = (self._cfg.github_base_branch or "main").strip() or "main"
            pref_ref = f"origin/{br}"
        except Exception:
            pref_ref = None

        def work() -> list[dict[str, str]]:
            # Prefer remote-tracking history; fall back to local HEAD if not fetched yet.
            try:
                if pref_ref:
                    return git_log_last_commits_for_path(self._repo_path, rel_path, n=10, ref=pref_ref)
            except Exception:
                pass
            return git_log_last_commits_for_path(self._repo_path, rel_path, n=10, ref=None)

        def done(res: list[dict[str, str]] | None, err: Exception | None) -> None:
            if not is_window_alive(self):
                return
            if err:
                self._append_log(f"History load failed: {err}")
                self._set_history_rows([])
                return
            self._set_history_rows(list(res or []))

        self._set_history_rows([])  # clear immediately to indicate refresh
        self._tasks.run(work, done)

    def _set_history_rows(self, rows: list[dict[str, str]]) -> None:
        self._hist_rows = list(rows or [])
        try:
            self._hist_list.DeleteAllItems()
        except Exception:
            pass
        for r in (self._hist_rows or []):
            dt = (r.get("date") or "").strip()
            author = (r.get("author") or "").strip()
            subj = (r.get("subject") or "").strip()
            # Keep rows simple; store SHA in our side array.
            try:
                self._hist_list.AppendItem([dt, author, subj])
            except Exception:
                pass
        try:
            self._hist_show_btn.Enable(False)
        except Exception:
            pass

    def _on_hist_selection_changed(self, _evt: dv.DataViewEvent) -> None:
        try:
            row = int(self._hist_list.GetSelectedRow())
        except Exception:
            row = -1
        ok = 0 <= row < len(getattr(self, "_hist_rows", []) or [])
        try:
            self._hist_show_btn.Enable(bool(ok) and bool(self._hist_for_path))
        except Exception:
            pass

    def _on_hist_item_activated(self, _evt: dv.DataViewEvent) -> None:
        # Double-click (or Enter) opens diff for the selected commit.
        self._on_hist_show_diff(wx.CommandEvent())

    def _on_hist_show_diff(self, _evt: wx.CommandEvent) -> None:
        rel_path = getattr(self, "_hist_for_path", None)
        if not rel_path:
            return
        try:
            row = int(self._hist_list.GetSelectedRow())
        except Exception:
            row = -1
        rows = getattr(self, "_hist_rows", []) or []
        if not (0 <= row < len(rows)):
            return
        sha = (rows[row].get("sha") or "").strip()
        if not sha:
            return
        meta = dict(rows[row] or {})

        self._append_log(f"Loading diff for {sha[:8]} {rel_path} (background)...")

        def work() -> dict[str, str]:
            out: dict[str, str] = {}
            out["patch"] = git_show_commit_for_path(self._repo_path, sha, rel_path)
            # For CSV viewer: fetch before/after file snapshots.
            p = str(rel_path or "").replace(os.sep, "/")
            try:
                out["after"] = run_git(["git", "-C", self._repo_path, "show", f"{sha}:{p}"], cwd=self._repo_path)
            except Exception:
                out["after"] = ""
            try:
                out["before"] = run_git(["git", "-C", self._repo_path, "show", f"{sha}^:{p}"], cwd=self._repo_path)
            except Exception:
                out["before"] = ""
            return out

        def done(res: dict[str, str] | None, err: Exception | None) -> None:
            if not is_window_alive(self):
                return
            if err:
                wx.MessageBox(str(err), "Show diff failed", wx.OK | wx.ICON_WARNING)
                return
            res = dict(res or {})
            txt0 = str(res.get("patch") or "")
            # Prefer a structured view for CSV diffs.
            # Important: we use before/after snapshots (not patch parsing) so it works even when
            # CSV rows contain embedded newlines (multi-line quoted fields).
            if str(rel_path or "").lower().endswith(".csv"):
                dlg = _CsvDiffDialog(
                    self,
                    title=f"Diff {sha[:8]} — {rel_path}",
                    sha=sha,
                    meta=meta,
                    rel_path=rel_path,
                    parsed=_try_parse_single_csv_unified_diff(txt0) or {},
                    before_text=str(res.get("before") or ""),
                    after_text=str(res.get("after") or ""),
                    raw_text=txt0,
                )
                try:
                    dlg.ShowModal()
                finally:
                    dlg.Destroy()
                return

            dlg = wx.Dialog(self, title=f"Diff {sha[:8]} — {rel_path}", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            s = wx.BoxSizer(wx.VERTICAL)
            txt = wx.TextCtrl(dlg, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2)
            txt.SetValue(txt0)
            s.Add(txt, 1, wx.ALL | wx.EXPAND, 8)
            btns = dlg.CreateStdDialogButtonSizer(wx.OK)
            if btns:
                s.Add(btns, 0, wx.ALL | wx.EXPAND, 8)
            dlg.SetSizer(s)
            dlg.SetSize((950, 700))
            try:
                dlg.ShowModal()
            finally:
                dlg.Destroy()

        self._tasks.run(work, done)


def _try_parse_single_csv_unified_diff(git_show_text: str) -> dict | None:
    """
    Best-effort parse of `git show` output for a single CSV file diff.

    Returns:
      {
        "headers": [...],
        "added": {ipn: rowdict},
        "removed": {ipn: rowdict},
      }
    or None if we can't parse.
    """
    lines = (git_show_text or "").splitlines()
    if not lines:
        return None

    # Extract diff body.
    try:
        i0 = next(i for i, s in enumerate(lines) if s.startswith("diff --git "))
        body = lines[i0:]
    except Exception:
        body = lines

    removed_raw: list[str] = []
    added_raw: list[str] = []
    for s in body:
        if not s:
            continue
        if s.startswith("--- ") or s.startswith("+++ ") or s.startswith("@@") or s.startswith("diff --git "):
            continue
        if s.startswith("index ") or s.startswith("new file") or s.startswith("deleted file"):
            continue
        if s[0] == "-":
            removed_raw.append(s[1:])
        elif s[0] == "+":
            added_raw.append(s[1:])

    # Find header line (prefer the actual CSV header).
    header_line = ""
    for cand in (removed_raw + added_raw):
        if (cand or "").strip().startswith("IPN,"):
            header_line = cand
            break
    if not header_line:
        return None

    try:
        headers = next(csv.reader(io.StringIO(header_line)))
        headers = [str(h or "").strip() for h in (headers or [])]
    except Exception:
        return None
    if not headers or "IPN" not in headers:
        return None

    def parse_row_line(line: str) -> dict[str, str] | None:
        try:
            fields = next(csv.reader(io.StringIO(line)))
        except Exception:
            return None
        if len(fields) != len(headers):
            return None
        out: dict[str, str] = {}
        for h, v in zip(headers, fields, strict=False):
            out[str(h)] = str(v or "")
        return out

    removed_by_ipn: dict[str, dict[str, str]] = {}
    for ln in removed_raw:
        if ln == header_line:
            continue
        r = parse_row_line(ln)
        if not r:
            continue
        ipn = str(r.get("IPN", "") or "").strip()
        if ipn:
            removed_by_ipn[ipn] = r

    added_by_ipn: dict[str, dict[str, str]] = {}
    for ln in added_raw:
        if ln == header_line:
            continue
        r = parse_row_line(ln)
        if not r:
            continue
        ipn = str(r.get("IPN", "") or "").strip()
        if ipn:
            added_by_ipn[ipn] = r

    if not removed_by_ipn and not added_by_ipn:
        return None
    return {"headers": headers, "added": added_by_ipn, "removed": removed_by_ipn}


def _merge_csv_lines_for_full_view(before_lines: list[str], after_lines: list[str]) -> list[tuple[str, str, list[str]]]:
    """
    Build a full-file view where diffs are placed in context.

    Returns a list of (kind, ipn, cells) where:
    - kind: "H" header, "=" unchanged, "+" added, "-" removed
    - ipn: extracted IPN if possible (empty if unknown)
    - cells: CSV cells (no leading +/-)
    """
    b = [str(x or "") for x in (before_lines or [])]
    a = [str(x or "") for x in (after_lines or [])]
    # Header is expected to be line 0 (same format both sides).
    header = ""
    if a:
        header = a[0]
    elif b:
        header = b[0]

    def parse_cells(line: str) -> list[str]:
        try:
            return [str(x or "") for x in next(csv.reader(io.StringIO(line)))]
        except Exception:
            return [line]

    hdr_cells = parse_cells(header) if header else []

    # If header exists, remove it from line-level diff.
    if b and header and b[0] == header:
        b_body = b[1:]
    else:
        b_body = b[1:] if b else []
    if a and header and a[0] == header:
        a_body = a[1:]
    else:
        a_body = a[1:] if a else []

    # Diff by raw line to preserve ordering.
    sm = difflib.SequenceMatcher(a=b_body, b=a_body, autojunk=False)
    out: list[tuple[str, str, list[str]]] = []
    if hdr_cells:
        out.append(("H", "", hdr_cells))

    # Determine IPN column index if possible.
    try:
        ipn_idx = hdr_cells.index("IPN") if "IPN" in hdr_cells else -1
    except Exception:
        ipn_idx = -1

    def ipn_for_cells(cells: list[str]) -> str:
        if ipn_idx >= 0 and ipn_idx < len(cells):
            return str(cells[ipn_idx] or "").strip()
        return ""

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for ln in a_body[j1:j2]:
                cells = parse_cells(ln)
                out.append(("=", ipn_for_cells(cells), cells))
        elif tag == "delete":
            for ln in b_body[i1:i2]:
                cells = parse_cells(ln)
                out.append(("-", ipn_for_cells(cells), cells))
        elif tag == "insert":
            for ln in a_body[j1:j2]:
                cells = parse_cells(ln)
                out.append(("+", ipn_for_cells(cells), cells))
        else:  # replace
            for ln in b_body[i1:i2]:
                cells = parse_cells(ln)
                out.append(("-", ipn_for_cells(cells), cells))
            for ln in a_body[j1:j2]:
                cells = parse_cells(ln)
                out.append(("+", ipn_for_cells(cells), cells))
    return out


def _merge_csv_records_for_full_view(before_text: str, after_text: str) -> list[tuple[str, str, list[str]]]:
    """
    Full-file CSV view with diffs in context, but at *record* granularity.
    This correctly handles CSV records with embedded newlines (quoted fields).

    Returns list of (kind, ipn, cells) where kind is:
    - "H" header
    - "=" unchanged row
    - "+" added row
    - "-" removed row
    """

    def parse_records(txt: str) -> list[list[str]]:
        if not (txt or "").strip():
            return []
        try:
            rdr = csv.reader(io.StringIO(txt))
            return [[str(x or "") for x in row] for row in rdr]
        except Exception:
            return [[ln] for ln in str(txt or "").splitlines()]

    before_recs = parse_records(before_text)
    after_recs = parse_records(after_text)
    header = after_recs[0] if after_recs else (before_recs[0] if before_recs else [])
    b_body = before_recs[1:] if len(before_recs) > 0 else []
    a_body = after_recs[1:] if len(after_recs) > 0 else []

    out: list[tuple[str, str, list[str]]] = []
    if header:
        out.append(("H", "", list(header)))

    try:
        ipn_idx = header.index("IPN") if header and "IPN" in header else -1
    except Exception:
        ipn_idx = -1

    def ipn_for(cells: list[str]) -> str:
        if ipn_idx >= 0 and ipn_idx < len(cells):
            return str(cells[ipn_idx] or "").strip()
        return ""

    b_keys = [tuple(r) for r in b_body]
    a_keys = [tuple(r) for r in a_body]
    sm = difflib.SequenceMatcher(a=b_keys, b=a_keys, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for rec in a_body[j1:j2]:
                out.append(("=", ipn_for(rec), list(rec)))
        elif tag == "delete":
            for rec in b_body[i1:i2]:
                out.append(("-", ipn_for(rec), list(rec)))
        elif tag == "insert":
            for rec in a_body[j1:j2]:
                out.append(("+", ipn_for(rec), list(rec)))
        else:  # replace
            for rec in b_body[i1:i2]:
                out.append(("-", ipn_for(rec), list(rec)))
            for rec in a_body[j1:j2]:
                out.append(("+", ipn_for(rec), list(rec)))
    return out


class _CsvDiffDialog(wx.Dialog):
    def __init__(
        self,
        parent: wx.Window,
        *,
        title: str,
        sha: str,
        meta: dict,
        rel_path: str,
        parsed: dict,
        before_text: str,
        after_text: str,
        raw_text: str,
    ):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX)
        self._sha = str(sha or "")
        self._meta = dict(meta or {})
        self._rel_path = str(rel_path or "")
        self._parsed = dict(parsed or {})
        self._before_text = str(before_text or "")
        self._after_text = str(after_text or "")
        self._raw_text = str(raw_text or "")

        root = wx.BoxSizer(wx.VERTICAL)

        dt = str(self._meta.get("date") or "").strip()
        author = str(self._meta.get("author") or "").strip()
        subj = str(self._meta.get("subject") or "").strip()
        hdr = wx.StaticText(self, label=f"{self._sha[:8]}  {dt}  {author}\n{subj}")
        root.Add(hdr, 0, wx.ALL | wx.EXPAND, 8)

        self._summary = wx.StaticText(self, label="")
        root.Add(self._summary, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        # Row-based CSV diff grid:
        # - green rows: additions
        # - red rows: deletions
        # - edits: a red (old) row followed by a green (new) row
        self._grid = gridlib.Grid(self)
        self._grid.CreateGrid(0, 1)
        try:
            self._grid.EnableEditing(False)
        except Exception:
            pass
        try:
            self._grid.SetRowLabelSize(0)
        except Exception:
            pass
        root.Add(self._grid, 1, wx.ALL | wx.EXPAND, 8)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        raw_btn = wx.Button(self, label="View raw diff")
        close_btn = wx.Button(self, id=wx.ID_OK, label="Close")
        btns.AddStretchSpacer(1)
        btns.Add(raw_btn, 0, wx.ALL, 6)
        btns.Add(close_btn, 0, wx.ALL, 6)
        root.Add(btns, 0, wx.EXPAND)

        self.SetSizer(root)
        self.SetSize((1400, 900))
        self.SetMinSize((980, 650))

        raw_btn.Bind(wx.EVT_BUTTON, self._on_raw)

        self._rebuild()

    def _on_raw(self, _evt: wx.CommandEvent) -> None:
        dlg = wx.Dialog(self, title="Raw diff", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX)
        s = wx.BoxSizer(wx.VERTICAL)
        txt = wx.TextCtrl(dlg, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2)
        txt.SetValue(self._raw_text)
        s.Add(txt, 1, wx.ALL | wx.EXPAND, 8)
        btns = dlg.CreateStdDialogButtonSizer(wx.OK)
        if btns:
            s.Add(btns, 0, wx.ALL | wx.EXPAND, 8)
        dlg.SetSizer(s)
        dlg.SetSize((950, 700))
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

    def _rebuild(self) -> None:
        # Filter UI removed: always show full diff context.
        flt = ""
        headers: list[str] = list(self._parsed.get("headers") or [])
        added: dict[str, dict[str, str]] = dict(self._parsed.get("added") or {})
        removed: dict[str, dict[str, str]] = dict(self._parsed.get("removed") or {})

        add_ipns = set(added.keys()) - set(removed.keys())
        del_ipns = set(removed.keys()) - set(added.keys())
        both_ipns = set(added.keys()) & set(removed.keys())

        # Identify modified IPNs (any column differs).
        mod_ipns: set[str] = set()
        for ipn in list(both_ipns):
            a = added.get(ipn) or {}
            b = removed.get(ipn) or {}
            for col in headers:
                if str(col) == "IPN":
                    continue
                if str((a.get(col, "") or "")).strip() != str((b.get(col, "") or "")).strip():
                    mod_ipns.add(ipn)
                    break

        # Build display entries: (kind, ipn, rowdict)
        # kind in {"-", "+", "sep"}; edits are "-" then "+".
        entries: list[tuple[str, str, dict[str, str]]] = []
        for ipn in sorted(del_ipns):
            entries.append(("-", ipn, dict(removed.get(ipn) or {})))
        for ipn in sorted(add_ipns):
            entries.append(("+", ipn, dict(added.get(ipn) or {})))
        for ipn in sorted(mod_ipns):
            entries.append(("-", ipn, dict(removed.get(ipn) or {})))
            entries.append(("+", ipn, dict(added.get(ipn) or {})))

        # (filter removed)

        # Grid columns: Δ + headers
        col_labels = ["Δ"] + [str(h or "") for h in headers]
        want_cols = len(col_labels)
        try:
            cur_cols = int(self._grid.GetNumberCols())
        except Exception:
            cur_cols = 0
        if cur_cols != want_cols:
            try:
                if cur_cols > want_cols:
                    self._grid.DeleteCols(0, cur_cols - want_cols)
                elif cur_cols < want_cols:
                    self._grid.AppendCols(want_cols - cur_cols)
            except Exception:
                pass
        for ci, lab in enumerate(col_labels):
            try:
                self._grid.SetColLabelValue(ci, lab)
            except Exception:
                pass
        try:
            self._grid.SetColSize(0, 40)
        except Exception:
            pass

        # Resize rows to match entries.
        try:
            cur_rows = int(self._grid.GetNumberRows())
        except Exception:
            cur_rows = 0
        want_rows = len(entries)
        try:
            if cur_rows > want_rows:
                self._grid.DeleteRows(0, cur_rows - want_rows)
            elif cur_rows < want_rows:
                self._grid.AppendRows(want_rows - cur_rows)
        except Exception:
            pass

        # Darker backgrounds (better contrast with light text in dark themes).
        add_bg = wx.Colour(22, 92, 44)
        del_bg = wx.Colour(110, 28, 28)
        try:
            norm_bg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
        except Exception:
            norm_bg = wx.Colour(255, 255, 255)
        add_fg = wx.Colour(245, 245, 245)
        del_fg = wx.Colour(245, 245, 245)

        # Prefer showing the full file with diffs placed in context when we have both snapshots.
        # Use record-level parsing to handle embedded newlines inside quoted CSV fields.
        if (self._before_text or "").strip() or (self._after_text or "").strip():
            entries = _merge_csv_records_for_full_view(self._before_text, self._after_text)
            # (filter removed)

            # If we have a header row, use it.
            if entries and entries[0][0] == "H":
                hdr_cells = entries[0][2]
                headers = list(hdr_cells)
                entries = entries[1:]

            # Grid columns: Δ + headers
            col_labels = ["Δ"] + [str(h or "") for h in headers]
            want_cols = len(col_labels)
            try:
                cur_cols = int(self._grid.GetNumberCols())
            except Exception:
                cur_cols = 0
            if cur_cols != want_cols:
                try:
                    if cur_cols > want_cols:
                        self._grid.DeleteCols(0, cur_cols - want_cols)
                    elif cur_cols < want_cols:
                        self._grid.AppendCols(want_cols - cur_cols)
                except Exception:
                    pass
            for ci, lab in enumerate(col_labels):
                try:
                    self._grid.SetColLabelValue(ci, lab)
                except Exception:
                    pass
            try:
                self._grid.SetColSize(0, 40)
            except Exception:
                pass

            # Resize rows to match entries.
            try:
                cur_rows = int(self._grid.GetNumberRows())
            except Exception:
                cur_rows = 0
            want_rows = len(entries)
            try:
                if cur_rows > want_rows:
                    self._grid.DeleteRows(0, cur_rows - want_rows)
                elif cur_rows < want_rows:
                    self._grid.AppendRows(want_rows - cur_rows)
            except Exception:
                pass

            # Fill.
            for ri, (kind, _ipn, cells) in enumerate(entries):
                bg = add_bg if kind == "+" else del_bg if kind == "-" else None
                fg = add_fg if kind == "+" else del_fg if kind == "-" else None
                try:
                    self._grid.SetCellValue(ri, 0, kind if kind in {"+", "-"} else "")
                except Exception:
                    pass
                for ci, v in enumerate(cells, start=1):
                    if ci >= want_cols:
                        break
                    try:
                        self._grid.SetCellValue(ri, ci, str(v or ""))
                    except Exception:
                        pass
                # color whole row
                if bg is not None:
                    for ci in range(0, want_cols):
                        try:
                            self._grid.SetCellBackgroundColour(ri, ci, bg)
                        except Exception:
                            pass
                        try:
                            self._grid.SetCellTextColour(ri, ci, fg)
                        except Exception:
                            pass

            try:
                self._grid.ForceRefresh()
            except Exception:
                pass

            # Autosize columns to visible content (best-effort, capped).
            try:
                self._autosize_grid_columns(headers, entries)
            except Exception:
                pass

            # Summary (best-effort)
            try:
                plus = sum(1 for k, _i, _c in entries if k == "+")
                minus = sum(1 for k, _i, _c in entries if k == "-")
                self._summary.SetLabel(f"{plus} added rows  {minus} removed rows")
            except Exception:
                pass
            return

        # Populate.
        for ri, (kind, ipn, r) in enumerate(entries):
            bg = add_bg if kind == "+" else del_bg if kind == "-" else norm_bg
            try:
                self._grid.SetCellValue(ri, 0, kind)
            except Exception:
                pass
            for ci, h in enumerate(headers, start=1):
                try:
                    self._grid.SetCellValue(ri, ci, str((r or {}).get(h, "") or ""))
                except Exception:
                    pass
            # Color whole row.
            for ci in range(0, want_cols):
                try:
                    self._grid.SetCellBackgroundColour(ri, ci, bg)
                except Exception:
                    pass
                try:
                    if kind == "+":
                        self._grid.SetCellTextColour(ri, ci, add_fg)
                    elif kind == "-":
                        self._grid.SetCellTextColour(ri, ci, del_fg)
                except Exception:
                    pass

        try:
            self._grid.ForceRefresh()
        except Exception:
            pass

        # Autosize columns to visible content (best-effort, capped).
        try:
            # Convert rowdict entries to cell lists in the current header order.
            cell_entries: list[tuple[str, str, list[str]]] = []
            for kind, ipn, r in entries:
                cells = [str((r or {}).get(h, "") or "") for h in headers]
                cell_entries.append((kind, ipn, cells))
            self._autosize_grid_columns(headers, cell_entries)
        except Exception:
            pass

        try:
            self._summary.SetLabel(f"{len(add_ipns)} added  {len(del_ipns)} removed  {len(mod_ipns)} modified")
        except Exception:
            pass

    def _autosize_grid_columns(self, headers: list[str], entries: list[tuple[str, str, list[str]]]) -> None:
        """
        Best-effort "autosize to content" for wx.grid.Grid.
        We measure a sample of the currently visible rows (post-filter) and set column widths.
        """
        if not headers:
            return
        # Sample to avoid O(N) heavy measurement on huge files.
        sample = list(entries or [])
        max_rows = 350
        if len(sample) > max_rows:
            # keep start + evenly spaced + end
            step = max(1, int(len(sample) / max_rows))
            sample = sample[0:max_rows:step]

        # Track representative longest strings per column.
        reps: list[str] = ["Δ"] + [str(h or "") for h in headers]
        rep_lens: list[int] = [len(reps[0])] + [len(s) for s in reps[1:]]

        for kind, _ipn, cells in sample:
            # Δ column
            if kind in {"+", "-"} and 1 > rep_lens[0]:
                reps[0] = kind
                rep_lens[0] = 1
            for i, val in enumerate(list(cells or [])):
                ci = i + 1
                if ci >= len(reps):
                    break
                s = str(val or "")
                ln = len(s)
                if ln > rep_lens[ci]:
                    reps[ci] = s
                    rep_lens[ci] = ln

        # Measure using current grid font.
        dc = wx.ClientDC(self._grid)
        try:
            dc.SetFont(self._grid.GetDefaultCellFont())
        except Exception:
            pass

        pad = 18
        # Hard caps so e.g. Description/Datasheet don't take absurd width.
        cap = 520
        widths: list[int] = []
        for s in reps:
            try:
                w, _h = dc.GetTextExtent(str(s or ""))
                widths.append(int(min(cap, w + pad)))
            except Exception:
                widths.append(120)

        # Apply widths.
        try:
            self._grid.BeginBatch()
        except Exception:
            pass
        try:
            # Δ column fixed-ish
            try:
                self._grid.SetColSize(0, 40)
            except Exception:
                pass
            for ci in range(1, min(len(widths), self._grid.GetNumberCols())):
                try:
                    self._grid.SetColSize(ci, max(60, int(widths[ci])))
                except Exception:
                    continue
        finally:
            try:
                self._grid.EndBatch()
            except Exception:
                pass
