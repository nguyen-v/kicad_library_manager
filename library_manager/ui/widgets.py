from __future__ import annotations

from typing import Callable

import wx

from .preview_panel import PreviewPanel
from .async_ui import UiDebouncer


class SearchPickerDialog(wx.Dialog):
    """
    Generic picker for symbol/footprint references.
    """

    def __init__(self, parent: wx.Window, title: str, values: list[str], initial: str = ""):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._all_values = sorted(set(values))
        self._selected = ""

        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(wx.StaticText(self, label="Filter"), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self._filter = wx.TextCtrl(self, value=initial)
        root.Add(self._filter, 0, wx.ALL | wx.EXPAND, 8)

        self._list = wx.ListBox(self, choices=[])
        root.Add(self._list, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        btns = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        root.Add(btns, 0, wx.ALL | wx.ALIGN_RIGHT, 8)

        self.SetSizer(root)
        self.SetMinSize((560, 420))

        self._filter.Bind(wx.EVT_TEXT, self._on_filter)
        self._list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_dclick)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

        self._rebuild()

    def _on_filter(self, _evt: wx.CommandEvent) -> None:
        self._rebuild()

    def _rebuild(self) -> None:
        needle = (self._filter.GetValue() or "").strip().lower()
        if needle:
            values = [v for v in self._all_values if needle in v.lower()]
        else:
            values = list(self._all_values)
        self._list.Set(values)
        if values:
            self._list.SetSelection(0)

    def _on_dclick(self, _evt: wx.CommandEvent) -> None:
        self._capture_selected()
        if self._selected:
            self.EndModal(wx.ID_OK)

    def _on_ok(self, _evt: wx.CommandEvent) -> None:
        self._capture_selected()
        self.EndModal(wx.ID_OK)

    def _capture_selected(self) -> None:
        sel = self._list.GetStringSelection()
        self._selected = str(sel or "").strip()

    def get_selected(self) -> str:
        return self._selected


def _split_semicolon_list(s: str) -> list[str]:
    return [x.strip() for x in str(s or "").split(";") if x.strip()]


def _join_semicolon_list(values: list[str]) -> str:
    out: list[str] = []
    seen: set[str] = set()
    for v in list(values or []):
        vv = str(v or "").strip()
        if not vv or vv in seen:
            continue
        seen.add(vv)
        out.append(vv)
    return ";".join(out)


class MultiFootprintField(wx.Panel):
    """
    Multi-footprint selector (stores value as ';'-separated string like ui.py).
    """

    def __init__(
        self,
        parent: wx.Window,
        repo_path: str,
        footprints: list[str],
        *,
        value: str = "",
        on_create_footprint: Callable[[], str] | None = None,
        on_changed: Callable[[], None] | None = None,
    ):
        super().__init__(parent)
        self._repo_path = str(repo_path or "")
        self._footprints = sorted(set(footprints or []))
        self._create_footprint_cb = on_create_footprint
        self._on_changed = on_changed
        self._closing = False
        self._render_pending = False

        self._values: list[str] = []

        def _on_destroy(evt=None):
            # EVT_WINDOW_DESTROY is fired for children too; only mark closing if *this* panel is destroying.
            try:
                w = None
                if evt is not None and hasattr(evt, "GetWindow"):
                    w = evt.GetWindow()
                if w is None and evt is not None and hasattr(evt, "GetEventObject"):
                    w = evt.GetEventObject()
                if w is not None and w is not self:
                        evt.Skip()
                        return
            except Exception:
                pass
            self._closing = True
            try:
                if evt is not None:
                    evt.Skip()
            except Exception:
                pass

        try:
            self.Bind(wx.EVT_WINDOW_DESTROY, _on_destroy)
        except Exception:
            pass

        root = wx.BoxSizer(wx.VERTICAL)

        # One row per footprint with a remove button (scrollable, fixed-ish height).
        self._rows_host = wx.ScrolledWindow(self, style=wx.VSCROLL)
        try:
            self._rows_host.SetScrollRate(0, 12)
        except Exception:
            pass
        self._rows_inner = wx.Panel(self._rows_host)
        self._rows_sizer = wx.BoxSizer(wx.VERTICAL)
        self._rows_inner.SetSizer(self._rows_sizer)
        try:
            # Show ~3 rows by default.
            fixed_h = int(4 * 30 + 10)
            self._rows_host.SetMinSize((-1, fixed_h))
            # Also cap the height so vertical dialog resizing doesn't stretch this area.
            self._rows_host.SetMaxSize((-1, fixed_h))
        except Exception:
            pass
        host = wx.BoxSizer(wx.VERTICAL)
        host.Add(self._rows_inner, 1, wx.EXPAND)
        self._rows_host.SetSizer(host)
        root.Add(self._rows_host, 0, wx.TOP | wx.EXPAND, 6)

        actions = wx.BoxSizer(wx.HORIZONTAL)
        actions.AddStretchSpacer(1)
        add_btn = wx.Button(self, label="Add footprint")
        actions.Add(add_btn, 0)
        root.Add(actions, 0, wx.TOP | wx.EXPAND, 6)
        self.SetSizer(root)

        add_btn.Bind(wx.EVT_BUTTON, self._on_add)

        self.set_value(value)

    def _destroy_window_later(self, w: wx.Window) -> None:
        """
        Never destroy wx widgets inline in event handlers that originate from those widgets.
        It can lead to use-after-free / re-entrancy issues in wx (and you've already seen
        timer-related UAF crashes). We defer destruction to the UI event queue.
        """
        try:
            if not w or w.IsBeingDeleted():
                return
        except Exception:
            pass
        try:
            # Preferred if available.
            w.DestroyLater()  # type: ignore[attr-defined]
            return
        except Exception:
            pass
        try:
            wx.CallAfter(lambda: (None if (not w or w.IsBeingDeleted()) else w.Destroy()))
        except Exception:
            pass

    def _request_render_rows(self) -> None:
        if self._closing:
            return
        if self._render_pending:
            return
        self._render_pending = True

        def _do():
            try:
                if self._closing:
                    return
                try:
                    self._render_rows()
                except Exception:
                    pass
            finally:
                self._render_pending = False

        try:
            wx.CallAfter(_do)
        except Exception:
            # Fallback: do it inline (best-effort), but this path should be rare.
            _do()

    def _notify_changed(self) -> None:
        if self._on_changed:
            try:
                self._on_changed()
            except Exception:
                pass

    def set_footprint_choices(self, footprints: list[str]) -> None:
        self._footprints = sorted(set(footprints or []))

    def _on_add(self, _evt: wx.CommandEvent) -> None:
        # Use the full footprint browser (tree + preview) in picker mode.
        repo_path = str(self._repo_path or "")
        if not repo_path:
            dlg = SearchPickerDialog(self, "Select footprint", self._footprints, initial="")
            try:
                if dlg.ShowModal() != wx.ID_OK:
                    return
                selected = dlg.get_selected()
            finally:
                dlg.Destroy()
            if selected:
                self.append_refs([selected])
            return

        try:
            from .footprints.browser_dialog import FootprintBrowserDialog

            dlg = FootprintBrowserDialog(self, repo_path, picker_mode=True)
            try:
                if dlg.ShowModal() != wx.ID_OK:
                    return
                selected_refs = dlg.get_picked_refs()
            finally:
                dlg.Destroy()
        except Exception:
            selected_refs = []
        if selected_refs:
            self.append_refs(selected_refs)

    def append_refs(self, refs: list[str]) -> None:
        cur = list(self._values or [])
        merged = list(dict.fromkeys([*(cur or []), *[str(r or "").strip() for r in (refs or []) if str(r or "").strip()]]))
        merged = [x for x in merged if str(x or "").strip()]
        self._values = merged
        self._notify_changed()
        self._request_render_rows()

    def set_value(self, value: str) -> None:
        self._values = _split_semicolon_list(value)
        self._notify_changed()
        self._request_render_rows()

    def get_value(self) -> str:
        return _join_semicolon_list(list(self._values or []))

    def get_values_list(self) -> list[str]:
        return list(self._values or [])

    def _remove_ref(self, to_remove: str) -> None:
        if self._closing:
            return
        tr = str(to_remove or "").strip()
        if not tr:
            return
        self._values = [x for x in (self._values or []) if str(x or "").strip() != tr]
        self._notify_changed()
        self._request_render_rows()

    def _render_rows(self) -> None:
        try:
            for child in list(self._rows_inner.GetChildren()):
                try:
                    child.Hide()
                    self._destroy_window_later(child)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self._rows_sizer.Clear(delete_windows=False)
        except Exception:
            pass

        for v in list(self._values or []):
            vv = str(v or "").strip()
            if not vv:
                continue
            row = wx.Panel(self._rows_inner)
            s = wx.BoxSizer(wx.HORIZONTAL)
            lbl = wx.StaticText(row, label=vv)
            s.Add(lbl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
            rm = wx.Button(row, label="❌", size=(40, -1))
            try:
                rm.SetToolTip("Remove footprint")
            except Exception:
                pass
            s.Add(rm, 0)

            def on_rm(evt: wx.CommandEvent, to_remove: str = vv) -> None:
                if self._closing:
                    return
                # Disable immediately to prevent double-click re-entrancy.
                try:
                    btn = evt.GetEventObject()
                    if btn:
                        btn.Enable(False)
                except Exception:
                    pass
                try:
                    wx.CallAfter(self._remove_ref, to_remove)
                except Exception:
                    self._remove_ref(to_remove)

            rm.Bind(wx.EVT_BUTTON, on_rm)
            row.SetSizer(s)
            self._rows_sizer.Add(row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 2)

        try:
            self._rows_inner.Layout()
        except Exception:
            pass
        try:
            self._rows_host.FitInside()
        except Exception:
            pass
        # Propagate layout so the parent sizers recalculate row heights.
        try:
            self.Layout()
        except Exception:
            pass
        try:
            p = self.GetParent()
            if p:
                # Avoid scheduling layout after destroy (can crash wx).
                try:
                    if not p.IsBeingDeleted():
                        p.Layout()
                except Exception:
                    pass
        except Exception:
            pass


class ComponentFormPanel(wx.Panel):
    """
    Shared form used by Add and Edit dialogs, with ui.py-like previews.
    """

    def __init__(
        self,
        parent: wx.Window,
        repo_path: str,
        headers: list[str],
        row: dict[str, str],
        symbols: list[str],
        footprints: list[str],
        on_create_footprint: Callable[[], str] | None = None,
    ):
        super().__init__(parent)
        self._repo_path = str(repo_path or "")
        self._headers = headers
        self._symbols = sorted(set(symbols or []))
        self._footprints = sorted(set(footprints or []))
        self._create_footprint_cb = on_create_footprint
        self._ctrls: dict[str, wx.Control] = {}

        self._preview_pending = False
        self._closed = False
        self._preview_debouncer = UiDebouncer(self, delay_ms=250, callback=lambda: self._on_preview_timer(None))

        def _on_destroy(evt=None):
            # EVT_WINDOW_DESTROY may be observed when children are destroyed; only close if *we* are destroying.
            try:
                w = None
                if evt is not None and hasattr(evt, "GetWindow"):
                    w = evt.GetWindow()
                if w is None and evt is not None and hasattr(evt, "GetEventObject"):
                    w = evt.GetEventObject()
                if w is not None and w is not self:
                    evt.Skip()
                    return
            except Exception:
                try:
                    if evt is not None:
                        evt.Skip()
                except Exception:
                    pass
                return
            self._closed = True
            try:
                if getattr(self, "_preview_debouncer", None):
                    self._preview_debouncer.cancel()
            except Exception:
                pass
            try:
                if evt is not None:
                    evt.Skip()
            except Exception:
                pass

        try:
            self.Bind(wx.EVT_WINDOW_DESTROY, _on_destroy)
        except Exception:
            pass

        # Splitter: user-resizable preview pane width.
        splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)

        # Make the "fields" column scrollable (add/edit windows can be short).
        left_scroll = wx.ScrolledWindow(splitter, style=wx.VSCROLL)
        try:
            left_scroll.SetScrollRate(0, 12)
        except Exception:
            pass
        left = wx.Panel(left_scroll)
        grid = wx.FlexGridSizer(cols=2, vgap=8, hgap=10)
        grid.AddGrowableCol(1, 1)
        for row_i, h in enumerate(headers):
            grid.Add(wx.StaticText(left, label=h), 0, wx.ALIGN_CENTER_VERTICAL)
            ctrl = self._build_control_for_header(left, h, str(row.get(h, "") or ""))
            self._ctrls[h] = ctrl
            grid.Add(ctrl, 1, wx.EXPAND)
            # Footprint field uses an internal scrolled window with fixed height (~3 rows).
            # Keep it a fixed height even when the dialog is resized vertically.
        left_s = wx.BoxSizer(wx.VERTICAL)
        left_s.Add(grid, 1, wx.ALL | wx.EXPAND, 8)
        left.SetSizer(left_s)
        try:
            left_scroll_s = wx.BoxSizer(wx.VERTICAL)
            left_scroll_s.Add(left, 1, wx.EXPAND)
            left_scroll.SetSizer(left_scroll_s)
            left_scroll.FitInside()
        except Exception:
            pass

        # Make the preview pane scrollable (it contains two large preview boxes).
        right_scroll = wx.ScrolledWindow(splitter, style=wx.VSCROLL)
        try:
            right_scroll.SetScrollRate(0, 12)
        except Exception:
            pass
        right = wx.Panel(right_scroll)
        right_s = wx.BoxSizer(wx.VERTICAL)
        fp_box = wx.StaticBoxSizer(wx.VERTICAL, right, "Footprint preview")
        self.fp_prev = PreviewPanel(right, empty_label="(preview updates as you edit)", show_choice=True, min_bitmap_size=(-1, 280))
        fp_box.Add(self.fp_prev, 1, wx.EXPAND)
        right_s.Add(fp_box, 1, wx.ALL | wx.EXPAND, 6)
        sym_box = wx.StaticBoxSizer(wx.VERTICAL, right, "Symbol preview")
        self.sym_prev = PreviewPanel(
            right,
            empty_label="(preview updates as you edit)",
            show_choice=False,
            crop_to_alpha=True,
            min_bitmap_size=(-1, 280),
        )
        sym_box.Add(self.sym_prev, 1, wx.EXPAND)
        right_s.Add(sym_box, 1, wx.ALL | wx.EXPAND, 6)
        right.SetSizer(right_s)
        try:
            right_scroll_s = wx.BoxSizer(wx.VERTICAL)
            right_scroll_s.Add(right, 1, wx.EXPAND)
            right_scroll.SetSizer(right_scroll_s)
            right_scroll.FitInside()
        except Exception:
            pass

        try:
            splitter.SetMinimumPaneSize(420)
        except Exception:
            pass
        try:
            splitter.SplitVertically(left_scroll, right_scroll, sashPosition=820)
        except Exception:
            try:
                splitter.SplitVertically(left_scroll, right_scroll)
            except Exception:
                pass
        try:
            splitter.SetSashGravity(0.75)
        except Exception:
            pass

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(splitter, 1, wx.EXPAND)
        self.SetSizer(outer)

        self.fp_prev.choice.Bind(wx.EVT_CHOICE, lambda _e: self._queue_preview())
        # Defer first preview render until after initial layout/splitter sizing.
        try:
            wx.CallAfter(self._queue_preview)
        except Exception:
            self._queue_preview()

    def Destroy(self) -> bool:  # type: ignore[override]
        # Stop timer before C++ deletion is scheduled.
        try:
            self._closed = True
        except Exception:
            pass
        try:
            if getattr(self, "_preview_debouncer", None):
                self._preview_debouncer.cancel()
        except Exception:
            pass
        return super().Destroy()

    def _build_control_for_header(self, parent: wx.Window, header: str, value: str) -> wx.Control:
        if header == "IPN":
            # IPN is assigned automatically; show it but don't allow editing.
            ctrl = wx.TextCtrl(parent, value=value, style=wx.TE_READONLY)
            try:
                if not (value or "").strip():
                    ctrl.SetHint("(assigned automatically)")
            except Exception:
                pass
            try:
                ctrl.Enable(False)
            except Exception:
                pass
            return ctrl
        if header == "Symbol":
            return self._build_symbol_picker_row(parent, value)
        if header == "Footprint":
            return self._build_footprint_multi_row(parent, value)
        ctrl = wx.TextCtrl(parent, value=value)
        ctrl.Bind(wx.EVT_TEXT, lambda _e: self._queue_preview())
        return ctrl

    def _build_symbol_picker_row(self, parent: wx.Window, value: str) -> wx.Control:
        panel = wx.Panel(parent)
        s = wx.BoxSizer(wx.HORIZONTAL)
        # Browse-first UX: no dropdown (avoids huge suggestion lists).
        txt = wx.TextCtrl(panel, value=value)
        s.Add(txt, 1, wx.EXPAND)
        pick_btn = wx.Button(panel, label="Browse")
        s.Add(pick_btn, 0, wx.LEFT, 6)

        def on_pick(_evt: wx.CommandEvent) -> None:
            repo_path = self._repo_path
            if repo_path:
                try:
                    from .symbols.browser_dialog import SymbolBrowserDialog

                    dlg = SymbolBrowserDialog(self, repo_path, picker_mode=True)
                    try:
                        if dlg.ShowModal() != wx.ID_OK:
                            return
                        selected = dlg.get_picked_ref()
                    finally:
                        dlg.Destroy()
                except Exception:
                    selected = ""
            else:
                dlg = SearchPickerDialog(panel, "Select symbol", self._symbols, initial=txt.GetValue())
                try:
                    if dlg.ShowModal() != wx.ID_OK:
                        return
                    selected = dlg.get_selected()
                finally:
                    dlg.Destroy()
            if selected:
                txt.SetValue(selected)
                self._queue_preview()

        pick_btn.Bind(wx.EVT_BUTTON, on_pick)
        txt.Bind(wx.EVT_TEXT, lambda _e: self._queue_preview())
        panel.SetSizer(s)
        return panel

    def _build_footprint_multi_row(self, parent: wx.Window, value: str) -> wx.Control:
        return MultiFootprintField(
            parent,
            self._repo_path,
            self._footprints,
            value=value,
            on_create_footprint=self._create_footprint_cb,
            on_changed=self._queue_preview,
        )

    def get_row(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for h in self._headers:
            out[h] = self._read_control(self._ctrls.get(h)).strip()
        return out

    def set_row_values(self, row: dict[str, str]) -> None:
        """
        Update controls in-place from a row dict.
        """
        row = row or {}
        for h in self._headers:
            val = str(row.get(h, "") or "")
            ctrl = self._ctrls.get(h)
            if isinstance(ctrl, MultiFootprintField):
                ctrl.set_value(val)
                continue
            combo = self._extract_combo(ctrl)
            if combo:
                try:
                    combo.SetValue(val)
                except Exception:
                    pass
                continue
            if isinstance(ctrl, wx.TextCtrl):
                try:
                    ctrl.SetValue(val)
                except Exception:
                    pass
                continue
            txt = self._extract_text(ctrl)
            if txt:
                # e.g. "Symbol" is a panel containing a TextCtrl (browse-first UX).
                try:
                    txt.SetValue(val)
                except Exception:
                    pass
        self._queue_preview()

    def set_footprint_choices(self, footprints: list[str]) -> None:
        self._footprints = sorted(set(footprints or []))
        ctrl = self._ctrls.get("Footprint")
        if isinstance(ctrl, MultiFootprintField):
            ctrl.set_footprint_choices(self._footprints)

    def set_symbol_choices(self, symbols: list[str]) -> None:
        self._symbols = sorted(set(symbols or []))
        # Symbol field is a TextCtrl (browse-first); keep list only for fallback picker.
        return

    def _read_control(self, ctrl: wx.Control | None) -> str:
        if isinstance(ctrl, MultiFootprintField):
            return ctrl.get_value()
        combo = self._extract_combo(ctrl)
        if combo:
            return str(combo.GetValue() or "")
        txt = self._extract_text(ctrl)
        if txt:
            return str(txt.GetValue() or "")
        if isinstance(ctrl, wx.TextCtrl):
            return str(ctrl.GetValue() or "")
        return ""

    def _extract_combo(self, ctrl: wx.Control | None) -> wx.ComboBox | None:
        if isinstance(ctrl, wx.ComboBox):
            return ctrl
        if isinstance(ctrl, wx.Panel):
            for child in ctrl.GetChildren():
                if isinstance(child, wx.ComboBox):
                    return child
        return None

    def _extract_text(self, ctrl: wx.Control | None) -> wx.TextCtrl | None:
        if isinstance(ctrl, wx.TextCtrl):
            return ctrl
        if isinstance(ctrl, wx.Panel):
            for child in ctrl.GetChildren():
                if isinstance(child, wx.TextCtrl):
                    return child
        return None

    def _queue_preview(self) -> None:
        if bool(getattr(self, "_closed", False)):
            return
        self._preview_pending = True
        try:
            if getattr(self, "_preview_debouncer", None):
                self._preview_debouncer.trigger(delay_ms=250)
        except Exception:
            self._on_preview_timer(None)

    def _on_preview_timer(self, _evt=None) -> None:
        if bool(getattr(self, "_closed", False)):
            return
        if not self._preview_pending:
            return
        self._preview_pending = False
        self._update_previews()

    def _update_previews(self) -> None:
        if bool(getattr(self, "_closed", False)):
            return
        row = self.get_row() or {}
        sym_ref = str(row.get("Symbol", "") or "").strip()
        fps = _split_semicolon_list(str(row.get("Footprint", "") or ""))

        # Footprint choice list
        try:
            prev_sel = str(self.fp_prev.choice.GetStringSelection() or "").strip()
        except Exception:
            prev_sel = ""
        try:
            self.fp_prev.choice.Clear()
            for x in fps:
                self.fp_prev.choice.Append(x)
            if fps:
                self.fp_prev.choice.Enable(True)
                if prev_sel and prev_sel in fps:
                    self.fp_prev.choice.SetStringSelection(prev_sel)
                else:
                    self.fp_prev.choice.SetSelection(0)
            else:
                self.fp_prev.choice.Enable(False)
        except Exception:
            pass
        self.fp_prev.set_choice_visible(len(fps) > 1)

        fp_ref = ""
        try:
            if self.fp_prev.choice.IsEnabled() and self.fp_prev.choice.GetSelection() != wx.NOT_FOUND:
                fp_ref = str(self.fp_prev.choice.GetStringSelection() or "").strip()
        except Exception:
            fp_ref = ""
        if not fp_ref and fps:
            fp_ref = fps[0]

        repo_path = str(getattr(self, "_repo_path", "") or "")
        if fp_ref:
            from .footprints.ops import find_footprint_mod_any, render_footprint_svg

            mtime = "0"
            try:
                if ":" in fp_ref:
                    lib, fpname = fp_ref.split(":", 1)
                    mod = find_footprint_mod_any(repo_path, lib, fpname)
                    mtime = str(os.path.getmtime(mod)) if mod and os.path.exists(mod) else "0"
            except Exception:
                mtime = "0"
            self.fp_prev.render_cached_svg_async(
                kind_dir="fp",
                cache_key_prefix="fp_edit_component",
                ref=fp_ref,
                source_mtime=mtime,
                render_svg=lambda r, p: render_footprint_svg(repo_path, r, p),
                quality_scale=2.5,
            )
        else:
            self.fp_prev.set_empty()

        if sym_ref:
            from .symbols.libcache import resolve_symbol_lib_path
            from .symbols.ops import render_symbol_svg

            mtime = "0"
            try:
                if ":" in sym_ref:
                    lib = sym_ref.split(":", 1)[0].strip()
                    lib_path = resolve_symbol_lib_path(repo_path, lib) or ""
                    mtime = str(os.path.getmtime(lib_path)) if lib_path and os.path.exists(lib_path) else "0"
            except Exception:
                mtime = "0"
            self.sym_prev.render_cached_svg_async(
                kind_dir="sym",
                cache_key_prefix="sym_edit_component",
                ref=sym_ref,
                source_mtime=mtime,
                render_svg=lambda r, p: render_symbol_svg(repo_path, r, p),
                quality_scale=2.5,
            )
        else:
            self.sym_prev.set_empty()


class TemplatePickerDialog(wx.Dialog):
    """
    Pick an existing component row (for "Copy from existing…"), ui.py-like.
    """

    def __init__(self, parent: wx.Window, *, repo_path: str, headers: list[str], rows: list[dict[str, str]], title: str):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX)
        self._repo_path = repo_path
        self._headers = list(headers or [])
        self._rows = list(rows or [])
        self._visible_idx: list[int] = []
        self._selected: dict[str, str] | None = None

        vbox = wx.BoxSizer(wx.VERTICAL)
        vbox.Add(wx.StaticText(self, label="Select a part to copy fields from:"), 0, wx.ALL, 8)

        self.filter = wx.TextCtrl(self)
        self.filter.SetHint("Filter (IPN / MPN / Manufacturer / Value / Description)")
        self.filter.Bind(wx.EVT_TEXT, self._on_filter)
        vbox.Add(self.filter, 0, wx.ALL | wx.EXPAND, 8)

        splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)
        left = wx.Panel(splitter)
        right = wx.Panel(splitter)

        left_s = wx.BoxSizer(wx.VERTICAL)
        # wx.ListCtrl manages horizontal scrolling in report mode; avoid wx.HSCROLL to
        # prevent header/content scroll desync on some platforms.
        self.list = wx.ListCtrl(left, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for i, col in enumerate(["IPN", "MPN", "Manufacturer", "Value", "Description"]):
            self.list.InsertColumn(i, col)
        left_s.Add(self.list, 1, wx.ALL | wx.EXPAND, 0)
        left.SetSizer(left_s)

        right_s = wx.BoxSizer(wx.VERTICAL)
        fp_box = wx.StaticBoxSizer(wx.VERTICAL, right, "Footprint preview")
        self.fp_prev = PreviewPanel(right, empty_label="(select a row)", show_choice=True, min_bitmap_size=(-1, 260))
        fp_box.Add(self.fp_prev, 1, wx.EXPAND)
        right_s.Add(fp_box, 1, wx.ALL | wx.EXPAND, 6)

        sym_box = wx.StaticBoxSizer(wx.VERTICAL, right, "Symbol preview")
        self.sym_prev = PreviewPanel(right, empty_label="(select a row)", show_choice=False, crop_to_alpha=True, min_bitmap_size=(-1, 260))
        sym_box.Add(self.sym_prev, 1, wx.EXPAND)
        right_s.Add(sym_box, 1, wx.ALL | wx.EXPAND, 6)

        right.SetSizer(right_s)
        splitter.SplitVertically(left, right, sashPosition=780)
        splitter.SetMinimumPaneSize(350)
        # Avoid negative sash positions (can assert on some wx ports, notably macOS).
        def _set_default_sash() -> None:
            try:
                w = int(splitter.GetClientSize().GetWidth() or 0)
            except Exception:
                w = 0
            right_w = 350
            try:
                pos = max(350, w - right_w) if w else 780
                splitter.SetSashPosition(pos)
            except Exception:
                pass

        wx.CallAfter(_set_default_sash)
        vbox.Add(splitter, 1, wx.ALL | wx.EXPAND, 8)

        btns = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        vbox.Add(btns, 0, wx.ALL | wx.EXPAND, 8)

        self.SetSizerAndFit(vbox)
        self.SetMinSize((1200, 750))
        self.SetSize((1400, 900))

        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_select)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_activate)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        self.fp_prev.choice.Bind(wx.EVT_CHOICE, lambda _e: self._render_previews())

        self._render_list()

    def get_selected_row(self) -> dict[str, str] | None:
        return dict(self._selected or {}) if self._selected else None

    def _on_filter(self, _evt: wx.CommandEvent) -> None:
        self._render_list()

    def _row_text(self, r: dict[str, str]) -> str:
        parts = [r.get("IPN", ""), r.get("MPN", ""), r.get("Manufacturer", ""), r.get("Value", ""), r.get("Description", "")]
        return " ".join([str(x or "") for x in parts]).lower()

    def _render_list(self) -> None:
        q = (self.filter.GetValue() or "").strip().lower()
        self.list.DeleteAllItems()
        self._visible_idx = []
        for i, r in enumerate(self._rows):
            if q and q not in self._row_text(r):
                continue
            idx = self.list.InsertItem(self.list.GetItemCount(), str(r.get("IPN", "") or ""))
            self.list.SetItem(idx, 1, str(r.get("MPN", "") or ""))
            self.list.SetItem(idx, 2, str(r.get("Manufacturer", "") or ""))
            self.list.SetItem(idx, 3, str(r.get("Value", "") or ""))
            self.list.SetItem(idx, 4, str(r.get("Description", "") or ""))
            self._visible_idx.append(i)
        for c in range(5):
            try:
                self.list.SetColumnWidth(c, wx.LIST_AUTOSIZE_USEHEADER)
            except Exception:
                pass
        if self.list.GetItemCount() > 0:
            try:
                self.list.Select(0)
                self.list.Focus(0)
            except Exception:
                pass

    def _on_select(self, _evt: wx.ListEvent) -> None:
        sel = self.list.GetFirstSelected()
        if sel < 0 or sel >= len(self._visible_idx):
            self._selected = None
            return
        self._selected = dict(self._rows[self._visible_idx[sel]] or {})
        self._render_previews()

    def _on_activate(self, _evt: wx.ListEvent) -> None:
        self._on_ok(wx.CommandEvent())

    def _on_ok(self, _evt: wx.CommandEvent) -> None:
        self._capture_selected()
        self.EndModal(wx.ID_OK)

    def _capture_selected(self) -> None:
        sel = self.list.GetFirstSelected()
        if sel < 0 or sel >= len(self._visible_idx):
            self._selected = None
            return
        self._selected = dict(self._rows[self._visible_idx[sel]] or {})

    def _render_previews(self) -> None:
        row = self._selected or {}
        sym_ref = str(row.get("Symbol", "") or "").strip()
        fps = _split_semicolon_list(str(row.get("Footprint", "") or ""))

        # Populate footprint choice
        try:
            prev_sel = str(self.fp_prev.choice.GetStringSelection() or "").strip()
        except Exception:
            prev_sel = ""
        try:
            self.fp_prev.choice.Clear()
            for x in fps:
                self.fp_prev.choice.Append(x)
            if fps:
                self.fp_prev.choice.Enable(True)
                if prev_sel and prev_sel in fps:
                    self.fp_prev.choice.SetStringSelection(prev_sel)
                else:
                    self.fp_prev.choice.SetSelection(0)
            else:
                self.fp_prev.choice.Enable(False)
        except Exception:
            pass
        self.fp_prev.set_choice_visible(len(fps) > 1)

        fp_ref = ""
        try:
            if self.fp_prev.choice.IsEnabled() and self.fp_prev.choice.GetSelection() != wx.NOT_FOUND:
                fp_ref = str(self.fp_prev.choice.GetStringSelection() or "").strip()
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
            self.fp_prev.render_cached_svg_async(
                kind_dir="fp",
                cache_key_prefix="fp_template_picker",
                ref=fp_ref,
                source_mtime=mtime,
                render_svg=lambda r, p: render_footprint_svg(self._repo_path, r, p),
                quality_scale=2.5,
            )
        else:
            self.fp_prev.set_empty()

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
            self.sym_prev.render_cached_svg_async(
                kind_dir="sym",
                cache_key_prefix="sym_template_picker",
                ref=sym_ref,
                source_mtime=mtime,
                render_svg=lambda r, p: render_symbol_svg(self._repo_path, r, p),
                quality_scale=2.5,
            )
        else:
            self.sym_prev.set_empty()
