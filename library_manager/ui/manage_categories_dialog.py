from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
from dataclasses import dataclass

import wx
import wx.dataview as dv

from ..config import Config
from ..repo import Category, list_categories
from .async_ui import UiRepeater, is_window_alive
from .git_ops import (
    git_fetch_head_mtime,
    git_object_exists,
    git_sync_ff_only,
    git_sync_status,
    run_git,
)
from .icons import make_status_bitmap
from .pending import PENDING, reconcile_pending_against_local_csv, update_pending_states_after_fetch
from .requests import prompt_commit_message, submit_request


def _repo_categories_yml_path(repo_path: str) -> str:
    return os.path.join(repo_path, "Database", "categories.yml")


def _repo_cat_fields_cfg_path(repo_path: str, cat_name: str) -> str:
    return os.path.join(repo_path, "Database", "category_fields", f"{cat_name}.json")


@dataclass(frozen=True)
class _PrefixSpec:
    prefix: str
    width: int


def _parse_categories_yml(repo_path: str) -> dict[str, _PrefixSpec]:
    """
    Parse Database/categories.yml (very small, simple YAML subset).
    """
    p = _repo_categories_yml_path(repo_path)
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = list(f.readlines())
    except Exception:
        return {}

    out: dict[str, _PrefixSpec] = {}
    cur_cat: str | None = None
    cur_prefix: str | None = None
    cur_width: int | None = None
    for raw in lines:
        s = raw.rstrip("\n")
        if not s.strip() or s.lstrip().startswith("#"):
            continue
        if not s.startswith(" "):
            # new category
            cur_cat = s.split(":", 1)[0].strip()
            cur_prefix = None
            cur_width = None
            continue
        if cur_cat:
            m = re.match(r"^\s*prefix:\s*(.+)\s*$", s)
            if m:
                v = (m.group(1) or "").strip().strip('"').strip("'")
                cur_prefix = v
            m2 = re.match(r"^\s*width:\s*(\d+)\s*$", s)
            if m2:
                try:
                    cur_width = int(m2.group(1))
                except Exception:
                    cur_width = None
            if cur_prefix is not None and cur_width is not None:
                out[cur_cat] = _PrefixSpec(prefix=cur_prefix, width=cur_width)
    return out


def _read_csv_headers(csv_path: str) -> list[str]:
    try:
        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            r = csv.reader(f)
            return list(next(r, []) or [])
    except Exception:
        return []


def _read_cat_fields_cfg(repo_path: str, cat_name: str) -> dict[str, dict]:
    """
    Return {column: fieldDef} from Database/category_fields/<cat>.json
    """
    p = _repo_cat_fields_cfg_path(repo_path, cat_name)
    try:
        with open(p, "r", encoding="utf-8") as f:
            body = json.load(f)
        fields = list((body or {}).get("fields") or [])
    except Exception:
        fields = []
    out: dict[str, dict] = {}
    for fd in fields:
        if not isinstance(fd, dict):
            continue
        col = str(fd.get("column") or fd.get("name") or "").strip()
        if not col:
            continue
        out[col] = dict(fd)
    return out


class _CategoryWizardDialog(wx.Dialog):
    def __init__(
        self,
        parent: wx.Window,
        *,
        title: str,
        cat_name: str = "",
        allow_rename: bool = True,
        require_prefix: bool = False,
        prefix: str = "",
        width: int = 7,
        fields: list[dict] | None = None,
    ):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX)
        self._result: tuple[str, str, int, list[dict]] | None = None
        self._required_fields = {"IPN", "Symbol", "Footprint", "Value", "Description"}
        self._require_prefix = bool(require_prefix)

        vbox = wx.BoxSizer(wx.VERTICAL)

        grid = wx.FlexGridSizer(cols=2, vgap=8, hgap=10)
        grid.AddGrowableCol(1, 1)
        vbox.Add(grid, 0, wx.ALL | wx.EXPAND, 10)

        grid.Add(wx.StaticText(self, label="Category"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._cat = wx.TextCtrl(self, value=str(cat_name or ""))
        if not allow_rename:
            self._cat.Enable(False)
        grid.Add(self._cat, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label="Prefix"), 0, wx.ALIGN_CENTER_VERTICAL)
        # UI prefix should be entered WITHOUT the trailing dash; we add "-" automatically.
        pfx_ui = str(prefix or "").strip()
        if pfx_ui.endswith("-"):
            pfx_ui = pfx_ui[:-1].strip()
        self._prefix = wx.TextCtrl(self, value=pfx_ui)
        try:
            self._prefix.SetHint('Example: "IND" (a trailing "-" will be added automatically)')
        except Exception:
            pass
        grid.Add(self._prefix, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label="Width"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._width = wx.SpinCtrl(self, min=1, max=12, initial=int(width or 7))
        grid.Add(self._width, 0)

        vbox.Add(wx.StaticText(self, label="Fields"), 0, wx.LEFT | wx.RIGHT, 10)

        self._fields = dv.DataViewListCtrl(self, style=dv.DV_ROW_LINES | dv.DV_VERT_RULES | dv.DV_HORIZ_RULES)
        self._fields.AppendTextColumn("Field", width=260)
        self._fields.AppendToggleColumn("Visible on add", width=130)
        self._fields.AppendToggleColumn("Visible in chooser", width=150)
        vbox.Add(self._fields, 1, wx.ALL | wx.EXPAND, 10)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        add_f = wx.Button(self, label="Add custom field…")
        rm_f = wx.Button(self, label="Remove selected field")
        up_f = wx.Button(self, label="Move up")
        dn_f = wx.Button(self, label="Move down")
        add_f.Bind(wx.EVT_BUTTON, self._on_add_custom)
        rm_f.Bind(wx.EVT_BUTTON, self._on_remove_field)
        up_f.Bind(wx.EVT_BUTTON, self._on_move_up)
        dn_f.Bind(wx.EVT_BUTTON, self._on_move_down)
        btn_row.Add(add_f, 0, wx.RIGHT, 8)
        btn_row.Add(rm_f, 0, wx.RIGHT, 8)
        btn_row.Add(up_f, 0, wx.RIGHT, 8)
        btn_row.Add(dn_f, 0)
        btn_row.AddStretchSpacer(1)
        vbox.Add(btn_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        ok = wx.Button(self, id=wx.ID_OK, label="OK")
        cancel = wx.Button(self, id=wx.ID_CANCEL, label="Cancel")
        ok.Bind(wx.EVT_BUTTON, self._on_ok)
        h = wx.BoxSizer(wx.HORIZONTAL)
        h.AddStretchSpacer(1)
        h.Add(ok, 0, wx.ALL, 8)
        h.Add(cancel, 0, wx.ALL, 8)
        vbox.Add(h, 0, wx.EXPAND)

        self.SetSizer(vbox)
        self.SetMinSize((760, 560))
        self.SetSize((920, 700))

        if fields:
            for fd in list(fields or []):
                try:
                    col = str(fd.get("name") or fd.get("column") or "").strip()
                except Exception:
                    col = ""
                if not col:
                    continue
                try:
                    voa = bool(fd.get("visible_on_add", False))
                except Exception:
                    voa = False
                try:
                    vic = bool(fd.get("visible_in_chooser", True))
                except Exception:
                    vic = True
                try:
                    self._fields.AppendItem([col, voa, vic])
                except Exception:
                    pass
        else:
            self._populate_defaults()

    def _populate_defaults(self) -> None:
        # Mandatory (match legacy ui.py).
        self._add_row("IPN", False, True)
        self._add_row("Symbol", False, False)
        self._add_row("Footprint", False, False)
        self._add_row("Value", False, True)
        self._add_row("Description", False, True)

        # Optional defaults (enabled in chooser; hidden on add by default).
        for name in [
            "MPN",
            "Manufacturer",
            "Datasheet",
            "Operating Temperature",
            "RoHS Status",
            "Supplier",
            "Supplier Part Number",
        ]:
            self._add_row(name, False, True)

    def _add_row(self, name: str, visible_on_add: bool, visible_in_chooser: bool) -> None:
        try:
            self._fields.AppendItem([str(name or "").strip(), bool(visible_on_add), bool(visible_in_chooser)])
        except Exception:
            pass

    def _on_add_custom(self, _evt: wx.CommandEvent) -> None:
        dlg = wx.TextEntryDialog(self, "Field name", "Add custom field")
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            name = (dlg.GetValue() or "").strip()
            if not name:
                return
            # Prevent duplicates.
            try:
                for i in range(int(self._fields.GetItemCount())):
                    if str(self._fields.GetTextValue(i, 0) or "").strip() == name:
                        return
            except Exception:
                pass
            self._add_row(name, False, True)
        finally:
            dlg.Destroy()

    def _on_remove_field(self, _evt: wx.CommandEvent) -> None:
        try:
            sel = int(self._fields.GetSelectedRow())
        except Exception:
            sel = wx.NOT_FOUND
        if sel == wx.NOT_FOUND:
            return
        try:
            name = str(self._fields.GetTextValue(sel, 0) or "").strip()
        except Exception:
            name = ""
        if name in self._required_fields:
            wx.MessageBox("Cannot remove a required field.", "Category fields", wx.OK | wx.ICON_INFORMATION)
            return
        try:
            self._fields.DeleteItem(sel)
        except Exception:
            pass

    def _swap_rows(self, a: int, b: int) -> None:
        if a == b:
            return
        if a < 0 or b < 0:
            return
        try:
            n = int(self._fields.GetItemCount())
        except Exception:
            n = 0
        if a >= n or b >= n:
            return
        try:
            va = [
                str(self._fields.GetTextValue(a, 0) or ""),
                bool(self._fields.GetToggleValue(a, 1)),
                bool(self._fields.GetToggleValue(a, 2)),
            ]
            vb = [
                str(self._fields.GetTextValue(b, 0) or ""),
                bool(self._fields.GetToggleValue(b, 1)),
                bool(self._fields.GetToggleValue(b, 2)),
            ]
        except Exception:
            return
        lo, hi = (a, b) if a < b else (b, a)
        try:
            self._fields.DeleteItem(hi)
            self._fields.DeleteItem(lo)
        except Exception:
            return
        first = vb if lo == a else va
        second = va if lo == a else vb
        try:
            self._fields.InsertItem(lo, first)
            self._fields.InsertItem(hi, second)
        except Exception:
            try:
                # Fallback: append if insert isn't available on this build.
                cur = []
                cur.append((lo, first))
                cur.append((hi, second))
                cur.sort(key=lambda x: x[0])
                for _idx, row in cur:
                    self._fields.AppendItem(row)
            except Exception:
                pass
        try:
            self._fields.SelectRow(b)
        except Exception:
            pass

    def _on_move_up(self, _evt: wx.CommandEvent) -> None:
        try:
            sel = int(self._fields.GetSelectedRow())
        except Exception:
            sel = wx.NOT_FOUND
        if sel == wx.NOT_FOUND or sel <= 0:
            return
        self._swap_rows(sel, sel - 1)

    def _on_move_down(self, _evt: wx.CommandEvent) -> None:
        try:
            sel = int(self._fields.GetSelectedRow())
        except Exception:
            sel = wx.NOT_FOUND
        try:
            n = int(self._fields.GetItemCount())
        except Exception:
            n = 0
        if sel == wx.NOT_FOUND or sel >= (n - 1):
            return
        self._swap_rows(sel, sel + 1)

    def _on_ok(self, _evt: wx.CommandEvent) -> None:
        cat = (self._cat.GetValue() or "").strip()
        prefix_in = (self._prefix.GetValue() or "").strip()
        # Normalize: user types "ASC" and we store "ASC-".
        prefix_in = prefix_in[:-1].strip() if prefix_in.endswith("-") else prefix_in
        prefix = (prefix_in + "-") if prefix_in else ""
        try:
            width = int(self._width.GetValue())
        except Exception:
            width = 7

        fields: list[dict] = []
        seen: set[str] = set()
        try:
            n = int(self._fields.GetItemCount())
        except Exception:
            n = 0
        for i in range(n):
            try:
                name = str(self._fields.GetTextValue(i, 0) or "").strip()
            except Exception:
                name = ""
            if not name or name in seen:
                continue
            seen.add(name)
            try:
                voa = bool(self._fields.GetToggleValue(i, 1))
            except Exception:
                voa = False
            try:
                vic = bool(self._fields.GetToggleValue(i, 2))
            except Exception:
                vic = True
            fields.append({"name": name, "visible_on_add": voa, "visible_in_chooser": vic})

        if not cat:
            wx.MessageBox("Category name is required.", "Edit category", wx.OK | wx.ICON_WARNING)
            return
        if self._require_prefix and not prefix_in:
            wx.MessageBox("Prefix is required for a new category.", "Edit category", wx.OK | wx.ICON_WARNING)
            return
        if "/" in cat or "\\" in cat:
            wx.MessageBox("Category name must not contain slashes.", "Edit category", wx.OK | wx.ICON_WARNING)
            return
        if not fields:
            wx.MessageBox("At least one field is required.", "Edit category", wx.OK | wx.ICON_WARNING)
            return
        # Ensure required fields exist.
        names = {str(f.get("name") or "").strip() for f in fields}
        missing = sorted([x for x in self._required_fields if x not in names])
        if missing:
            wx.MessageBox(f"Missing required field(s): {', '.join(missing)}", "Edit category", wx.OK | wx.ICON_WARNING)
            return

        self._result = (cat, prefix, width, fields)
        self.EndModal(wx.ID_OK)

    def get_result(self) -> tuple[str, str, int, list[dict]]:
        if not self._result:
            raise RuntimeError("No result")
        return self._result


class ManageCategoriesDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, repo_path: str):
        super().__init__(parent, title="Manage categories", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX)
        self._repo_path = repo_path
        self._cfg = Config.load_effective(self._repo_path)
        self._categories: list[Category] = []
        self._closing = False
        self._last_fetch_mtime = float(git_fetch_head_mtime(self._repo_path) or 0.0)
        self._watch_repeater: UiRepeater | None = None

        self._bmp_green = make_status_bitmap(wx.Colour(46, 160, 67))
        self._bmp_red = make_status_bitmap(wx.Colour(220, 53, 69))
        self._bmp_yellow = make_status_bitmap(wx.Colour(255, 193, 7))
        self._bmp_blue = make_status_bitmap(wx.Colour(13, 110, 253))
        self._bmp_gray = make_status_bitmap(wx.Colour(160, 160, 160))

        vbox = wx.BoxSizer(wx.VERTICAL)

        top = wx.BoxSizer(wx.HORIZONTAL)
        self._status_icon = wx.StaticBitmap(self, bitmap=self._bmp_gray)
        self._status_lbl = wx.StaticText(self, label="Library status: unknown")
        top.Add(self._status_icon, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        top.Add(self._status_lbl, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        top.AddStretchSpacer(1)
        self._fetch_btn = wx.Button(self, label="↓  Fetch remote")
        self._sync_btn = wx.Button(self, label="🗘  Sync library")
        self._fetch_btn.Bind(wx.EVT_BUTTON, self._on_fetch_remote)
        self._sync_btn.Bind(wx.EVT_BUTTON, self._on_sync_library)
        top.Add(self._fetch_btn, 0, wx.ALL, 6)
        top.Add(self._sync_btn, 0, wx.ALL, 6)
        vbox.Add(top, 0, wx.EXPAND)

        vbox.Add(wx.StaticText(self, label="Fields used across categories"), 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self._table = dv.DataViewListCtrl(self, style=dv.DV_ROW_LINES | dv.DV_VERT_RULES | dv.DV_HORIZ_RULES)
        self._table.AppendIconTextColumn("Category", width=240, mode=dv.DATAVIEW_CELL_INERT)
        self._table.AppendTextColumn("Status", width=120, mode=dv.DATAVIEW_CELL_INERT)
        vbox.Add(self._table, 1, wx.ALL | wx.EXPAND, 8)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        add_btn = wx.Button(self, label="Add category…")
        edit_btn = wx.Button(self, label="Edit category…")
        del_btn = wx.Button(self, label="Delete category…")
        close_btn = wx.Button(self, id=wx.ID_CANCEL, label="Close")
        add_btn.Bind(wx.EVT_BUTTON, self._on_add)
        edit_btn.Bind(wx.EVT_BUTTON, self._on_edit)
        del_btn.Bind(wx.EVT_BUTTON, self._on_delete)
        btns.Add(add_btn, 0, wx.ALL, 6)
        btns.Add(edit_btn, 0, wx.ALL, 6)
        btns.Add(del_btn, 0, wx.ALL, 6)
        btns.AddStretchSpacer(1)
        btns.Add(close_btn, 0, wx.ALL, 6)
        vbox.Add(btns, 0, wx.EXPAND)

        self.SetSizer(vbox)
        self.SetMinSize((980, 650))
        self.SetSize((1250, 800))

        self._reload()
        self._start_watch()
        self.Bind(wx.EVT_CLOSE, self._on_close)
        try:
            self.Bind(wx.EVT_WINDOW_DESTROY, self._on_close)
        except Exception:
            pass
    def _on_close(self, evt: wx.Event) -> None:
        self._closing = True
        try:
            if getattr(self, "_watch_repeater", None):
                self._watch_repeater.stop()  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            evt.Skip()
        except Exception:
            pass

    def _start_watch(self) -> None:
        if getattr(self, "_watch_repeater", None):
            return

        def tick() -> None:
            if self._closing or not is_window_alive(self):
                return
            # If FETCH_HEAD mtime changes, a fetch happened (manual or background).
            try:
                fm = float(git_fetch_head_mtime(self._repo_path) or 0.0)
            except Exception:
                fm = 0.0
            if fm <= float(getattr(self, "_last_fetch_mtime", 0.0) or 0.0):
                return
            self._last_fetch_mtime = fm
            br = (self._cfg.github_base_branch or "main").strip() or "main"
            try:
                for cat_name, _items in (PENDING.items_by_category() or {}).items():
                    try:
                        update_pending_states_after_fetch(self._repo_path, category_name=str(cat_name), branch=br, fetch_mtime=fm)
                    except Exception:
                        continue
            except Exception:
                pass
            # Refresh UI to reflect pending->sync needed transitions.
            try:
                self._reload()
            except Exception:
                pass

        self._watch_repeater = UiRepeater(self, interval_ms=1000, callback=tick)

    def _selected_category_name(self) -> str | None:
        """
        Return selected category name from the matrix.

        Note: column 0 is a DataViewIconText, so GetTextValue() can be unreliable on some wx builds.
        """
        row = -1
        # Prefer selection->row mapping (most reliable across wx ports).
        try:
            item = self._table.GetSelection()
            if item and item.IsOk():
                row = int(self._table.ItemToRow(item))
        except Exception:
            row = -1
        if row < 0:
            try:
                row = int(self._table.GetSelectedRow())
            except Exception:
                row = -1
        if row < 0:
            return None

        # Extract the category text from col0 (IconText).
        try:
            v = self._table.GetValue(row, 0)
            if hasattr(v, "GetText"):
                s = str(v.GetText() or "").strip()
                return s.strip() or None
        except Exception:
            pass
        try:
            s2 = str(self._table.GetTextValue(row, 0) or "").strip()
            return s2.strip() or None
        except Exception:
            return None

    def _set_busy(self, busy: bool, msg: str | None = None) -> None:
        try:
            self._fetch_btn.Enable(not busy)
            self._sync_btn.Enable(not busy)
        except Exception:
            pass
        if msg is not None:
            try:
                self._status_lbl.SetLabel(str(msg))
            except Exception:
                pass

    def _refresh_top_status(self) -> None:
        try:
            st = git_sync_status(self._repo_path)
        except Exception:
            st = {"stale": True}

        stale = bool(st.get("stale"))
        if stale:
            age = st.get("age")
            suffix = f" (last fetch {age}s ago)" if age is not None else ""
            self._status_icon.SetBitmap(self._bmp_gray)
            self._status_lbl.SetLabel("Library status: unknown / stale — click Fetch remote" + suffix)
            return

        # Pending (any) beats red.
        any_pending = False
        any_applied = False
        try:
            for _cat, items in (PENDING.items_by_category() or {}).items():
                pend = list(items or [])
                if not pend:
                    continue
                any_pending = True
                if any(str(p.get("state") or "") == "applied_remote" for p in pend):
                    any_applied = True
        except Exception:
            pass

        if any_applied:
            self._status_icon.SetBitmap(self._bmp_blue)
            self._status_lbl.SetLabel("Library status: sync needed")
            return
        if any_pending:
            self._status_icon.SetBitmap(self._bmp_yellow)
            self._status_lbl.SetLabel("Library status: pending changes")
            return

        # Otherwise green if up_to_date.
        if bool(st.get("up_to_date")):
            br = (self._cfg.github_base_branch or "main").strip() or "main"
            self._status_icon.SetBitmap(self._bmp_green)
            self._status_lbl.SetLabel(f"Library status: synchronized with origin/{br}")
        else:
            behind = st.get("behind")
            self._status_icon.SetBitmap(self._bmp_red)
            if isinstance(behind, int):
                self._status_lbl.SetLabel(f"Library status: out of date (behind {behind})")
            else:
                self._status_lbl.SetLabel("Library status: out of date")

    def _icon_for_status(self, status_txt: str) -> wx.Bitmap:
        s = (status_txt or "").strip().lower()
        if s == "pending":
            return self._bmp_yellow
        if s == "sync needed":
            return self._bmp_blue
        return self._bmp_green

    def _reload(self) -> None:
        # Preserve selection across reloads.
        prev_sel = None
        try:
            r = int(self._table.GetSelectedRow())
            if r != wx.NOT_FOUND and r >= 0:
                prev_sel = str(self._table.GetTextValue(r, 0) or "").strip() or None
        except Exception:
            prev_sel = None
        if prev_sel:
            prev_sel = prev_sel.strip()

        self._categories = list_categories(self._repo_path)

        # Build category x field usage matrix (like legacy ui.py).
        fields_by_cat: list[tuple[str, set[str]]] = []
        all_fields: set[str] = set()
        existing_names: set[str] = set()

        for cat in (self._categories or []):
            name = str(getattr(cat, "display_name", "") or "").strip()
            if not name:
                continue
            existing_names.add(name)
            # Determine fields from DBL config (category_fields/<cat>.json), not CSV headers.
            # CSV can contain historical columns that are no longer shown in DBL.
            used = set()
            try:
                cfg_by_col = _read_cat_fields_cfg(self._repo_path, name)
                used = {str(k or "").strip() for k in (cfg_by_col or {}).keys() if str(k or "").strip()}
            except Exception:
                used = set()
            if not used:
                # Fallback for legacy/missing config: use CSV headers.
                hdr = _read_csv_headers(str(getattr(cat, "csv_path", "") or ""))
                used = {str(x or "").strip() for x in (hdr or []) if str(x or "").strip()}
            fields_by_cat.append((name, used))
            all_fields |= used

        # Pending category adds that are not yet present locally should still appear (yellow/blue).
        try:
            for cat_name, items in sorted((PENDING.items_by_category() or {}).items(), key=lambda kv: str(kv[0] or "").lower()):
                name = str(cat_name or "").strip()
                if not name or name in existing_names:
                    continue
                pend = list(items or [])
                if not any(str(p.get("action") or "").strip() == "category_add" for p in pend):
                    continue
                # Try to recover requested fields from the pending request payload.
                req_fields: list[dict] = []
                for p in pend:
                    if str(p.get("action") or "").strip() == "category_add":
                        try:
                            req_fields = list(p.get("fields") or [])
                        except Exception:
                            req_fields = []
                        break
                used = set()
                for fd in (req_fields or []):
                    if not isinstance(fd, dict):
                        continue
                    col = str(fd.get("name") or fd.get("column") or "").strip()
                    if col:
                        used.add(col)
                fields_by_cat.append((name, used))
                all_fields |= used
        except Exception:
            pass

        preferred = [
            "IPN",
            "Symbol",
            "Footprint",
            "Value",
            "Description",
            "Datasheet",
            "MPN",
            "Manufacturer",
        ]
        cols = [c for c in preferred if c in all_fields]
        cols.extend(sorted([c for c in all_fields if c not in set(preferred)], key=lambda s: (s or "").lower()))

        self._table.Freeze()
        try:
            self._table.DeleteAllItems()
        except Exception:
            pass
        try:
            self._table.ClearColumns()
        except Exception:
            pass

        # Recreate columns.
        self._table.AppendIconTextColumn("Category", width=220, mode=dv.DATAVIEW_CELL_INERT)
        self._table.AppendTextColumn("Status", width=120, mode=dv.DATAVIEW_CELL_INERT)
        for name in cols:
            w = max(90, min(180, 10 * max(6, len(name))))
            self._table.AppendTextColumn(name, width=w, mode=dv.DATAVIEW_CELL_INERT)

        def _status_for_cat(cat_name: str) -> str:
            try:
                pend = PENDING.list_for(cat_name)
                if not pend:
                    return ""
                applied = any(str(p.get("state") or "") == "applied_remote" for p in pend)
                return "sync needed" if applied else "pending"
            except Exception:
                return ""

        # Insert rows.
        for cat_name, used in fields_by_cat:
            status_txt = _status_for_cat(cat_name)
            bmp = self._icon_for_status(status_txt)
            ico = wx.Icon()
            try:
                ico.CopyFromBitmap(bmp)
            except Exception:
                ico = wx.Icon()
            it = dv.DataViewIconText("  " + str(cat_name or "").strip(), ico)
            row = [it, status_txt]
            for f in cols:
                row.append("✖" if f in used else "")
            try:
                self._table.AppendItem(row)
            except Exception:
                pass

        self._table.Thaw()

        # Restore selection if possible.
        if prev_sel:
            try:
                for i in range(int(self._table.GetItemCount())):
                    if str(self._table.GetTextValue(i, 0) or "").strip() == prev_sel:
                        self._table.SelectRow(i)
                        break
            except Exception:
                pass

        self._refresh_top_status()

    def _on_fetch_remote(self, _evt: wx.CommandEvent) -> None:
        br = (self._cfg.github_base_branch or "main").strip() or "main"
        self._set_busy(True, "Fetching remote...")

        def worker() -> None:
            err: Exception | None = None
            try:
                run_git(["git", "fetch", "origin", br, "--quiet"], cwd=self._repo_path)
                fm = git_fetch_head_mtime(self._repo_path)
                if fm is not None:
                    for cat_name, _items in (PENDING.items_by_category() or {}).items():
                        try:
                            update_pending_states_after_fetch(self._repo_path, category_name=str(cat_name), branch=br, fetch_mtime=float(fm))
                        except Exception:
                            continue
            except Exception as e:  # noqa: BLE001
                err = e

            def done() -> None:
                if not is_window_alive(self):
                    return
                self._set_busy(False)
                if err:
                    wx.MessageBox(str(err), "Fetch remote failed", wx.OK | wx.ICON_WARNING)
                self._reload()
                try:
                    parent = self.GetParent()
                    if parent and hasattr(parent, "_refresh_sync_status"):
                        parent._refresh_sync_status()  # type: ignore[misc]
                    if parent and hasattr(parent, "_reload_category_statuses"):
                        parent._reload_category_statuses()  # type: ignore[misc]
                    if parent and hasattr(parent, "_refresh_categories_status_icon"):
                        parent._refresh_categories_status_icon()  # type: ignore[misc]
                except Exception:
                    pass

            wx.CallAfter(done)

        threading.Thread(target=worker, daemon=True).start()

    def _on_sync_library(self, _evt: wx.CommandEvent) -> None:
        br = (self._cfg.github_base_branch or "main").strip() or "main"
        self._set_busy(True, "Syncing library...")

        def worker() -> None:
            err: Exception | None = None
            try:
                git_sync_ff_only(self._repo_path, branch=br)
                # After sync/pull updates local CSVs, clear pending items reflected locally.
                try:
                    cats = list_categories(self._repo_path)
                except Exception:
                    cats = []
                try:
                    for cat in cats:
                        cat_name = str(getattr(cat, "display_name", "") or "").strip()
                        if not cat_name or not PENDING.has_any(cat_name):
                            continue
                        local_by_ipn: dict[str, dict[str, str]] = {}
                        try:
                            with open(cat.csv_path, "r", encoding="utf-8", newline="") as f:
                                rdr = csv.DictReader(f)
                                for rr in rdr:
                                    ipn = str((rr or {}).get("IPN", "") or "").strip()
                                    if ipn:
                                        local_by_ipn[ipn] = dict(rr)
                        except Exception:
                            local_by_ipn = {}
                        # Best-effort: resolve pending adds against local CSV so they can clear.
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
                            pass

                    # Also drop applied_remote category_* requests once synced (request file removed from HEAD).
                    try:
                        for cat_name, items in (PENDING.items_by_category() or {}).items():
                            pend = list(items or [])
                            if not pend:
                                continue
                            kept: list[dict] = []
                            for p in pend:
                                act = str(p.get("action") or "").strip()
                                if act not in ("category_add", "category_delete", "category_update"):
                                    kept.append(p)
                                    continue
                                st0 = str(p.get("state") or "").strip()
                                rp = str(p.get("req_path") or "").strip()
                                if st0 == "applied_remote" and rp:
                                    try:
                                        if not git_object_exists(self._repo_path, f"HEAD:{rp}"):
                                            continue
                                    except Exception:
                                        pass
                                kept.append(p)
                            if kept != pend:
                                PENDING.set_items(str(cat_name), kept)
                    except Exception:
                        pass
                except Exception:
                    pass
            except Exception as e:  # noqa: BLE001
                err = e

            def done() -> None:
                if not is_window_alive(self):
                    return
                self._set_busy(False)
                if err:
                    wx.MessageBox(str(err), "Sync failed", wx.OK | wx.ICON_WARNING)
                self._reload()
                try:
                    parent = self.GetParent()
                    if parent and hasattr(parent, "_refresh_sync_status"):
                        parent._refresh_sync_status()  # type: ignore[misc]
                    if parent and hasattr(parent, "_reload_category_statuses"):
                        parent._reload_category_statuses()  # type: ignore[misc]
                    if parent and hasattr(parent, "_refresh_remote_cat_updated_times_async"):
                        parent._refresh_remote_cat_updated_times_async()  # type: ignore[misc]
                    if parent and hasattr(parent, "_refresh_categories_status_icon"):
                        parent._refresh_categories_status_icon()  # type: ignore[misc]
                except Exception:
                    pass

            wx.CallAfter(done)

        threading.Thread(target=worker, daemon=True).start()
    def _on_add(self, _evt: wx.CommandEvent) -> None:
        cfg = Config.load_effective(self._repo_path)
        if not (cfg.github_owner.strip() and cfg.github_repo.strip()):
            wx.MessageBox("GitHub is not configured. Click Settings… first.", "KiCad Library Manager", wx.OK | wx.ICON_WARNING)
            return
        wiz = _CategoryWizardDialog(self, title="New category", cat_name="", allow_rename=True, require_prefix=True, prefix="", width=7, fields=[])
        try:
            if wiz.ShowModal() != wx.ID_OK:
                return
            cat_name, prefix, width, fields = wiz.get_result()
        finally:
            wiz.Destroy()

        msg = prompt_commit_message(self, default=f"request: add category {cat_name}")
        if msg is None:
            return
        req_path = submit_request(
            cfg,
            action="category_add",
            payload={"category": cat_name, "prefix": prefix, "width": width, "fields": fields},
            commit_message=msg,
        )
        PENDING.add(
            cat_name,
            {
                "action": "category_add",
                "fields": list(fields or []),
                "prefix": prefix,
                "width": int(width or 7),
                "created_at": time.time(),
                "fetch_mtime_at_submit": (git_fetch_head_mtime(self._repo_path) or 0.0),
                "state": "submitted",
                "req_path": req_path,
            },
        )
        self._reload()

    def _on_edit(self, _evt: wx.CommandEvent) -> None:
        cat = self._selected_category_name()
        if not cat:
            wx.MessageBox("Select a category first.", "Edit category", wx.OK | wx.ICON_INFORMATION)
            return
        if wx.MessageBox(
            "Editing a category affects all users and can break existing projects.\n\n"
            "Only proceed if you are sure.\n\nContinue?",
            "Edit category (warning)",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        ) != wx.YES:
            return

        cfg = Config.load_effective(self._repo_path)
        if not (cfg.github_owner.strip() and cfg.github_repo.strip()):
            wx.MessageBox("GitHub is not configured. Click Settings… first.", "Edit category", wx.OK | wx.ICON_WARNING)
            return

        # Prefill from DBL config (category_fields/<cat>.json) + categories.yml prefix.
        pref_spec = _parse_categories_yml(self._repo_path).get(cat)
        prefix = (pref_spec.prefix if pref_spec else "")
        width = (pref_spec.width if pref_spec else 7)

        required = ["IPN", "Symbol", "Footprint", "Value", "Description"]

        # Build field list from DBL config (ordered list), so removed fields don't reappear.
        fields: list[dict] = []
        try:
            cfg_path = _repo_cat_fields_cfg_path(self._repo_path, cat)
            with open(cfg_path, "r", encoding="utf-8") as f:
                body = json.load(f) or {}
            cfg_fields = list(body.get("fields") or [])
        except Exception:
            cfg_fields = []

        if cfg_fields:
            for fd in cfg_fields:
                if not isinstance(fd, dict):
                    continue
                name = str(fd.get("name") or fd.get("column") or "").strip()
                if not name:
                    continue
                fields.append(
                    {
                        "name": name,
                        "visible_on_add": bool(fd.get("visible_on_add", False)),
                        "visible_in_chooser": bool(fd.get("visible_in_chooser", True)),
                    }
                )
        else:
            # Fallback: legacy/missing config → use CSV headers.
            csv_path = os.path.join(self._repo_path, "Database", f"db-{cat}.csv")
            headers = _read_csv_headers(csv_path)
            for h in (headers or []):
                hh = str(h or "").strip()
                if not hh:
                    continue
                fields.append({"name": hh, "visible_on_add": False, "visible_in_chooser": True})

        # Ensure required fields exist (edit UI shouldn't allow losing them).
        have = {str(f.get("name") or "").strip() for f in fields}
        for r in required:
            if r not in have:
                fields.insert(0, {"name": r, "visible_on_add": False, "visible_in_chooser": True if r in {"IPN", "Value"} else False})
                have.add(r)

        wiz = _CategoryWizardDialog(
            self,
            title=f"Edit category: {cat}",
            cat_name=cat,
            allow_rename=False,
            prefix=prefix,
            width=width,
            fields=fields,
        )
        try:
            if wiz.ShowModal() != wx.ID_OK:
                return
            cat_name, new_prefix, new_width, new_fields = wiz.get_result()
        finally:
            wiz.Destroy()

        msg = prompt_commit_message(self, default=f"request: update category {cat_name}")
        if msg is None:
            return
        req_path = submit_request(
            cfg,
            action="category_update",
            payload={"category": cat_name, "prefix": new_prefix, "width": int(new_width or 7), "fields": new_fields},
            commit_message=msg,
        )
        PENDING.add(
            cat_name,
            {
                "action": "category_update",
                "fields": list(new_fields or []),
                "prefix": new_prefix,
                "width": int(new_width or 7),
                "created_at": time.time(),
                "fetch_mtime_at_submit": (git_fetch_head_mtime(self._repo_path) or 0.0),
                "state": "submitted",
                "req_path": req_path,
            },
        )
        self._reload()

    def _on_delete(self, _evt: wx.CommandEvent) -> None:
        cat = self._selected_category_name()
        if not cat:
            wx.MessageBox("Select a category first.", "Delete category", wx.OK | wx.ICON_INFORMATION)
            return
        # Prevent duplicate delete requests while a pending delete is already in flight.
        try:
            pend = PENDING.list_for(cat)
            if any(str(p.get("action") or "") == "category_delete" for p in (pend or [])):
                wx.MessageBox(
                    "This category already has a pending delete request.\n\n"
                    "Fetch remote to see when it is applied, then Sync library to remove it locally.",
                    "Delete category",
                    wx.OK | wx.ICON_INFORMATION,
                )
                return
        except Exception:
            pass
        if wx.MessageBox(
            f"Delete category '{cat}'?\n\nThis will remove Database/db-{cat}.csv and its DBL field configuration.\nProceed only if you are sure.",
            "Confirm delete",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        ) != wx.YES:
            return

        cfg = Config.load_effective(self._repo_path)
        if not (cfg.github_owner.strip() and cfg.github_repo.strip()):
            wx.MessageBox("GitHub is not configured. Click Settings… first.", "Delete category", wx.OK | wx.ICON_WARNING)
            return
        msg = prompt_commit_message(self, default=f"request: delete category {cat}")
        if msg is None:
            return
        req_path = submit_request(cfg, action="category_delete", payload={"category": cat}, commit_message=msg)
        PENDING.add(
            cat,
            {
                "action": "category_delete",
                "created_at": time.time(),
                "fetch_mtime_at_submit": (git_fetch_head_mtime(self._repo_path) or 0.0),
                "state": "submitted",
                "req_path": req_path,
            },
        )
        self._reload()
