from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
from pathlib import Path

import wx
import wx.dataview as dv

from ..config import Config
from ..github_api import GitHubError
from ..repo import Category
from .dialogs import AddEntryDialog, EditEntryDialog
from .async_ui import UiDebouncer, UiRepeater, is_window_alive
from .git_ops import (
    git_fetch_head_age_seconds,
    git_fetch_head_mtime,
    format_age_minutes,
    fetch_stale_threshold_seconds,
    git_status_entries,
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
from .preview_panel import PreviewPanel
from .requests import prompt_commit_message, submit_request
from .services import category_title, load_csv_table, row_label, save_csv_table
from .window_title import with_library_suffix

try:
    from rapidfuzz import fuzz as _rf_fuzz  # type: ignore
    from rapidfuzz import process as _rf_process  # type: ignore
    from rapidfuzz import utils as _rf_utils  # type: ignore
except Exception:  # pragma: no cover
    _rf_fuzz = None
    _rf_process = None
    _rf_utils = None


class BrowseDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, repo_path: str, category: Category):
        # Child dialog (subwindow) of the main plugin window.
        super().__init__(
            parent,
            title=with_library_suffix(f"Browse: {category_title(category)}", repo_path),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX | wx.CLIP_CHILDREN,
        )
        self._repo_path = repo_path
        self._category = category
        self._owner = parent
        self._headers: list[str] = []
        self._rows: list[dict[str, str]] = []
        # Display rows currently shown in the list (includes virtual pending-add rows).
        self._visible_rows: list[dict[str, str]] = []
        # For each display row, the source index in self._rows, or None for virtual rows.
        self._visible_src_idx: list[int | None] = []
        self._row_texts: list[str] = []
        self._show_cols: list[str] = []
        # Ordered DBL-defined columns for the browser (excludes icon col).
        self._dbl_show_cols: list[str] = []
        self._sort_col_idx = 1  # 0 is icon col
        self._sort_asc = True

        self._remote_by_ipn: dict[str, dict[str, str]] | None = None
        self._remote_loaded = False
        self._remote_loading = False

        # Footprint density variant grouping (reuses the same grouping source as the footprint browser).
        # base_ref -> [variant_refs...], plus a reverse map variant_ref -> base_ref.
        self._fp_groups: dict[str, list[str]] = {}
        self._fp_group_rev: dict[str, str] = {}

        self._bmp_green = make_status_bitmap(wx.Colour(46, 160, 67))
        self._bmp_red = make_status_bitmap(wx.Colour(220, 53, 69))
        self._bmp_yellow = make_status_bitmap(wx.Colour(255, 193, 7))
        self._bmp_blue = make_status_bitmap(wx.Colour(13, 110, 253))
        self._bmp_gray = make_status_bitmap(wx.Colour(160, 160, 160))

        root = wx.BoxSizer(wx.VERTICAL)

        # top: status (left) + actions (right)
        top = wx.BoxSizer(wx.HORIZONTAL)
        self.status_icon = wx.StaticBitmap(self, bitmap=self._bmp_red)
        self.status_lbl = wx.StaticText(self, label="")
        top.Add(self.status_icon, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        top.Add(self.status_lbl, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        top.AddStretchSpacer(1)
        self.refresh_btn = wx.Button(self, label="↓  Fetch remote")
        self.refresh_btn.Bind(wx.EVT_BUTTON, self._on_fetch_remote)
        top.Add(self.refresh_btn, 0, wx.ALL, 6)
        self.sync_btn = wx.Button(self, label="↻  Sync library")
        self.sync_btn.Bind(wx.EVT_BUTTON, self._on_sync_library)
        top.Add(self.sync_btn, 0, wx.ALL, 6)
        root.Add(top, 0, wx.EXPAND)

        # filter/search
        self._search = wx.TextCtrl(self)
        self._search.SetHint("Filter (IPN / MPN / Manufacturer / Value / Description)")
        self._search.Bind(wx.EVT_TEXT, self._on_search)
        root.Add(self._search, 0, wx.ALL | wx.EXPAND, 8)
        try:
            # Shortcut handler (e.g. D to open datasheet).
            self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        except Exception:
            pass

        splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)
        self._splitter = splitter
        left = wx.Panel(splitter)
        right = wx.Panel(splitter)

        left_s = wx.BoxSizer(wx.VERTICAL)
        # Use DataViewListCtrl to avoid wx.ListCtrl header/content horizontal scroll desync on GTK.
        self._list = dv.DataViewListCtrl(left, style=dv.DV_ROW_LINES | dv.DV_VERT_RULES | dv.DV_HORIZ_RULES | dv.DV_SINGLE)
        # Bitmap bundles for the icon column.
        self._bb_green = wx.BitmapBundle.FromBitmap(self._bmp_green)
        self._bb_red = wx.BitmapBundle.FromBitmap(self._bmp_red)
        self._bb_yellow = wx.BitmapBundle.FromBitmap(self._bmp_yellow)
        self._bb_blue = wx.BitmapBundle.FromBitmap(self._bmp_blue)
        self._bb_gray = wx.BitmapBundle.FromBitmap(self._bmp_gray)
        left_s.Add(self._list, 1, wx.ALL | wx.EXPAND, 0)
        left.SetSizer(left_s)

        # right: previews (scrollable)
        right_scroll = wx.ScrolledWindow(right, style=wx.VSCROLL)
        right_scroll.SetScrollRate(0, 10)
        self._right_scroll = right_scroll
        right_s = wx.BoxSizer(wx.VERTICAL)
        self._right_scroll_sizer = right_s

        fp_box = wx.StaticBoxSizer(wx.VERTICAL, right_scroll, "Footprint preview")
        self._fp_prev = PreviewPanel(right_scroll, empty_label="(select a row)", show_choice=True, min_bitmap_size=(-1, 320))
        fp_box.Add(self._fp_prev, 1, wx.ALL | wx.EXPAND, 0)
        right_s.Add(fp_box, 1, wx.ALL | wx.EXPAND, 6)

        sym_box = wx.StaticBoxSizer(wx.VERTICAL, right_scroll, "Symbol preview")
        self._sym_prev = PreviewPanel(right_scroll, empty_label="(select a row)", show_choice=False, crop_to_alpha=True, min_bitmap_size=(-1, 320))
        sym_box.Add(self._sym_prev, 1, wx.ALL | wx.EXPAND, 0)
        right_s.Add(sym_box, 1, wx.ALL | wx.EXPAND, 6)

        right_scroll.SetSizer(right_s)
        right_outer = wx.BoxSizer(wx.VERTICAL)
        right_outer.Add(right_scroll, 1, wx.EXPAND)
        right.SetSizer(right_outer)

        splitter.SplitVertically(left, right, sashPosition=760)
        splitter.SetMinimumPaneSize(350)
        # Avoid negative sash positions (can assert on some wx ports, notably macOS).
        def _set_default_sash() -> None:
            try:
                w = int(splitter.GetClientSize().GetWidth() or 0)
            except Exception:
                w = 0
            # Keep ~350px for the preview pane when possible.
            right_w = 350
            try:
                pos = max(350, w - right_w) if w else 760
                splitter.SetSashPosition(pos)
            except Exception:
                pass

        wx.CallAfter(_set_default_sash)
        root.Add(splitter, 1, wx.ALL | wx.EXPAND, 8)

        # buttons (match legacy)
        btns = wx.BoxSizer(wx.HORIZONTAL)
        add_btn = wx.Button(self, label="Add")
        edit_btn = wx.Button(self, label="Edit")
        del_btn = wx.Button(self, label="Delete")
        ds_btn = wx.Button(self, label="Open datasheet")
        add_btn.Bind(wx.EVT_BUTTON, self._on_add)
        edit_btn.Bind(wx.EVT_BUTTON, self._on_edit)
        del_btn.Bind(wx.EVT_BUTTON, self._on_delete)
        ds_btn.Bind(wx.EVT_BUTTON, self._on_open_datasheet)
        btns.Add(add_btn, 0, wx.ALL, 6)
        btns.Add(edit_btn, 0, wx.ALL, 6)
        btns.Add(del_btn, 0, wx.ALL, 6)
        btns.Add(ds_btn, 0, wx.ALL, 6)
        btns.AddStretchSpacer(1)
        close_btn = wx.Button(self, label="Close")
        close_btn.Bind(wx.EVT_BUTTON, lambda _e: self.Close())
        btns.Add(close_btn, 0, wx.ALL, 6)
        root.Add(btns, 0, wx.EXPAND)

        self.SetSizer(root)
        self.SetMinSize((1200, 750))
        # Slightly taller by default to make previews more usable.
        self.SetSize((1400, 1100))

        # After first show/layout, force the scrolled preview pane to recalc its best sizes.
        # Without this, wx/GTK sometimes keeps the preview boxes slightly too small until
        # the user manually resizes the window.
        self._did_post_show_layout = False
        try:
            self.Bind(wx.EVT_SHOW, self._on_show_event)
        except Exception:
            self._did_post_show_layout = True

        self._list.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self._on_row_selected)
        self._list.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, self._on_edit)
        self._list.Bind(dv.EVT_DATAVIEW_COLUMN_HEADER_CLICK, self._on_col_click)
        # Important: changing density choice should NOT rebuild the choice list (that resets selection).
        self._fp_prev.choice.Bind(wx.EVT_CHOICE, self._on_fp_choice_changed)

        self._search_pending_q = ""
        self._search_debouncer = UiDebouncer(self, delay_ms=350, callback=self._rebuild_list)

        # Auto-refresh on remote fetch or local CSV changes.
        self._last_fetch_mtime = git_fetch_head_mtime(self._repo_path) or 0.0
        try:
            self._last_csv_mtime = float(os.path.getmtime(self._category.csv_path))
        except Exception:
            self._last_csv_mtime = 0.0
        self._watch_repeater = UiRepeater(self, interval_ms=1000, callback=self._on_watch_tick)
        self._closing = False
        self.Bind(wx.EVT_CLOSE, self._on_close)
        try:
            # `Destroy()` doesn't necessarily emit EVT_CLOSE; stop timers on destruction too.
            self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        except Exception:
            pass

        self._refresh_top_status()
        self._on_reload()

    def _on_show_event(self, evt: wx.ShowEvent) -> None:
        try:
            if not evt.IsShown():
                evt.Skip()
                return
        except Exception:
            pass
        if getattr(self, "_did_post_show_layout", False):
            try:
                evt.Skip()
            except Exception:
                pass
            return
        self._did_post_show_layout = True

        def _post() -> None:
            if self._closing:
                return
            try:
                sp = getattr(self, "_splitter", None)
                if sp:
                    try:
                        sp.UpdateSize()
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                self.Layout()
            except Exception:
                pass
            try:
                rs = getattr(self, "_right_scroll", None)
                if rs:
                    rs.Layout()
                    rs.FitInside()
            except Exception:
                pass
            try:
                self._update_previews(rebuild_choice=False)
            except Exception:
                pass

        # Two ticks: splitter + scrolled windows can settle over multiple layout passes.
        try:
            wx.CallAfter(lambda: wx.CallAfter(_post))
        except Exception:
            _post()
        try:
            evt.Skip()
        except Exception:
            pass

    def _on_char_hook(self, evt: wx.KeyEvent) -> None:
        """
        Keyboard shortcuts:
        - D: open datasheet for selected row
        """
        try:
            key = int(evt.GetKeyCode())
        except Exception:
            key = 0
        try:
            focus = wx.Window.FindFocus()
        except Exception:
            focus = None
        # Don't steal typing from text inputs.
        if isinstance(focus, wx.TextCtrl):
            evt.Skip()
            return
        if key == wx.WXK_ESCAPE:
            try:
                if self.IsModal():
                    self.EndModal(wx.ID_CANCEL)
                else:
                    self.Close()
                return
            except Exception:
                evt.Skip()
                return
        if key in (ord("D"), ord("d")):
            try:
                self._on_open_datasheet(None)
                return
            except Exception:
                evt.Skip()
                return
        evt.Skip()

    def _on_destroy(self, evt) -> None:
        # Mirror close behavior for robustness against use-after-free crashes from timers.
        self._closing = True
        try:
            if getattr(self, "_watch_repeater", None):
                self._watch_repeater.stop()
            if getattr(self, "_search_debouncer", None):
                self._search_debouncer.cancel()
        except Exception:
            pass
        try:
            evt.Skip()
        except Exception:
            pass

    def _on_close(self, evt: wx.CloseEvent) -> None:
        self._closing = True
        try:
            if getattr(self, "_watch_repeater", None):
                self._watch_repeater.stop()
            if getattr(self, "_search_debouncer", None):
                self._search_debouncer.cancel()
        except Exception:
            pass
        # When shown modally, end the modal loop so callers can Destroy() in a finally block.
        try:
            if self.IsModal():
                try:
                    self.EndModal(wx.ID_CANCEL)
                    return
                except Exception:
                    pass
        except Exception:
            pass
        evt.Skip()

    def _on_watch_tick(self) -> None:
        if self._closing:
            return
        try:
            fetch_m = git_fetch_head_mtime(self._repo_path) or 0.0
        except Exception:
            fetch_m = 0.0
        if fetch_m > self._last_fetch_mtime:
            self._last_fetch_mtime = fetch_m
            # Update pending states for this category based on request file presence on origin/<branch>.
            try:
                br = (Config.load_effective(self._repo_path).github_base_branch.strip() or "main")
            except Exception:
                br = "main"
            try:
                update_pending_states_after_fetch(self._repo_path, category_name=self._category.display_name, branch=br, fetch_mtime=fetch_m)
            except Exception:
                pass
            self._remote_loaded = False
            self._remote_by_ipn = None
            self._remote_loading = False
            self._refresh_top_status()
            self._rebuild_list()
            return
        try:
            csv_m = float(os.path.getmtime(self._category.csv_path))
        except Exception:
            csv_m = 0.0
        if csv_m > self._last_csv_mtime:
            self._last_csv_mtime = csv_m
            self._on_reload()

    def _refresh_top_status(self) -> None:
        try:
            st = git_sync_status(self._repo_path)
            stale = bool(st.get("stale"))
            dirty = bool(st.get("dirty"))
            if stale:
                age = st.get("age")
                suffix = f" (last fetch {format_age_minutes(age)})" if age is not None else ""
                self.status_icon.SetBitmap(self._bmp_gray)
                self.status_lbl.SetLabel("Library status: unknown / stale — click Fetch remote" + suffix)
            elif bool(st.get("up_to_date")):
                self.status_icon.SetBitmap(self._bmp_green)
                from ..config import Config

                br = (Config.load_effective(self._repo_path).github_base_branch.strip() or "main")
                self.status_lbl.SetLabel(f"Library status: synchronized with origin/{br}")
            elif dirty:
                self.status_icon.SetBitmap(self._bmp_yellow)
                self.status_lbl.SetLabel("Library status: local changes (uncommitted)")
            else:
                behind = st.get("behind")
                self.status_icon.SetBitmap(self._bmp_red)
                if isinstance(behind, int):
                    self.status_lbl.SetLabel(f"Library status: out of date (behind {behind})")
                else:
                    self.status_lbl.SetLabel("Library status: out of date")
        except Exception as exc:  # noqa: BLE001
            self.status_icon.SetBitmap(self._bmp_red)
            self.status_lbl.SetLabel(f"Library status: unavailable ({exc})")

        # Pending overrides status color (legacy behavior: pending beats green/red).
        try:
            has_pend, applied = pending_tag_for_category(self._category.display_name)
            behind = None
            try:
                behind = st.get("behind")
            except Exception:
                behind = None

            if has_pend and applied and bool(st.get("up_to_date")) and isinstance(behind, int) and behind <= 0:
                # If we're already synced (up_to_date), clear applied_remote items so we don't show "sync needed".
                try:
                    drop_applied_pending_if_already_synced(self._repo_path, category_name=self._category.display_name)
                except Exception:
                    pass
                has_pend, applied = pending_tag_for_category(self._category.display_name)

            if has_pend:
                if applied and isinstance(behind, int) and behind > 0:
                    self.status_icon.SetBitmap(self._bmp_blue)
                    self.status_lbl.SetLabel("Library status: sync needed")
                else:
                    self.status_icon.SetBitmap(self._bmp_yellow)
                    self.status_lbl.SetLabel("Library status: pending changes")
        except Exception:
            pass

        try:
            self.status_lbl.Wrap(max(200, self.GetClientSize().width - 80))
        except Exception:
            pass
        try:
            self.Layout()
        except Exception:
            pass

    def _on_fetch_remote(self, evt: wx.CommandEvent) -> None:
        # Best-effort fallback: do a plain fetch.
        from ..config import Config

        cfg = Config.load_effective(self._repo_path)
        branch = (cfg.github_base_branch.strip() or "main")

        try:
            self.status_lbl.SetLabel("Fetching remote...")
            self.status_icon.SetBitmap(self._bmp_gray)
        except Exception:
            pass
        try:
            self.refresh_btn.Enable(False)
            self.sync_btn.Enable(False)
        except Exception:
            pass

        def worker() -> None:
            err: Exception | None = None
            try:
                run_git(["git", "-C", self._repo_path, "fetch", "origin", branch, "--quiet"], cwd=self._repo_path)
            except Exception as e:  # noqa: BLE001
                err = e

            def done_on_ui() -> None:
                if self._closing:
                    return
                try:
                    self.refresh_btn.Enable(True)
                    self.sync_btn.Enable(True)
                except Exception:
                    pass
                if err:
                    wx.MessageBox(f"Fetch remote failed:\n\n{err}", "Fetch remote", wx.OK | wx.ICON_WARNING)
                self._last_fetch_mtime = git_fetch_head_mtime(self._repo_path) or self._last_fetch_mtime
                try:
                    update_pending_states_after_fetch(
                        self._repo_path,
                        category_name=self._category.display_name,
                        branch=branch,
                        fetch_mtime=self._last_fetch_mtime,
                    )
                except Exception:
                    pass
                self._remote_loaded = False
                self._remote_by_ipn = None
                self._remote_loading = False
                self._refresh_top_status()
                self._rebuild_list()
                try:
                    if self._owner and hasattr(self._owner, "_append_log"):
                        self._owner._append_log(f"Fetched origin/{branch} from remote.")  # type: ignore[misc]
                except Exception:
                    pass
                # Notify main window so global/category status updates immediately.
                try:
                    if self._owner and hasattr(self._owner, "_refresh_sync_status"):
                        self._owner._refresh_sync_status()  # type: ignore[misc]
                    if self._owner and hasattr(self._owner, "_reload_category_statuses"):
                        self._owner._reload_category_statuses()  # type: ignore[misc]
                    if self._owner and hasattr(self._owner, "_refresh_remote_cat_updated_times_async"):
                        self._owner._refresh_remote_cat_updated_times_async()  # type: ignore[misc]
                    if self._owner and hasattr(self._owner, "_refresh_categories_status_icon"):
                        self._owner._refresh_categories_status_icon()  # type: ignore[misc]
                except Exception:
                    pass

            wx.CallAfter(done_on_ui)

        threading.Thread(target=worker, daemon=True).start()

    def _on_sync_library(self, evt: wx.CommandEvent) -> None:
        try:
            self.status_lbl.SetLabel("Syncing library...")
            self.status_icon.SetBitmap(self._bmp_gray)
        except Exception:
            pass
        try:
            self.refresh_btn.Enable(False)
            self.sync_btn.Enable(False)
        except Exception:
            pass

        def worker() -> None:
            err: Exception | None = None
            out: str = ""
            try:
                br = (Config.load_effective(self._repo_path).github_base_branch or "main").strip() or "main"
            except Exception:
                br = "main"
            try:
                entries = git_status_entries(self._repo_path)
                assets = paths_changed_under(entries, ["Symbols", "Footprints"])
                others = [p for _st, p in entries if p not in set(assets)]
                if others:
                    preview = "\n".join(f"- {p}" for p in others[:20])
                    raise RuntimeError(
                        "Local changes exist outside Symbols/ and Footprints/.\n"
                        "Please commit or revert them manually before syncing.\n\n" + preview
                    )
                from .git_ops import git_sync_ff_only

                out = git_sync_ff_only(self._repo_path, branch=br)
            except Exception as e:  # noqa: BLE001
                err = e

            def done_on_ui() -> None:
                if self._closing:
                    return
                try:
                    self.refresh_btn.Enable(True)
                    self.sync_btn.Enable(True)
                except Exception:
                    pass
                if err:
                    wx.MessageBox(str(err), "Sync failed", wx.OK | wx.ICON_WARNING)
                else:
                    try:
                        if self._owner and hasattr(self._owner, "_append_log"):
                            self._owner._append_log((out or "").strip() or "Sync completed.")  # type: ignore[misc]
                    except Exception:
                        pass
                # Reload status/icons either way.
                self._on_reload()
                # Notify main window so global status becomes green immediately.
                try:
                    if self._owner and hasattr(self._owner, "_refresh_sync_status"):
                        self._owner._refresh_sync_status()  # type: ignore[misc]
                    if self._owner and hasattr(self._owner, "_reload_category_statuses"):
                        self._owner._reload_category_statuses()  # type: ignore[misc]
                    if self._owner and hasattr(self._owner, "_refresh_remote_cat_updated_times_async"):
                        self._owner._refresh_remote_cat_updated_times_async()  # type: ignore[misc]
                    if self._owner and hasattr(self._owner, "_refresh_categories_status_icon"):
                        self._owner._refresh_categories_status_icon()  # type: ignore[misc]
                except Exception:
                    pass

            wx.CallAfter(done_on_ui)

        threading.Thread(target=worker, daemon=True).start()

    def _on_reload(self) -> None:
        table = load_csv_table(self._category.csv_path)
        self._headers = table.headers
        self._rows = table.rows
        self._dbl_show_cols = self._load_dbl_show_cols()
        # After sync/pull updates local CSV, clear any pending items that are now reflected locally.
        try:
            local_by_ipn: dict[str, dict[str, str]] = {}
            for r in (self._rows or []):
                ipn = str((r or {}).get("IPN", "") or "").strip()
                if ipn:
                    local_by_ipn[ipn] = dict(r)
            reconcile_pending_against_local_csv(self._repo_path, category_name=self._category.display_name, local_by_ipn=local_by_ipn)
        except Exception:
            pass
        # After a sync, pending adds may still not have `resolved_ipn`.
        # Best-effort: resolve them against the freshly updated LOCAL CSV, then reconcile again.
        try:
            pend = PENDING.list_for(self._category.display_name)
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
                        # At this point, it is reflected locally, so the request is effectively applied.
                        pp["state"] = "applied_remote"
                        updated_items.append(pp)
                        changed = True
                    else:
                        updated_items.append(p)
                if changed:
                    PENDING.set_items(self._category.display_name, updated_items)
                    reconcile_pending_against_local_csv(self._repo_path, category_name=self._category.display_name, local_by_ipn=local_by_ipn)
        except Exception:
            pass

        # Refresh status after reload.
        try:
            self._refresh_top_status()
        except Exception:
            pass
        self._build_row_texts()
        self._rebuild_list()

    def _load_dbl_show_cols(self) -> list[str]:
        """
        Return ordered columns for the component browser based on DBL config:
        Database/category_fields/<Category>.json (ordered fields list).

        If the config is missing/invalid, fall back to CSV headers.
        """
        cat = str(getattr(self._category, "display_name", "") or "").strip()
        if not cat:
            return []

        # Prefer DBL config for the browser column set.
        p = os.path.join(self._repo_path, "Database", "category_fields", f"{cat}.json")
        fields: list[dict] = []
        try:
            with open(p, "r", encoding="utf-8") as f:
                body = json.load(f) or {}
            fields = list((body or {}).get("fields") or [])
        except Exception:
            fields = []

        ordered: list[str] = []
        if fields:
            for fd in fields:
                if not isinstance(fd, dict):
                    continue
                # Use the CSV column key (not the display label) so we can read row dicts reliably.
                name = str(fd.get("column") or fd.get("name") or "").strip()
                if not name:
                    continue
                ordered.append(name)

        # Always show IPN first (ui.py behavior + required for selection/status).
        if "IPN" in ordered:
            ordered = ["IPN"] + [h for h in ordered if h != "IPN"]
        else:
            # If config didn't include IPN (or hid it), still include it.
            ordered = ["IPN"] + ordered if ordered else []

        # Dedupe while preserving order.
        out: list[str] = []
        seen: set[str] = set()
        for h in ordered:
            hh = str(h or "").strip()
            if not hh or hh in seen:
                continue
            seen.add(hh)
            out.append(hh)

        # If no usable DBL columns, fall back to CSV headers (legacy behavior).
        if not out:
            headers = list(self._headers or [])
            if "IPN" in headers:
                headers = ["IPN"] + [h for h in headers if h != "IPN"]
            out = [str(h or "").strip() for h in headers if str(h or "").strip()]

        return out

    def _load_dbl_form_headers(self, *, add_mode: bool) -> list[str]:
        """
        Return ordered headers for the add/edit dialogs based on DBL config
        (Database/category_fields/<Category>.json).

        Note: DBL visibility flags (visible_on_add/visible_in_chooser) are used by KiCad's DBL
        integration, not by this plugin's add/edit dialogs. We therefore use the ordered DBL
        field list without filtering.

        - add_mode=True: same as edit (all DBL fields), plus required columns.
        - add_mode=False: all DBL fields, plus required columns.
        - fallback: if config missing/invalid/empty → CSV headers.
        """
        required = ["IPN", "Symbol", "Footprint"]
        cat = str(getattr(self._category, "display_name", "") or "").strip()
        if not cat:
            return list(required)

        p = os.path.join(self._repo_path, "Database", "category_fields", f"{cat}.json")
        fields: list[dict] = []
        cfg_has_fields = False
        try:
            with open(p, "r", encoding="utf-8") as f:
                body = json.load(f) or {}
            fields = list((body or {}).get("fields") or [])
            cfg_has_fields = bool(fields)
        except Exception:
            fields = []
            cfg_has_fields = False

        ordered: list[str] = []
        if cfg_has_fields:
            all_fields: list[str] = []
            for fd in fields:
                if not isinstance(fd, dict):
                    continue
                # Use the CSV column key (not the display label).
                col = str(fd.get("column") or fd.get("name") or "").strip()
                if not col:
                    continue
                all_fields.append(col)
            ordered = list(all_fields)

            # Ensure required columns exist (validate_row depends on them).
            have = {str(h or "").strip() for h in ordered}
            for r in required:
                if r not in have:
                    ordered.insert(0, r)
                    have.add(r)

            # Force IPN first.
            if "IPN" in ordered:
                ordered = ["IPN"] + [h for h in ordered if h != "IPN"]

            # Dedupe while preserving order.
            out: list[str] = []
            seen: set[str] = set()
            for h in ordered:
                hh = str(h or "").strip()
                if not hh or hh in seen:
                    continue
                seen.add(hh)
                out.append(hh)

            return out

        # Fallback to CSV headers (legacy behavior).
        headers = list(self._headers or [])
        if "IPN" in headers:
            headers = ["IPN"] + [h for h in headers if h != "IPN"]
        out = [str(h or "").strip() for h in headers if str(h or "").strip()]
        for r in required:
            if r not in out:
                out.insert(0, r)
        # Force IPN first again after required insertion.
        if "IPN" in out:
            out = ["IPN"] + [h for h in out if h != "IPN"]
        return out

    def _on_search(self, _evt: wx.CommandEvent) -> None:
        self._search_pending_q = (self._search.GetValue() or "").strip()
        try:
            self._search_debouncer.trigger(delay_ms=350)
        except Exception:
            self._rebuild_list()

    def _build_row_texts(self) -> None:
        # Use DBL-defined columns for search text too (falls back to CSV headers).
        ordered = list(self._dbl_show_cols or self._load_dbl_show_cols() or [])
        texts: list[str] = []
        for row in self._rows:
            parts = [str(row.get(h, "") or "") for h in ordered]
            texts.append(" ".join([p for p in parts if p]).strip())
        self._row_texts = texts

    def _ensure_remote_loaded_async(self) -> None:
        """
        Load origin/<branch> CSV for this category in background (for per-row status icons).
        """
        if self._remote_loaded or self._remote_loading or self._closing:
            return
        age = git_fetch_head_age_seconds(self._repo_path)
        if age is None or age > fetch_stale_threshold_seconds(self._repo_path):
            # Stale/unknown: don't try loading remote file.
            return
        self._remote_loading = True

        def worker() -> None:
            remote_by_ipn: dict[str, dict[str, str]] | None = None
            try:
                # Read the remote CSV content without network (uses FETCH_HEAD refs).
                from ..config import Config

                br = (Config.load_effective(self._repo_path).github_base_branch.strip() or "main")
                spec = f"origin/{br}:Database/{self._category.filename}"
                txt = run_git(["git", "-C", self._repo_path, "show", spec], cwd=self._repo_path)
                rdr = csv.DictReader((txt or "").splitlines())
                rows = [dict(r) for r in rdr]
                remote_by_ipn = {}
                for r in rows:
                    ipn = str((r or {}).get("IPN", "") or "").strip()
                    if ipn:
                        remote_by_ipn[ipn] = {k: str(v or "") for k, v in (r or {}).items()}
            except Exception:
                remote_by_ipn = None

            def done_on_ui() -> None:
                if self._closing:
                    return
                self._remote_by_ipn = remote_by_ipn
                self._remote_loaded = True
                self._remote_loading = False
                self._rebuild_list()

            wx.CallAfter(done_on_ui)

        threading.Thread(target=worker, daemon=True).start()

    def _rebuild_list(self) -> None:
        q_raw = (self._search_pending_q or "").strip()
        q = q_raw.lower()

        # Preserve selection across rebuilds.
        selected_ipn = self._selected_ipn()

        self._ensure_remote_loaded_async()

        # Pending actions for this category (legacy request flow).
        pend = PENDING.list_for(self._category.display_name)
        pending_delete = {
            str(p.get("ipn") or "").strip()
            for p in pend
            if str(p.get("action") or "").strip() == "delete" and str(p.get("ipn") or "").strip()
        }
        pending_update = {
            str(p.get("ipn") or "").strip(): p
            for p in pend
            if str(p.get("action") or "").strip() == "update" and str(p.get("ipn") or "").strip()
        }
        pending_add = [p for p in pend if str(p.get("action") or "").strip() == "add"]
        pending_delete_items = [p for p in pend if str(p.get("action") or "").strip() == "delete" and str(p.get("ipn") or "").strip()]

        # Column order: icon + DBL-defined columns (fallback to CSV headers).
        ordered = list(self._dbl_show_cols or self._load_dbl_show_cols() or [])
        self._show_cols = [""] + ordered

        # If we have remote rows loaded, attempt to resolve pending adds to a remote IPN (legacy behavior).
        try:
            if self._remote_loaded and isinstance(self._remote_by_ipn, dict) and self._remote_by_ipn and pending_add:
                local_by_ipn: dict[str, dict[str, str]] = {}
                for r in (self._rows or []):
                    ipn = str((r or {}).get("IPN", "") or "").strip()
                    if ipn:
                        local_by_ipn[ipn] = dict(r)

                match_keys = ["MPN", "Manufacturer", "Value", "Footprint", "Symbol", "Description"]

                def _fields_match(rr: dict[str, str], fields: dict[str, str]) -> bool:
                    for k in match_keys:
                        pv = str(fields.get(k, "") or "").strip()
                        if not pv:
                            continue
                        if str((rr.get(k) or "")).strip() != pv:
                            return False
                    return True

                changed = False
                updated_items: list[dict] = []
                for p in pend:
                    if str(p.get("action") or "").strip() != "add":
                        updated_items.append(p)
                        continue
                    if str(p.get("resolved_ipn") or "").strip():
                        updated_items.append(p)
                        continue
                    fields = dict(p.get("fields") or {})
                    non_empty = [k for k in match_keys if str(fields.get(k, "") or "").strip()]
                    if len(non_empty) < 2:
                        updated_items.append(p)
                        continue
                    cands: list[str] = []
                    for ipn, rr in (self._remote_by_ipn or {}).items():
                        try:
                            if _fields_match(rr, fields):
                                cands.append(str(ipn or "").strip())
                        except Exception:
                            continue
                    if len(cands) == 1:
                        cand_ipn = cands[0]
                        if cand_ipn and cand_ipn not in local_by_ipn:
                            pp = dict(p)
                            pp["resolved_ipn"] = cand_ipn
                            pp["state"] = "applied_remote"
                            updated_items.append(pp)
                            changed = True
                            continue
                    updated_items.append(p)
                if changed:
                    PENDING.set_items(self._category.display_name, updated_items)
                    pend = updated_items
                    pending_delete = {
                        str(p.get("ipn") or "").strip()
                        for p in pend
                        if str(p.get("action") or "").strip() == "delete" and str(p.get("ipn") or "").strip()
                    }
                    pending_update = {
                        str(p.get("ipn") or "").strip(): p
                        for p in pend
                        if str(p.get("action") or "").strip() == "update" and str(p.get("ipn") or "").strip()
                    }
                    pending_add = [p for p in pend if str(p.get("action") or "").strip() == "add"]
        except Exception:
            pass

        # Build visible indices via RapidFuzz (fallback to substring).
        visible: list[int] = []
        if not q:
            visible = list(range(len(self._rows)))
        elif _rf_process is None or _rf_fuzz is None or _rf_utils is None:
            for i, txt in enumerate(self._row_texts):
                if q in (txt or "").lower():
                    visible.append(i)
        else:
            # Extract matches with a moderate threshold; allow short numeric queries to match well.
            choices = self._row_texts
            limit = min(len(choices), 8000)
            try:
                matches = _rf_process.extract(
                    q_raw,
                    choices,
                    scorer=_rf_fuzz.WRatio,
                    processor=_rf_utils.default_process,
                    limit=limit,
                )
            except Exception:
                matches = []
            # Filter by score; keep it permissive for short queries.
            thr = 55 if len(q_raw) >= 3 else 65
            for _choice, score, idx in matches:
                try:
                    if float(score or 0.0) >= float(thr):
                        visible.append(int(idx))
                except Exception:
                    continue

        # Sort by selected column (text sort).
        col_idx = int(self._sort_col_idx or 1)
        asc = bool(self._sort_asc)
        show_cols = list(self._show_cols)
        if col_idx <= 0 or col_idx >= len(show_cols):
            self._sort_col_idx = 1
            col_idx = 1

        def key_for(i: int) -> str:
            if i < 0 or i >= len(self._rows):
                return ""
            # col_idx maps into show_cols; col 0 is icon.
            if col_idx <= 0 or col_idx >= len(show_cols):
                return ""
            h = show_cols[col_idx]
            return str((self._rows[i] or {}).get(h, "") or "").strip().lower()

        try:
            visible.sort(key=key_for, reverse=not asc)
        except Exception:
            pass

        # Build display rows:
        # - local CSV rows (sorted) excluding optimistic pending deletes
        # - pending adds appended at the end
        # - pending deletes appended at the end as "DELETED" placeholders
        display_rows: list[dict[str, str]] = []
        display_src_idx: list[int | None] = []

        local_by_ipn: dict[str, dict[str, str]] = {}
        try:
            for r in (self._rows or []):
                ipn = str((r or {}).get("IPN", "") or "").strip()
                if ipn:
                    local_by_ipn[ipn] = dict(r)
        except Exception:
            local_by_ipn = {}

        for src_i in visible:
            try:
                row = dict(self._rows[src_i] or {})
            except Exception:
                continue
            ipn = str(row.get("IPN", "") or "").strip()
            if ipn and ipn in pending_delete:
                continue
            # Overlay pending updates for display + preview.
            if ipn and ipn in pending_update:
                set_fields = dict((pending_update[ipn] or {}).get("set") or {})
                for k, v in set_fields.items():
                    row[str(k)] = str(v or "")
            display_rows.append(row)
            display_src_idx.append(int(src_i))

        for p in pending_add:
            fields = dict(p.get("fields") or {})
            ripn = str(p.get("resolved_ipn") or "PENDING").strip() or "PENDING"
            prow: dict[str, str] = {"IPN": ripn}
            for k, v in fields.items():
                if str(k) == "IPN":
                    continue
                prow[str(k)] = str(v or "")
            display_rows.append(prow)
            display_src_idx.append(None)

        for p in pending_delete_items:
            ipn = str(p.get("ipn") or "").strip()
            if not ipn:
                continue
            base = dict(local_by_ipn.get(ipn) or {"IPN": ipn})
            base["__pending_delete"] = True
            display_rows.append(base)
            display_src_idx.append(None)

        self._visible_rows = display_rows
        self._visible_src_idx = display_src_idx

        try:
            self._list.Freeze()
        except Exception:
            pass
        try:
            try:
                self._list.DeleteAllItems()
            except Exception:
                pass
            try:
                self._list.ClearColumns()
            except Exception:
                pass

            # Columns: first is icon, then text columns for fields.
            self._list.AppendIconTextColumn("", width=32)
            for col in self._show_cols[1:]:
                self._list.AppendTextColumn(str(col or ""), width=wx.COL_WIDTH_AUTOSIZE)

            for row, src_i in zip(self._visible_rows, self._visible_src_idx, strict=False):
                # icon column + remaining columns in the same order as self._show_cols[1:].
                try:
                    ipn = str(row.get("IPN", "") or "").strip()
                    if src_i is None:
                        if bool(row.get("__pending_delete")):
                            # pending delete placeholder
                            st = ""
                            try:
                                for pp in pending_delete_items:
                                    if str(pp.get("ipn") or "").strip() == ipn:
                                        st = str(pp.get("state") or "").strip()
                                        break
                            except Exception:
                                st = ""
                            bb = self._bb_blue if st == "applied_remote" else self._bb_yellow
                        else:
                            # pending add
                            st = ""
                            try:
                                for pp in pending_add:
                                    if str(pp.get("resolved_ipn") or "PENDING").strip() == ipn:
                                        st = str(pp.get("state") or "").strip()
                                        break
                            except Exception:
                                st = ""
                            bb = self._bb_blue if st == "applied_remote" else self._bb_yellow
                    elif ipn and ipn in pending_update:
                        st = str((pending_update[ipn] or {}).get("state") or "submitted").strip()
                        bb = self._bb_blue if st == "applied_remote" else self._bb_yellow
                    else:
                        bb = self._row_status_bundle(row)
                except Exception:
                    bb = self._bb_gray
                icon_cell = dv.DataViewIconText("", bb)
                vals = [icon_cell]
                for h in self._show_cols[1:]:
                    if bool(row.get("__pending_delete")) and str(h) == "IPN":
                        vals.append("DELETED")
                    else:
                        vals.append(str(row.get(h, "") or ""))
                self._list.AppendItem(vals)
        finally:
            try:
                self._list.Thaw()
            except Exception:
                pass

        # Restore selection + update preview.
        if selected_ipn:
            self._select_ipn(selected_ipn)
        # Rebuild choice on list rebuilds to keep it in sync with current row.
        self._update_previews(rebuild_choice=True)

    def _row_status_bundle(self, row: dict[str, str]) -> wx.BitmapBundle:
        """
        Best-effort per-row status vs origin/<branch>.
        - gray: unknown/stale/remote not loaded
        - blue: remote loading in progress
        - green: identical to origin/<branch> row
        - yellow: new row (not on origin/<branch>)
        - red: differs from origin/<branch>
        """
        if not self._remote_loaded:
            return self._bb_blue if self._remote_loading else self._bb_gray
        if not self._remote_by_ipn:
            return self._bb_gray
        ipn = str(row.get("IPN", "") or "").strip()
        if not ipn:
            return self._bb_gray
        remote = self._remote_by_ipn.get(ipn)
        if not remote:
            return self._bb_yellow
        # Compare all columns we display (excluding icon).
        for h in self._show_cols[1:]:
            if str((row.get(h, "") or "")).strip() != str((remote.get(h, "") or "")).strip():
                return self._bb_red
        return self._bb_green

    def _on_col_click(self, evt) -> None:
        # DataView events provide a column object; map it to a column index.
        try:
            col_obj = evt.GetColumn()
            col = int(self._list.GetColumnPosition(col_obj))
        except Exception:
            col = 1
        if col <= 0:
            return
        if col == self._sort_col_idx:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col_idx = col
            self._sort_asc = True
        self._rebuild_list()

    def _selected_row_index(self) -> int:
        try:
            sel = int(self._list.GetSelectedRow())
        except Exception:
            sel = -1
        if sel < 0 or sel >= len(self._visible_src_idx):
            return -1
        src = self._visible_src_idx[sel]
        return int(src) if isinstance(src, int) else -1

    def _selected_display_row(self) -> dict[str, str] | None:
        try:
            sel = int(self._list.GetSelectedRow())
        except Exception:
            sel = -1
        if sel < 0 or sel >= len(self._visible_rows):
            return None
        try:
            return dict(self._visible_rows[sel] or {})
        except Exception:
            return None

    def _selected_ipn(self) -> str:
        row = self._selected_display_row() or {}
        ipn = str(row.get("IPN", "") or "").strip()
        if not ipn:
            return ""
        return ipn

    def _select_ipn(self, ipn: str) -> None:
        ipn = (ipn or "").strip()
        if not ipn:
            return
        try:
            for disp_i, row in enumerate(self._visible_rows):
                try:
                    txt = str((row or {}).get("IPN", "") or "").strip()
                except Exception:
                    txt = ""
                if txt == ipn:
                    try:
                        self._list.UnselectAll()
                    except Exception:
                        pass
                    self._list.SelectRow(disp_i)
                    try:
                        self._list.EnsureVisible(disp_i)
                    except Exception:
                        pass
                    break
        except Exception:
            return

    def _on_add(self, _evt: wx.CommandEvent) -> None:
        form_headers = self._load_dbl_form_headers(add_mode=True)
        dlg = AddEntryDialog(self, self._repo_path, self._category, form_headers, self._rows)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            row = dlg.get_row()
        finally:
            dlg.Destroy()

        cfg = Config.load_effective(self._repo_path)
        if not (cfg.github_owner.strip() and cfg.github_repo.strip()):
            wx.MessageBox("GitHub is not configured. Click Settings… first.", "Add component", wx.OK | wx.ICON_WARNING)
            return
        try:
            msg = prompt_commit_message(self, default=f"request: add {self._category.display_name} part")
            if msg is None:
                return
            fields = {k: (v or "") for k, v in (row or {}).items() if str(k) != "IPN"}
            req_path = submit_request(cfg, action="add", payload={"category": self._category.display_name, "fields": fields}, commit_message=msg)
            try:
                br = (Config.load_effective(self._repo_path).github_base_branch.strip() or "main")
            except Exception:
                br = "main"
            try:
                origin_sha = (run_git(["git", "rev-parse", f"origin/{br}"], cwd=self._repo_path) or "").strip()
            except Exception:
                origin_sha = ""
            PENDING.add(
                self._category.display_name,
                {
                    "action": "add",
                    "fields": fields,
                    "created_at": time.time(),
                    "fetch_mtime_at_submit": (git_fetch_head_mtime(self._repo_path) or 0.0),
                    "origin_sha_at_submit": origin_sha,
                    "seen_remote": False,
                    "state": "submitted",
                    "req_path": req_path,
                },
            )
        except GitHubError as e:
            wx.MessageBox(f"GitHub submission failed:\n\n{e}", "Add component", wx.OK | wx.ICON_WARNING)
            return
        except Exception as e:  # noqa: BLE001
            wx.MessageBox(f"Add component failed:\n\n{e}", "Add component", wx.OK | wx.ICON_WARNING)
            return

        self._refresh_top_status()
        self._rebuild_list()

    def _on_edit(self, _evt: wx.CommandEvent) -> None:
        idx = self._selected_row_index()
        if idx < 0:
            if self._selected_display_row():
                wx.MessageBox("Pending rows cannot be edited. Sync the library first.", "Edit component", wx.OK | wx.ICON_INFORMATION)
            else:
                wx.MessageBox("Select a row to edit.", "Edit component", wx.OK | wx.ICON_INFORMATION)
            return
        src = dict(self._rows[idx])
        ipn = str((src or {}).get("IPN", "") or "").strip()
        if ipn:
            try:
                # Block edits for parts already pending deletion.
                for p in PENDING.list_for(self._category.display_name):
                    if str(p.get("action") or "").strip() == "delete" and str(p.get("ipn") or "").strip() == ipn:
                        wx.MessageBox("This component is pending deletion. Sync the library first.", "Edit component", wx.OK | wx.ICON_INFORMATION)
                        return
            except Exception:
                pass

        form_headers = self._load_dbl_form_headers(add_mode=False)
        dlg = EditEntryDialog(self, self._repo_path, self._category, form_headers, src, self._rows)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            row = dlg.get_row()
        finally:
            dlg.Destroy()

        ipn = str((src or {}).get("IPN", "") or "").strip()
        if not ipn or ipn == "PENDING":
            wx.MessageBox("Pending rows cannot be edited. Sync the library first.", "Edit component", wx.OK | wx.ICON_INFORMATION)
            return

        set_fields: dict[str, str] = {}
        for h in (form_headers or []):
            if h == "IPN":
                continue
            a = str((src or {}).get(h, "") or "")
            b = str((row or {}).get(h, "") or "")
            if a != b:
                set_fields[h] = b
        if not set_fields:
            return

        cfg = Config.load_effective(self._repo_path)
        if not (cfg.github_owner.strip() and cfg.github_repo.strip()):
            wx.MessageBox("GitHub is not configured. Click Settings… first.", "Edit component", wx.OK | wx.ICON_WARNING)
            return
        try:
            msg = prompt_commit_message(self, default=f"request: update {ipn}")
            if msg is None:
                return
            req_path = submit_request(cfg, action="update", payload={"ipn": ipn, "set": dict(set_fields)}, commit_message=msg)
            try:
                br = (Config.load_effective(self._repo_path).github_base_branch.strip() or "main")
            except Exception:
                br = "main"
            try:
                origin_sha = (run_git(["git", "rev-parse", f"origin/{br}"], cwd=self._repo_path) or "").strip()
            except Exception:
                origin_sha = ""
            PENDING.add(
                self._category.display_name,
                {
                    "action": "update",
                    "ipn": ipn,
                    "set": dict(set_fields),
                    "created_at": time.time(),
                    "fetch_mtime_at_submit": (git_fetch_head_mtime(self._repo_path) or 0.0),
                    "origin_sha_at_submit": origin_sha,
                    "seen_remote": False,
                    "state": "submitted",
                    "req_path": req_path,
                },
            )
        except GitHubError as e:
            wx.MessageBox(f"GitHub submission failed:\n\n{e}", "Edit component", wx.OK | wx.ICON_WARNING)
            return
        except Exception as e:  # noqa: BLE001
            wx.MessageBox(f"Edit component failed:\n\n{e}", "Edit component", wx.OK | wx.ICON_WARNING)
            return

        self._refresh_top_status()
        self._rebuild_list()
        # Avoid wx.LogMessage popups; status is reflected via pending icons.

    def _on_delete(self, _evt: wx.CommandEvent) -> None:
        idx = self._selected_row_index()
        if idx < 0:
            if self._selected_display_row():
                wx.MessageBox("Pending rows cannot be deleted. Sync the library first.", "Delete component", wx.OK | wx.ICON_INFORMATION)
            else:
                wx.MessageBox("Select a row to delete.", "Delete component", wx.OK | wx.ICON_INFORMATION)
            return
        row = dict(self._rows[idx])
        label = row_label(row, self._headers)
        if wx.MessageBox(f"Delete this component?\n\n{label}", "Delete component", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING) != wx.YES:
            return
        ipn = str(row.get("IPN", "") or "").strip()
        if not ipn or ipn == "PENDING":
            wx.MessageBox("Pending rows cannot be deleted. Sync the library first.", "Delete component", wx.OK | wx.ICON_INFORMATION)
            return
        try:
            # Prevent duplicate delete requests for the same IPN.
            for p in PENDING.list_for(self._category.display_name):
                if str(p.get("action") or "").strip() == "delete" and str(p.get("ipn") or "").strip() == ipn:
                    wx.MessageBox("This component is already pending deletion.", "Delete component", wx.OK | wx.ICON_INFORMATION)
                    return
        except Exception:
            pass
        cfg = Config.load_effective(self._repo_path)
        if not (cfg.github_owner.strip() and cfg.github_repo.strip()):
            wx.MessageBox("GitHub is not configured. Click Settings… first.", "Delete component", wx.OK | wx.ICON_WARNING)
            return
        try:
            msg = prompt_commit_message(self, default=f"request: delete {ipn}")
            if msg is None:
                return
            req_path = submit_request(cfg, action="delete", payload={"ipn": ipn}, commit_message=msg)
            try:
                br = (Config.load_effective(self._repo_path).github_base_branch.strip() or "main")
            except Exception:
                br = "main"
            try:
                origin_sha = (run_git(["git", "rev-parse", f"origin/{br}"], cwd=self._repo_path) or "").strip()
            except Exception:
                origin_sha = ""
            PENDING.add(
                self._category.display_name,
                {
                    "action": "delete",
                    "ipn": ipn,
                    "created_at": time.time(),
                    "fetch_mtime_at_submit": (git_fetch_head_mtime(self._repo_path) or 0.0),
                    "origin_sha_at_submit": origin_sha,
                    "seen_remote": False,
                    "state": "submitted",
                    "req_path": req_path,
                },
            )
        except GitHubError as e:
            wx.MessageBox(f"GitHub submission failed:\n\n{e}", "Delete component", wx.OK | wx.ICON_WARNING)
            return
        except Exception as e:  # noqa: BLE001
            wx.MessageBox(f"Delete component failed:\n\n{e}", "Delete component", wx.OK | wx.ICON_WARNING)
            return

        self._refresh_top_status()
        self._rebuild_list()

    def _datasheet_value_for_row(self, row: dict[str, str]) -> str:
        """
        Best-effort: locate a datasheet field in the row.
        """
        # Prefer exact header matches first.
        candidates = {"datasheet", "datasheeturl", "datasheetlink", "datasheeturi"}
        for k, v in (row or {}).items():
            kk = str(k or "").strip()
            if not kk:
                continue
            norm = re.sub(r"[^a-z0-9]+", "", kk.lower())
            if norm in candidates:
                return str(v or "").strip()
        # Fallback: try common variants (spaces/underscores) in headers.
        for pref in ("Datasheet", "Datasheet URL", "DatasheetURL"):
            if pref in (row or {}):
                return str((row or {}).get(pref, "") or "").strip()
        return ""

    def _open_datasheet_value(self, value: str) -> bool:
        """
        Open a datasheet, which can be an URL or a local path (absolute or repo-relative).
        Returns True if a launch was attempted.
        """
        v = str(value or "").strip()
        if not v:
            return False

        # URL case.
        if v.startswith(("http://", "https://", "file://")):
            try:
                return bool(wx.LaunchDefaultBrowser(v))
            except Exception:
                return False

        # Local path case: try as-is, then relative to repo.
        cand: list[str] = [v]
        try:
            if self._repo_path:
                cand.append(os.path.join(self._repo_path, v))
        except Exception:
            pass
        for p in cand:
            try:
                ap = os.path.abspath(p)
            except Exception:
                ap = p
            try:
                if os.path.isfile(ap):
                    uri = Path(ap).resolve().as_uri()
                    return bool(wx.LaunchDefaultBrowser(uri))
            except Exception:
                continue
        return False

    def _on_open_datasheet(self, _evt) -> None:
        row = self._selected_display_row() or {}
        if not row:
            wx.MessageBox("Select a row first.", "Open datasheet", wx.OK | wx.ICON_INFORMATION)
            return
        val = self._datasheet_value_for_row(row)
        if not val:
            wx.MessageBox("No datasheet field found for this component.", "Open datasheet", wx.OK | wx.ICON_INFORMATION)
            return
        if not self._open_datasheet_value(val):
            wx.MessageBox(f"Could not open datasheet:\n\n{val}", "Open datasheet", wx.OK | wx.ICON_WARNING)
            return

    def _on_row_selected(self, _evt) -> None:
        self._update_previews(rebuild_choice=True)

    def _on_fp_choice_changed(self, _evt: wx.CommandEvent) -> None:
        # Re-render only; do not rebuild the dropdown.
        self._update_previews(rebuild_choice=False)

    def _ensure_fp_groups_started(self) -> None:
        """
        Start footprint cache in the background, and snapshot its grouping if available.
        Safe to call often.
        """
        try:
            from .footprints.libcache import FP_LIBCACHE

            FP_LIBCACHE.ensure_started(self._repo_path)
            st = FP_LIBCACHE.snapshot(self._repo_path)
            groups = st.get("footprint_groups") or {}
            if isinstance(groups, dict) and groups:
                # Normalize into plain dict[str, list[str]] and build reverse map once.
                gg: dict[str, list[str]] = {}
                rev: dict[str, str] = {}
                for base, vs in list(groups.items()):
                    b = str(base or "").strip()
                    if not b:
                        continue
                    try:
                        vv = [str(x or "").strip() for x in (vs or []) if str(x or "").strip()]
                    except Exception:
                        vv = []
                    if not vv:
                        continue
                    gg[b] = vv
                    for v in vv:
                        if v and v not in rev:
                            rev[v] = b
                self._fp_groups = gg
                self._fp_group_rev = rev
        except Exception:
            return

    def _expand_density_variants(self, fps: list[str]) -> list[str]:
        """
        If the component has a single footprint ref, expand to density variants using the shared cache.
        Otherwise keep the explicit list (e.g. multiple footprints separated by ';').
        """
        fps = [str(x or "").strip() for x in (fps or []) if str(x or "").strip()]
        if len(fps) != 1:
            return fps
        ref = fps[0]
        if not ref:
            return fps
        self._ensure_fp_groups_started()
        if ref in self._fp_groups:
            return list(self._fp_groups.get(ref) or [ref])
        base = self._fp_group_rev.get(ref) or ""
        if base and base in self._fp_groups:
            return list(self._fp_groups.get(base) or [ref])
        return fps

    def _update_previews(self, *, rebuild_choice: bool) -> None:
        if self._closing:
            return
        row = self._selected_display_row() or {}
        if not row:
            try:
                self._fp_prev.choice.Clear()
                self._fp_prev.choice.Enable(False)
                self._fp_prev.set_choice_visible(False)
            except Exception:
                pass
            self._fp_prev.set_empty()
            self._sym_prev.set_empty()
            return
        sym_ref = str(row.get("Symbol", "") or "").strip()
        fp_val = str(row.get("Footprint", "") or "").strip()
        fps_raw = [x.strip() for x in fp_val.split(";") if x.strip()] if fp_val else []
        fps = self._expand_density_variants(fps_raw)

        if rebuild_choice:
            # Preserve current selection if possible.
            try:
                prev_sel = str(self._fp_prev.choice.GetStringSelection() or "").strip()
            except Exception:
                prev_sel = ""
            try:
                self._fp_prev.choice.Clear()
                for x in fps:
                    self._fp_prev.choice.Append(x)
                if fps:
                    self._fp_prev.choice.Enable(True)
                    if prev_sel and prev_sel in fps:
                        self._fp_prev.choice.SetStringSelection(prev_sel)
                    else:
                        self._fp_prev.choice.SetSelection(0)
                else:
                    self._fp_prev.choice.Enable(False)
            except Exception:
                pass
            self._fp_prev.set_choice_visible(len(fps) > 1)

        # Render footprint
        fp_ref = ""
        try:
            if self._fp_prev.choice.IsEnabled() and self._fp_prev.choice.GetSelection() != wx.NOT_FOUND:
                fp_ref = str(self._fp_prev.choice.GetStringSelection() or "").strip()
        except Exception:
            fp_ref = ""
        if not fp_ref and fps:
            fp_ref = fps[0]
        if fp_ref:
            from .footprints.ops import find_footprint_mod_any, render_footprint_svg

            mtime = "0"
            try:
                if ":" in fp_ref:
                    lib, fpname = fp_ref.split(":", 1)
                    mod = find_footprint_mod_any(self._repo_path, lib, fpname)
                    mtime = str(os.path.getmtime(mod)) if mod and os.path.exists(mod) else "0"
            except Exception:
                mtime = "0"
            self._fp_prev.render_cached_svg_async(
                kind_dir="fp",
                cache_key_prefix="fp_browse_component",
                ref=fp_ref,
                source_mtime=mtime,
                render_svg=lambda r, p: render_footprint_svg(self._repo_path, r, p),
                quality_scale=2.5,
            )
        else:
            self._fp_prev.set_empty()

        # Render symbol
        if sym_ref:
            from .symbols.libcache import resolve_symbol_lib_path
            from .symbols.ops import render_symbol_svg

            mtime = "0"
            try:
                if ":" in sym_ref:
                    lib = sym_ref.split(":", 1)[0].strip()
                    lib_path = resolve_symbol_lib_path(self._repo_path, lib) or ""
                    mtime = str(os.path.getmtime(lib_path)) if lib_path and os.path.exists(lib_path) else "0"
            except Exception:
                mtime = "0"
            self._sym_prev.render_cached_svg_async(
                kind_dir="sym",
                cache_key_prefix="sym_browse_component",
                ref=sym_ref,
                source_mtime=mtime,
                render_svg=lambda r, p: render_symbol_svg(self._repo_path, r, p),
                quality_scale=2.5,
            )
        else:
            self._sym_prev.set_empty()


class ComponentPickerDialog(wx.Dialog):
    """
    Modal "picker mode" component browser, used by "Copy from existing…".

    This reuses the same visual building blocks as the component browser (DataView list + previews),
    but runs as a modal dialog so it works from within Add/Edit dialogs.
    """

    def __init__(self, parent: wx.Window, *, repo_path: str, category: Category, title: str = "Copy from existing"):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX)
        self._repo_path = str(repo_path or "")
        self._category = category
        self._closing = False

        self._headers: list[str] = []
        self._rows: list[dict[str, str]] = []
        self._visible_idx: list[int] = []
        self._row_texts: list[str] = []
        self._show_cols: list[str] = []
        self._selected: dict[str, str] | None = None

        self._remote_by_ipn: dict[str, dict[str, str]] | None = None
        self._remote_loaded = False
        self._remote_loading = False

        try:
            table = load_csv_table(self._category.csv_path)
            self._headers = list(table.headers or [])
            self._rows = list(table.rows or [])
        except Exception:
            self._headers = []
            self._rows = []

        # Status icon bundles (same palette as the main component browser).
        self._bb_green = wx.BitmapBundle.FromBitmap(make_status_bitmap(wx.Colour(46, 160, 67)))
        self._bb_red = wx.BitmapBundle.FromBitmap(make_status_bitmap(wx.Colour(220, 53, 69)))
        self._bb_yellow = wx.BitmapBundle.FromBitmap(make_status_bitmap(wx.Colour(255, 193, 7)))
        self._bb_blue = wx.BitmapBundle.FromBitmap(make_status_bitmap(wx.Colour(13, 110, 253)))
        self._bb_gray = wx.BitmapBundle.FromBitmap(make_status_bitmap(wx.Colour(160, 160, 160)))
        self._show_cols = [""] + self._dbl_cols()
        self._build_row_texts()

        root = wx.BoxSizer(wx.VERTICAL)

        # Top: library status (like the main component browser, but read-only here).
        self._bmp_green = make_status_bitmap(wx.Colour(46, 160, 67))
        self._bmp_red = make_status_bitmap(wx.Colour(220, 53, 69))
        self._bmp_yellow = make_status_bitmap(wx.Colour(255, 193, 7))
        self._bmp_blue = make_status_bitmap(wx.Colour(13, 110, 253))
        self._bmp_gray = make_status_bitmap(wx.Colour(160, 160, 160))

        top = wx.BoxSizer(wx.HORIZONTAL)
        self.status_icon = wx.StaticBitmap(self, bitmap=self._bmp_gray)
        self.status_lbl = wx.StaticText(self, label="")
        top.Add(self.status_icon, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        top.Add(self.status_lbl, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        top.AddStretchSpacer(1)
        self.fetch_btn = wx.Button(self, label="↓  Fetch remote")
        self.sync_btn = wx.Button(self, label="↻  Sync library")
        top.Add(self.fetch_btn, 0, wx.ALL, 6)
        top.Add(self.sync_btn, 0, wx.ALL, 6)
        root.Add(top, 0, wx.EXPAND)

        self._search = wx.TextCtrl(self)
        try:
            self._search.SetHint("Filter (IPN / MPN / Manufacturer / Value / Description)")
        except Exception:
            pass
        root.Add(self._search, 0, wx.ALL | wx.EXPAND, 8)

        splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)
        left = wx.Panel(splitter)
        right = wx.Panel(splitter)

        left_s = wx.BoxSizer(wx.VERTICAL)
        self._list = dv.DataViewListCtrl(left, style=dv.DV_ROW_LINES | dv.DV_VERT_RULES | dv.DV_HORIZ_RULES | dv.DV_SINGLE)
        left_s.Add(self._list, 1, wx.ALL | wx.EXPAND, 0)
        left.SetSizer(left_s)

        right_scroll = wx.ScrolledWindow(right, style=wx.VSCROLL)
        right_scroll.SetScrollRate(0, 10)
        right_s = wx.BoxSizer(wx.VERTICAL)

        fp_box = wx.StaticBoxSizer(wx.VERTICAL, right_scroll, "Footprint preview")
        self._fp_prev = PreviewPanel(right_scroll, empty_label="(select a row)", show_choice=True, min_bitmap_size=(-1, 320))
        fp_box.Add(self._fp_prev, 1, wx.ALL | wx.EXPAND, 0)
        right_s.Add(fp_box, 1, wx.ALL | wx.EXPAND, 6)

        sym_box = wx.StaticBoxSizer(wx.VERTICAL, right_scroll, "Symbol preview")
        self._sym_prev = PreviewPanel(right_scroll, empty_label="(select a row)", show_choice=False, crop_to_alpha=True, min_bitmap_size=(-1, 320))
        sym_box.Add(self._sym_prev, 1, wx.ALL | wx.EXPAND, 0)
        right_s.Add(sym_box, 1, wx.ALL | wx.EXPAND, 6)

        right_scroll.SetSizer(right_s)
        right_outer = wx.BoxSizer(wx.VERTICAL)
        right_outer.Add(right_scroll, 1, wx.EXPAND)
        right.SetSizer(right_outer)

        splitter.SplitVertically(left, right, sashPosition=760)
        splitter.SetMinimumPaneSize(350)
        # Avoid negative sash positions (can assert on some wx ports, notably macOS).
        def _set_default_sash() -> None:
            try:
                w = int(splitter.GetClientSize().GetWidth() or 0)
            except Exception:
                w = 0
            right_w = 350
            try:
                pos = max(350, w - right_w) if w else 760
                splitter.SetSashPosition(pos)
            except Exception:
                pass

        wx.CallAfter(_set_default_sash)
        root.Add(splitter, 1, wx.ALL | wx.EXPAND, 8)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        use_btn = wx.Button(self, label="Use selected")
        cancel_btn = wx.Button(self, label="Cancel")
        btns.AddStretchSpacer(1)
        btns.Add(use_btn, 0, wx.ALL, 6)
        btns.Add(cancel_btn, 0, wx.ALL, 6)
        root.Add(btns, 0, wx.EXPAND)

        self.SetSizer(root)
        self.SetMinSize((1200, 750))
        self.SetSize((1400, 900))

        self._search.Bind(wx.EVT_TEXT, self._on_search)
        self._list.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self._on_row_selected)
        self._list.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, self._on_use_selected)
        self._fp_prev.choice.Bind(wx.EVT_CHOICE, lambda _e: self._update_previews(rebuild_choice=False))
        use_btn.Bind(wx.EVT_BUTTON, self._on_use_selected)
        cancel_btn.Bind(wx.EVT_BUTTON, lambda _e: self.EndModal(wx.ID_CANCEL))
        self.fetch_btn.Bind(wx.EVT_BUTTON, self._on_fetch_remote)
        self.sync_btn.Bind(wx.EVT_BUTTON, self._on_sync_library)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        try:
            self.Bind(wx.EVT_WINDOW_DESTROY, self._on_close)
        except Exception:
            pass

        try:
            use_btn.SetDefault()
        except Exception:
            pass

        self._refresh_top_status()
        self._ensure_remote_loaded_async()
        self._rebuild_list()

    def _set_busy(self, busy: bool, msg: str = "") -> None:
        try:
            self.fetch_btn.Enable(not busy)
            self.sync_btn.Enable(not busy)
        except Exception:
            pass
        if msg:
            try:
                self.status_lbl.SetLabel(str(msg))
                self.status_icon.SetBitmap(self._bmp_gray)
            except Exception:
                pass

    def _on_fetch_remote(self, _evt: wx.CommandEvent) -> None:
        branch = (Config.load_effective(self._repo_path).github_base_branch.strip() or "main")
        self._set_busy(True, "Fetching remote...")

        def worker() -> None:
            err: Exception | None = None
            try:
                run_git(["git", "-C", self._repo_path, "fetch", "origin", branch, "--quiet"], cwd=self._repo_path)
            except Exception as e:  # noqa: BLE001
                err = e

            def done() -> None:
                if self._closing or not is_window_alive(self):
                    return
                self._set_busy(False, "")
                if err:
                    wx.MessageBox(f"Fetch remote failed:\n\n{err}", "Fetch remote", wx.OK | wx.ICON_WARNING)
                try:
                    fm = float(git_fetch_head_mtime(self._repo_path) or 0.0)
                except Exception:
                    fm = 0.0
                try:
                    update_pending_states_after_fetch(self._repo_path, category_name=self._category.display_name, branch=branch, fetch_mtime=fm)
                except Exception:
                    pass
                # Force per-row status refresh.
                self._remote_loaded = False
                self._remote_by_ipn = None
                self._remote_loading = False
                self._ensure_remote_loaded_async()
                self._refresh_top_status()
                self._rebuild_list()

            wx.CallAfter(done)

        threading.Thread(target=worker, daemon=True).start()

    def _on_sync_library(self, _evt: wx.CommandEvent) -> None:
        branch = (Config.load_effective(self._repo_path).github_base_branch.strip() or "main")
        self._set_busy(True, "Syncing library...")

        def worker() -> None:
            err: Exception | None = None
            try:
                git_sync_ff_only(self._repo_path, branch=branch)
            except Exception as e:  # noqa: BLE001
                err = e

            def done() -> None:
                if self._closing or not is_window_alive(self):
                    return
                self._set_busy(False, "")
                if err:
                    wx.MessageBox(str(err), "Sync failed", wx.OK | wx.ICON_WARNING)
                # Reload local CSV then refresh status/icons.
                try:
                    table = load_csv_table(self._category.csv_path)
                    self._headers = list(table.headers or [])
                    self._rows = list(table.rows or [])
                    self._show_cols = [""] + self._dbl_cols()
                    self._build_row_texts()
                except Exception:
                    pass
                self._remote_loaded = False
                self._remote_by_ipn = None
                self._remote_loading = False
                self._ensure_remote_loaded_async()
                self._refresh_top_status()
                self._rebuild_list()

            wx.CallAfter(done)

        threading.Thread(target=worker, daemon=True).start()

    def _on_close(self, evt: wx.Event) -> None:
        self._closing = True
        try:
            evt.Skip()
        except Exception:
            pass

    def _refresh_top_status(self) -> None:
        """
        Read-only status strip (same semantics as BrowseDialog).
        """
        try:
            st = git_sync_status(self._repo_path)
            stale = bool(st.get("stale"))
            dirty = bool(st.get("dirty"))
            if stale:
                age = st.get("age")
                suffix = f" (last fetch {format_age_minutes(age)})" if age is not None else ""
                self.status_icon.SetBitmap(self._bmp_gray)
                self.status_lbl.SetLabel("Library status: unknown / stale" + suffix)
            elif bool(st.get("up_to_date")):
                self.status_icon.SetBitmap(self._bmp_green)
                br = (Config.load_effective(self._repo_path).github_base_branch.strip() or "main")
                self.status_lbl.SetLabel(f"Library status: synchronized with origin/{br}")
            elif dirty:
                self.status_icon.SetBitmap(self._bmp_yellow)
                self.status_lbl.SetLabel("Library status: local changes (uncommitted)")
            else:
                behind = st.get("behind")
                self.status_icon.SetBitmap(self._bmp_red)
                if isinstance(behind, int):
                    self.status_lbl.SetLabel(f"Library status: out of date (behind {behind})")
                else:
                    self.status_lbl.SetLabel("Library status: out of date")
        except Exception as exc:  # noqa: BLE001
            try:
                self.status_icon.SetBitmap(self._bmp_red)
                self.status_lbl.SetLabel(f"Library status: unavailable ({exc})")
            except Exception:
                pass

        # Pending overrides status color (pending beats green/red).
        try:
            has_pend, applied = pending_tag_for_category(self._category.display_name)
            behind = None
            try:
                behind = st.get("behind")  # type: ignore[name-defined]
            except Exception:
                behind = None
            if has_pend:
                if applied and isinstance(behind, int) and behind > 0:
                    self.status_icon.SetBitmap(self._bmp_blue)
                    self.status_lbl.SetLabel("Library status: sync needed")
                else:
                    self.status_icon.SetBitmap(self._bmp_yellow)
                    self.status_lbl.SetLabel("Library status: pending changes")
        except Exception:
            pass

        try:
            self.status_lbl.Wrap(max(200, self.GetClientSize().width - 80))
        except Exception:
            pass

    def _ensure_remote_loaded_async(self) -> None:
        """
        Load origin/<branch> CSV for this category in background (for per-row status icons).
        Uses FETCH_HEAD refs (no network).
        """
        if self._remote_loaded or self._remote_loading:
            return
        age = git_fetch_head_age_seconds(self._repo_path)
        if age is None or age > fetch_stale_threshold_seconds(self._repo_path):
            # Stale/unknown: don't try loading remote file.
            return
        self._remote_loading = True

        def worker() -> None:
            remote_by_ipn: dict[str, dict[str, str]] | None = None
            try:
                br = (Config.load_effective(self._repo_path).github_base_branch.strip() or "main")
                spec = f"origin/{br}:Database/{self._category.filename}"
                txt = run_git(["git", "-C", self._repo_path, "show", spec], cwd=self._repo_path)
                rdr = csv.DictReader((txt or "").splitlines())
                rows = [dict(r) for r in rdr]
                remote_by_ipn = {}
                for r in rows:
                    ipn = str((r or {}).get("IPN", "") or "").strip()
                    if ipn:
                        remote_by_ipn[ipn] = {k: str(v or "") for k, v in (r or {}).items()}
            except Exception:
                remote_by_ipn = None

            def done_on_ui() -> None:
                try:
                    if self._closing or not is_window_alive(self):
                        return
                    self._remote_by_ipn = remote_by_ipn
                    self._remote_loaded = True
                finally:
                    self._remote_loading = False
                try:
                    self._rebuild_list()
                except Exception:
                    pass

            wx.CallAfter(done_on_ui)

        threading.Thread(target=worker, daemon=True).start()

    def _row_status_bundle(self, row: dict[str, str], *, pending_update_state: str = "") -> wx.BitmapBundle:
        """
        Best-effort per-row status vs origin/<branch>.
        - gray: unknown/stale/remote not loaded
        - blue: remote loading in progress
        - green: identical to origin/<branch> row
        - yellow: new row (not on origin/<branch>) OR pending change submitted
        - red: differs from origin/<branch>
        """
        # Pending update takes precedence.
        if pending_update_state:
            return self._bb_blue if pending_update_state == "applied_remote" else self._bb_yellow

        if not self._remote_loaded:
            return self._bb_blue if self._remote_loading else self._bb_gray
        if not self._remote_by_ipn:
            return self._bb_gray
        ipn = str((row or {}).get("IPN", "") or "").strip()
        if not ipn:
            return self._bb_gray
        remote = self._remote_by_ipn.get(ipn)
        if not remote:
            return self._bb_yellow
        for h in self._show_cols[1:]:
            if str((row.get(h, "") or "")).strip() != str((remote.get(h, "") or "")).strip():
                return self._bb_red
        return self._bb_green

    def _dbl_cols(self) -> list[str]:
        cat = str(getattr(self._category, "display_name", "") or "").strip()
        p = os.path.join(self._repo_path, "Database", "category_fields", f"{cat}.json")
        try:
            with open(p, "r", encoding="utf-8") as f:
                body = json.load(f) or {}
            fields = list((body or {}).get("fields") or [])
        except Exception:
            fields = []

        ordered: list[str] = []
        if fields:
            for fd in fields:
                if not isinstance(fd, dict):
                    continue
                col = str(fd.get("column") or fd.get("name") or "").strip()
                if col:
                    ordered.append(col)

        if "IPN" in ordered:
            ordered = ["IPN"] + [h for h in ordered if h != "IPN"]

        # Fallback to CSV headers.
        if not ordered:
            headers = list(self._headers or [])
            if "IPN" in headers:
                headers = ["IPN"] + [h for h in headers if h != "IPN"]
            ordered = [str(h or "").strip() for h in headers if str(h or "").strip()]

        # Ensure required columns exist.
        for r in ("IPN", "Symbol", "Footprint"):
            if r not in ordered:
                ordered.insert(0, r) if r == "IPN" else ordered.append(r)

        # Dedupe.
        out: list[str] = []
        seen: set[str] = set()
        for h in ordered:
            hh = str(h or "").strip()
            if not hh or hh in seen:
                continue
            seen.add(hh)
            out.append(hh)
        return out

    def get_selected_row(self) -> dict[str, str] | None:
        return dict(self._selected or {}) if self._selected else None

    def _build_row_texts(self) -> None:
        cols = list(self._show_cols[1:] or [])
        texts: list[str] = []
        for row in (self._rows or []):
            parts = [str((row or {}).get(h, "") or "") for h in cols]
            texts.append(" ".join([p for p in parts if p]).lower())
        self._row_texts = texts

    def _on_search(self, _evt: wx.CommandEvent) -> None:
        self._rebuild_list()

    def _on_row_selected(self, _evt=None) -> None:
        self._capture_selected()
        self._update_previews(rebuild_choice=True)

    def _capture_selected(self) -> None:
        try:
            sel = int(self._list.GetSelectedRow())
        except Exception:
            sel = -1
        if sel < 0 or sel >= len(self._visible_idx):
            self._selected = None
            return
        src_i = int(self._visible_idx[sel])
        try:
            self._selected = dict(self._rows[src_i] or {})
        except Exception:
            self._selected = None

    def _rebuild_list(self) -> None:
        q = (self._search.GetValue() or "").strip().lower()
        self._visible_idx = []

        # Pending actions for this category: don't allow copying from rows pending deletion,
        # and show pending-update status as yellow/blue.
        pend = PENDING.list_for(self._category.display_name)
        pending_delete = {
            str(p.get("ipn") or "").strip()
            for p in (pend or [])
            if str(p.get("action") or "").strip() == "delete" and str(p.get("ipn") or "").strip()
        }
        pending_update = {
            str(p.get("ipn") or "").strip(): p
            for p in (pend or [])
            if str(p.get("action") or "").strip() == "update" and str(p.get("ipn") or "").strip()
        }

        try:
            self._list.Freeze()
        except Exception:
            pass
        try:
            try:
                self._list.DeleteAllItems()
            except Exception:
                pass
            try:
                self._list.ClearColumns()
            except Exception:
                pass

            self._list.AppendIconTextColumn("", width=32)
            for col in self._show_cols[1:]:
                self._list.AppendTextColumn(str(col or ""), width=wx.COL_WIDTH_AUTOSIZE)

            for i, row in enumerate(self._rows or []):
                if q and (q not in (self._row_texts[i] if i < len(self._row_texts) else "")):
                    continue
                ipn = str((row or {}).get("IPN", "") or "").strip()
                if ipn and ipn in pending_delete:
                    continue
                st = ""
                try:
                    if ipn and ipn in pending_update:
                        st = str((pending_update[ipn] or {}).get("state") or "submitted").strip()
                except Exception:
                    st = ""
                bb = self._row_status_bundle(dict(row or {}), pending_update_state=st)
                icon_cell = dv.DataViewIconText("", bb)
                vals = [icon_cell] + [str((row or {}).get(h, "") or "") for h in self._show_cols[1:]]
                self._list.AppendItem(vals)
                self._visible_idx.append(i)
        finally:
            try:
                self._list.Thaw()
            except Exception:
                pass

        if self._list.GetItemCount() > 0:
            try:
                self._list.SelectRow(0)
            except Exception:
                pass
            # Selecting the first row programmatically does not always emit
            # EVT_DATAVIEW_SELECTION_CHANGED on all platforms/wx builds.
            # Ensure the preview reflects the default selection.
            try:
                self._on_row_selected(None)
            except Exception:
                pass
        else:
            # Ensure previews clear when the list becomes empty (e.g. after filtering).
            try:
                self._selected = None
            except Exception:
                pass
            try:
                self._update_previews(rebuild_choice=True)
            except Exception:
                pass

    def _on_use_selected(self, _evt=None) -> None:
        self._capture_selected()
        if not self._selected:
            wx.MessageBox("Select a row first.", self.GetTitle(), wx.OK | wx.ICON_INFORMATION)
            return
        self.EndModal(wx.ID_OK)

    def _update_previews(self, *, rebuild_choice: bool) -> None:
        row = dict(self._selected or {})
        if not row:
            try:
                self._fp_prev.choice.Clear()
                self._fp_prev.choice.Enable(False)
                self._fp_prev.set_choice_visible(False)
            except Exception:
                pass
            self._fp_prev.set_empty()
            self._sym_prev.set_empty()
            return

        sym_ref = str(row.get("Symbol", "") or "").strip()
        fp_val = str(row.get("Footprint", "") or "").strip()
        fps = [x.strip() for x in fp_val.split(";") if x.strip()] if fp_val else []

        if rebuild_choice:
            try:
                prev_sel = str(self._fp_prev.choice.GetStringSelection() or "").strip()
            except Exception:
                prev_sel = ""
            try:
                self._fp_prev.choice.Clear()
                for x in fps:
                    self._fp_prev.choice.Append(x)
                if fps:
                    self._fp_prev.choice.Enable(True)
                    if prev_sel and prev_sel in fps:
                        self._fp_prev.choice.SetStringSelection(prev_sel)
                    else:
                        self._fp_prev.choice.SetSelection(0)
                else:
                    self._fp_prev.choice.Enable(False)
            except Exception:
                pass
            self._fp_prev.set_choice_visible(len(fps) > 1)

        fp_ref = ""
        try:
            if self._fp_prev.choice.IsEnabled() and self._fp_prev.choice.GetSelection() != wx.NOT_FOUND:
                fp_ref = str(self._fp_prev.choice.GetStringSelection() or "").strip()
        except Exception:
            fp_ref = ""
        if not fp_ref and fps:
            fp_ref = fps[0]

        if fp_ref:
            from .footprints.ops import find_footprint_mod_any, render_footprint_svg

            mtime = "0"
            try:
                if ":" in fp_ref:
                    lib, fpname = fp_ref.split(":", 1)
                    mod = find_footprint_mod_any(self._repo_path, lib, fpname)
                    mtime = str(os.path.getmtime(mod)) if mod and os.path.exists(mod) else "0"
            except Exception:
                mtime = "0"
            self._fp_prev.render_cached_svg_async(
                kind_dir="fp",
                cache_key_prefix="fp_copy_from_existing",
                ref=fp_ref,
                source_mtime=mtime,
                render_svg=lambda r, p: render_footprint_svg(self._repo_path, r, p),
                quality_scale=2.5,
            )
        else:
            self._fp_prev.set_empty()

        if sym_ref:
            from .symbols.libcache import resolve_symbol_lib_path
            from .symbols.ops import render_symbol_svg

            mtime = "0"
            try:
                if ":" in sym_ref:
                    lib = sym_ref.split(":", 1)[0].strip()
                    lib_path = resolve_symbol_lib_path(self._repo_path, lib) or ""
                    mtime = str(os.path.getmtime(lib_path)) if lib_path and os.path.exists(lib_path) else "0"
            except Exception:
                mtime = "0"
            self._sym_prev.render_cached_svg_async(
                kind_dir="sym",
                cache_key_prefix="sym_copy_from_existing",
                ref=sym_ref,
                source_mtime=mtime,
                render_svg=lambda r, p: render_symbol_svg(self._repo_path, r, p),
                quality_scale=2.5,
            )
        else:
            self._sym_prev.set_empty()
