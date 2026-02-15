from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import tempfile
import threading
import traceback
from typing import Any, Dict, Tuple

import wx

try:
    # Used for preview rendering (kicad-cli lookup is tricky on macOS when PATH is limited).
    from library_manager.ui.kicad_env import resolve_kicad_cli  # type: ignore
except Exception:
    resolve_kicad_cli = None  # type: ignore

try:
    from library_manager.ui.window_title import with_library_suffix  # type: ignore
except Exception:
    with_library_suffix = None  # type: ignore

try:
    from library_manager.ui.async_ui import UiDebouncer  # type: ignore
except Exception:
    UiDebouncer = None  # type: ignore

try:
    from library_manager._subprocess import SUBPROCESS_NO_WINDOW  # type: ignore
except Exception:
    import sys as _sys
    SUBPROCESS_NO_WINDOW: dict = {}
    if _sys.platform == "win32":
        SUBPROCESS_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW}

try:
    # Reuse the preview widget + caching pipeline from the parts manager UI.
    from library_manager.ui.preview_panel import PreviewPanel  # type: ignore
except Exception:
    PreviewPanel = None  # type: ignore

from .generate import build_pattern, generate_footprint
from .form_model import KINDS, compute_auto_name, element_from_fields, schema_for_kind


def _run_in_bg(work, done) -> None:
    """
    Tiny background runner: executes work() in a thread, then calls done(res, err) on UI thread.
    """

    def _runner():
        res = None
        err = None
        try:
            res = work()
        except Exception as e:  # noqa: BLE001
            err = e
        try:
            wx.CallAfter(done, res, err)
        except Exception:
            pass

    threading.Thread(target=_runner, daemon=True).start()


_STATE_VERSION = 2
_STATE_MEM: dict[str, Any] | None = None


def _state_path() -> str:
    """
    Location for persisted generator state (cross-platform).
    """
    base = ""
    try:
        base = wx.StandardPaths.Get().GetUserConfigDir()
    except Exception:
        base = os.path.expanduser("~")
    return os.path.join(base, "kicad_library_manager", "footprint_generator_state.json")


def _load_state_best_effort() -> dict[str, Any]:
    global _STATE_MEM
    if _STATE_MEM is not None:
        return _STATE_MEM
    p = _state_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    prev_ver = int(d.get("version") or 0) if isinstance(d.get("version"), (int, float, str)) else 0
    # Basic shape
    if not isinstance(d, dict):
        d = {}
    d.setdefault("global", {})
    d.setdefault("kinds", {})

    # Migration: v1 saved implied `.nom` values for min/nom/max groups.
    # We now keep nominal boxes empty by default (using mean(min,max) as a hint),
    # and only persist `.nom` when user explicitly overrides.
    if prev_ver < 2:
        try:
            kinds = d.get("kinds", {})
            if isinstance(kinds, dict):
                for _k, ks in list(kinds.items()):
                    if not isinstance(ks, dict):
                        continue
                    fields = ks.get("fields", {})
                    if not isinstance(fields, dict):
                        continue
                    # Build base -> (min,max,nom) if present.
                    bases: dict[str, dict[str, float]] = {}
                    for pth, v in list(fields.items()):
                        if not isinstance(pth, str):
                            continue
                        if not pth.endswith((".min", ".max", ".nom")):
                            continue
                        try:
                            base, suf = pth.rsplit(".", 1)
                        except Exception:
                            continue
                        suf = suf.strip().lower()
                        if suf not in ("min", "max", "nom"):
                            continue
                        try:
                            fv = float(v)
                        except Exception:
                            continue
                        bases.setdefault(base, {})[suf] = fv
                    # Drop nom when it's just the mean(min,max).
                    for base, m in bases.items():
                        if not all(x in m for x in ("min", "max", "nom")):
                            continue
                        mean = (float(m["min"]) + float(m["max"])) / 2.0
                        if abs(float(m["nom"]) - mean) <= 1e-9:
                            fields.pop(base + ".nom", None)
        except Exception:
            pass

    d["version"] = _STATE_VERSION
    d.setdefault("global", {})
    d.setdefault("kinds", {})
    _STATE_MEM = d
    # Save migrated state (best-effort).
    try:
        _save_state_best_effort(d)
    except Exception:
        pass
    return d


def _save_state_best_effort(d: dict[str, Any]) -> None:
    global _STATE_MEM
    _STATE_MEM = d
    try:
        p = _state_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, sort_keys=True)
        os.replace(tmp, p)
    except Exception:
        # Never break UI for persistence
        return


def _wipe_state_file_best_effort() -> None:
    """
    Delete persisted state file (if present) and clear in-memory cache.
    """
    global _STATE_MEM
    _STATE_MEM = None
    try:
        os.remove(_state_path())
    except Exception:
        # ignore missing / permission errors
        return


def _list_pretty_dirs(repo_path: str) -> Tuple[str, list[str]]:
    """
    Return (footprints_root, pretty_dirs) for this repo.
    """
    root = os.path.join(repo_path, "Footprints")
    pretty: list[str] = []
    try:
        if os.path.isdir(root):
            for name in sorted(os.listdir(root)):
                if not name.endswith(".pretty"):
                    continue
                p = os.path.join(root, name)
                if os.path.isdir(p):
                    pretty.append(p)
    except Exception:
        pass
    return root, pretty


class FootprintGeneratorDialog(wx.Dialog):
    """
    wxPython-integrated footprint generator dialog for KiCad.
    Writes generated `.kicad_mod` files into a chosen `.pretty` directory.
    """

    def __init__(self, parent: wx.Window, repo_path: str):
        # We create this as a dialog parented to the main plugin window so it behaves like
        # other "subwindows" (Settings / Manage categories) and stays tied to the KiCad window.
        self._owner = parent
        super().__init__(
            parent,
            title=(with_library_suffix("Create footprint", repo_path) if with_library_suffix else "Create footprint"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX | wx.CLIP_CHILDREN,
        )
        self._repo_path = repo_path

        # IMPORTANT: avoid wx.Timer (native crash risk if handler is freed).
        self._hints_debouncer = UiDebouncer(self, delay_ms=200, callback=lambda: self._on_debounce_timer(None)) if UiDebouncer else None
        self._hint_job_id = 0

        # Preview update: do not update while user types; only on "commit" events (Enter / focus out).
        self._preview_debouncer = UiDebouncer(self, delay_ms=250, callback=lambda: self._on_preview_timer(None)) if UiDebouncer else None
        self._preview_pending = False
        self._preview_job_id = 0
        self._preview_last_key = ""
        self._closing = False

        # Temp .pretty folder for preview generation (never committed).
        self._preview_pretty_dir = None

        # Persisted state
        self._state = _load_state_best_effort()
        self._restoring = False

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(wx.StaticText(self, label="Create a new footprint using IPC-7351-style generators."), 0, wx.ALL, 8)

        splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)
        self._splitter = splitter
        left = wx.Panel(splitter)
        right = wx.Panel(splitter)
        self._left_panel = left

        left_s = wx.BoxSizer(wx.VERTICAL)
        self._left_sizer = left_s

        top = wx.FlexGridSizer(cols=2, vgap=8, hgap=10)
        top.AddGrowableCol(1, 1)

        # Pattern
        top.Add(wx.StaticText(left, label="Pattern"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.kind = wx.Choice(left, choices=KINDS)
        try:
            self.kind.SetStringSelection("soic")
        except Exception:
            if KINDS:
                self.kind.SetSelection(0)
        self.kind.Bind(wx.EVT_CHOICE, self._on_kind_change)
        top.Add(self.kind, 1, wx.EXPAND)

        # Density
        top.Add(wx.StaticText(left, label="Density"), 0, wx.ALIGN_CENTER_VERTICAL)
        dens_row = wx.BoxSizer(wx.HORIZONTAL)
        self.den_all = wx.CheckBox(left, label="All densities")
        self.den_L = wx.CheckBox(left, label="Least")
        self.den_N = wx.CheckBox(left, label="Nominal")
        self.den_M = wx.CheckBox(left, label="Most")
        self.den_N.SetValue(True)
        dens_row.Add(self.den_all, 0, wx.RIGHT, 10)
        dens_row.Add(self.den_L, 0, wx.RIGHT, 10)
        dens_row.Add(self.den_N, 0, wx.RIGHT, 10)
        dens_row.Add(self.den_M, 0, wx.RIGHT, 10)
        top.Add(dens_row, 0, wx.EXPAND)

        self.den_all.Bind(wx.EVT_CHECKBOX, self._on_toggle_all_densities)
        self.den_L.Bind(wx.EVT_CHECKBOX, self._on_density_toggle)
        self.den_N.Bind(wx.EVT_CHECKBOX, self._on_density_toggle)
        self.den_M.Bind(wx.EVT_CHECKBOX, self._on_density_toggle)

        # Library (.pretty)
        top.Add(wx.StaticText(left, label="Library"), 0, wx.ALIGN_CENTER_VERTICAL)
        out_row = wx.BoxSizer(wx.HORIZONTAL)
        self._fp_root, pretty_dirs = _list_pretty_dirs(repo_path)
        pretty_labels = [os.path.basename(p) for p in pretty_dirs]
        self.out_choice = wx.Choice(left, choices=pretty_labels)
        self._pretty_dirs = pretty_dirs
        if pretty_dirs:
            self.out_choice.SetSelection(0)
        self.out_choice.Bind(wx.EVT_CHOICE, lambda _e: self._schedule_preview_update())
        out_row.Add(self.out_choice, 1, wx.EXPAND)
        browse = wx.Button(left, label="Browse")
        browse.Bind(wx.EVT_BUTTON, self._on_browse_out)
        out_row.Add(browse, 0, wx.LEFT, 8)
        top.Add(out_row, 1, wx.EXPAND)

        left_s.Add(top, 0, wx.ALL | wx.EXPAND, 8)

        # Per-density name/description overrides (not persisted).
        nd_box = wx.StaticBoxSizer(wx.VERTICAL, left, "Per-density name/description (optional overrides)")
        nd_box.Add(
            wx.StaticText(
                left,
                label="Leave empty to use auto-generated values (shown as hints).",
            ),
            0,
            wx.ALL | wx.EXPAND,
            6,
        )
        self._nd_grid = wx.FlexGridSizer(cols=3, vgap=6, hgap=10)
        self._nd_grid.AddGrowableCol(1, 1)
        self._nd_grid.AddGrowableCol(2, 1)
        self._nd_grid.Add(wx.StaticText(left, label="Density"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._nd_grid.Add(wx.StaticText(left, label="Name"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._nd_grid.Add(wx.StaticText(left, label="Description"), 0, wx.ALIGN_CENTER_VERTICAL)
        nd_box.Add(self._nd_grid, 0, wx.ALL | wx.EXPAND, 6)
        left_s.Add(nd_box, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        # Dynamic fields panel (scrollable)
        self._fields_scroll = wx.ScrolledWindow(left, style=wx.VSCROLL)
        self._fields_scroll.SetScrollRate(0, 10)
        self._fields_sizer = wx.FlexGridSizer(cols=2, vgap=6, hgap=10)
        self._fields_sizer.AddGrowableCol(1, 1)
        self._fields_scroll.SetSizer(self._fields_sizer)
        left_s.Add(self._fields_scroll, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        self.status = wx.StaticText(left, label="")
        left_s.Add(self.status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        left.SetSizer(left_s)

        # Preview pane (right)
        right_s = wx.StaticBoxSizer(wx.VERTICAL, right, "Preview")
        dens_pick_row = wx.BoxSizer(wx.HORIZONTAL)
        dens_pick_row.Add(wx.StaticText(right, label="Preview density"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        if PreviewPanel:
            self._preview_panel = PreviewPanel(
                right,
                empty_label="(preview updates on Enter or when leaving a field)",
                show_choice=False,
                choice_parent=right,
                min_bitmap_size=(520, 360),
            )
            self.prev_density_choice = self._preview_panel.choice
            self.prev_status = self._preview_panel.status
            self.prev_bmp = self._preview_panel.bmp
            try:
                self.prev_density_choice.Show(True)
                self.prev_density_choice.Enable(True)
            except Exception:
                pass
        else:
            self._preview_panel = None
            self.prev_density_choice = wx.Choice(right, choices=[])
            self.prev_status = wx.StaticText(right, label="(preview updates on Enter or when leaving a field)")
            self.prev_bmp = wx.StaticBitmap(right, size=(-1, -1))

        self.prev_density_choice.Bind(wx.EVT_CHOICE, lambda _e: self._schedule_preview_update())
        dens_pick_row.Add(self.prev_density_choice, 1, wx.EXPAND)
        right_s.Add(dens_pick_row, 0, wx.ALL | wx.EXPAND, 6)
        try:
            self.prev_bmp.SetMinSize((520, 360))
        except Exception:
            pass
        self.prev_bmp.Bind(wx.EVT_SIZE, self._on_preview_size)
        if self._preview_panel:
            right_s.Add(self._preview_panel, 1, wx.ALL | wx.EXPAND, 6)
        else:
            right_s.Add(self.prev_status, 0, wx.ALL | wx.EXPAND, 6)
            right_s.Add(self.prev_bmp, 1, wx.ALL | wx.EXPAND, 6)
        right.SetSizer(right_s)

        # Default: preview pane uses ~1/3 width.
        splitter.SplitVertically(left, right, sashPosition=980)
        splitter.SetMinimumPaneSize(320)
        outer.Add(splitter, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        btns.AddStretchSpacer(1)
        self.gen_btn = wx.Button(self, label="Generate")
        self.gen_btn.Bind(wx.EVT_BUTTON, self._on_generate)
        btns.Add(self.gen_btn, 0, wx.ALL, 6)
        close = wx.Button(self, label="Close")
        close.Bind(wx.EVT_BUTTON, self._on_close_button)
        btns.Add(close, 0, wx.ALL, 6)
        outer.Add(btns, 0, wx.EXPAND)

        self.SetSizer(outer)
        self.SetMinSize((1200, 750))
        self.SetSize((1800, 900))
        self.Bind(wx.EVT_CLOSE, self._on_close)
        try:
            self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        except Exception:
            pass
        # ESC should close this dialog (common expectation, especially on macOS).
        try:
            self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        except Exception:
            pass

        # After initial layout, set splitter to 2/3 (left) vs 1/3 (right).
        try:
            wx.CallAfter(self._set_default_splitter_ratio)
        except Exception:
            pass

        self._field_ctrls: Dict[str, wx.Control] = {}
        self._nd_name: dict[str, wx.TextCtrl] = {}
        self._nd_desc: dict[str, wx.TextCtrl] = {}
        self._restore_global_state_best_effort()
        self._build_fields()
        self._rebuild_name_desc_overrides()
        self._update_preview_density_choices()
        self._restore_kind_state_best_effort(self.kind.GetStringSelection() or "soic")
        # Track which kind the current UI controls belong to (important when switching Kind).
        self._active_kind = self.kind.GetStringSelection() or "soic"
        self._schedule_hints_update()
        self._schedule_preview_update()

    def _on_char_hook(self, evt: wx.KeyEvent) -> None:
        try:
            code = int(evt.GetKeyCode())
        except Exception:
            code = -1
        if code == wx.WXK_ESCAPE:
            try:
                if self.IsModal():
                    self.EndModal(wx.ID_CANCEL)
                else:
                    self.Close()
                return
            except Exception:
                pass
        try:
            evt.Skip()
        except Exception:
            pass

    def _stop_timers_best_effort(self) -> None:
        try:
            if getattr(self, "_hints_debouncer", None):
                self._hints_debouncer.cancel()
        except Exception:
            pass
        try:
            if getattr(self, "_preview_debouncer", None):
                self._preview_debouncer.cancel()
        except Exception:
            pass

    def _on_destroy(self, evt) -> None:
        # EVT_WINDOW_DESTROY can be observed when child windows are destroyed; only treat this
        # as shutdown if the frame itself is being destroyed.
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
                evt.Skip()
            except Exception:
                pass
            return
        try:
            self._closing = True
        except Exception:
            pass
        try:
            self._stop_timers_best_effort()
        except Exception:
            pass
        try:
            evt.Skip()
        except Exception:
            pass

    def Destroy(self) -> bool:  # type: ignore[override]
        # Ensure timers are stopped before C++ deletion is scheduled.
        try:
            self._closing = True
        except Exception:
            pass
        try:
            self._stop_timers_best_effort()
        except Exception:
            pass
        return super().Destroy()

    def _set_default_splitter_ratio(self) -> None:
        try:
            sp = getattr(self, "_splitter", None)
            if not sp:
                return
            w = int(self.GetClientSize().GetWidth() or 0)
            if w <= 0:
                w = int(self.GetSize().GetWidth() or 0)
            if w <= 0:
                return
            # left takes 2/3, preview right takes 1/3
            sp.SetSashPosition(max(320, int((w * 2) / 3)))
        except Exception:
            return

    # --------------------------
    # UI helpers
    # --------------------------

    def _on_browse_out(self, _evt: wx.CommandEvent) -> None:
        dlg = wx.DirDialog(self, "Select output .pretty directory", defaultPath=self._fp_root or "")
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
            if not path:
                return
            # If user selects the Footprints root, allow it but warn later.
            # Add to choice list as a custom entry.
            label = os.path.basename(path.rstrip(os.sep)) or path
            if label not in [self.out_choice.GetString(i) for i in range(self.out_choice.GetCount())]:
                self.out_choice.Append(label)
                self._pretty_dirs.append(path)
            self.out_choice.SetStringSelection(label)
            self._schedule_preview_update()
        finally:
            dlg.Destroy()

    def _on_kind_change(self, _evt: wx.CommandEvent) -> None:
        # Save the previous kind state before switching.
        try:
            if not self._restoring:
                try:
                    if getattr(self, "_hints_debouncer", None):
                        self._hints_debouncer.cancel()
                except Exception:
                    pass
                try:
                    if getattr(self, "_preview_debouncer", None):
                        self._preview_debouncer.cancel()
                except Exception:
                    pass
                # IMPORTANT: EVT_CHOICE fires *after* selection changed, but our field controls
                # still contain the previous kind's values. Persist using the tracked active kind.
                old_kind = str(getattr(self, "_active_kind", "") or "")
                if old_kind:
                    self._persist_kind_state_best_effort(old_kind, update_global=False)
        except Exception:
            pass
        self._build_fields()
        self._reset_kind_ui_to_defaults_best_effort()
        self._rebuild_name_desc_overrides()
        self._update_preview_density_choices()
        new_kind = self.kind.GetStringSelection() or "soic"
        self._restore_kind_state_best_effort(new_kind)
        self._active_kind = new_kind
        self._schedule_hints_update()
        self._schedule_preview_update()

    def _schedule_hints_update(self) -> None:
        try:
            if getattr(self, "_hints_debouncer", None):
                self._hints_debouncer.trigger(delay_ms=200)
        except Exception:
            pass

    def _on_debounce_timer(self, _evt=None) -> None:
        self._update_hints_best_effort()

    def _on_close(self, evt: wx.CloseEvent) -> None:
        try:
            self._closing = True
        except Exception:
            pass
        try:
            self._stop_timers_best_effort()
        except Exception:
            pass
        try:
            k = str(getattr(self, "_active_kind", "") or (self.kind.GetStringSelection() or "soic"))
            self._persist_kind_state_best_effort(k, update_global=True)
        except Exception:
            pass
        # If shown modally, end the modal loop so the caller can Destroy() us.
        try:
            if self.IsModal():
                try:
                    self.EndModal(wx.ID_CANCEL)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self.Destroy()
        except Exception:
            try:
                evt.Skip()
            except Exception:
                pass

    def _clear_fields(self) -> None:
        try:
            self._fields_scroll.Freeze()
        except Exception:
            pass
        try:
            self._fields_sizer.Clear(delete_windows=True)
        except Exception:
            pass
        self._field_ctrls.clear()
        try:
            self._range_groups = {}
        except Exception:
            pass
        try:
            self._fields_scroll.Layout()
            self._fields_scroll.FitInside()
        except Exception:
            pass
        try:
            self._fields_scroll.Thaw()
        except Exception:
            pass

    def _build_fields(self) -> None:
        self._clear_fields()
        kind = self.kind.GetStringSelection() or "soic"
        specs = schema_for_kind(kind)

        # Group range-style fields: <base>.(min|nom|max) into one row with 3 slots.
        def _strip_range_suffix(lbl: str) -> str:
            s = (lbl or "").strip()
            for suf in (" min", " max", " nom", " minimum", " maximum", " nominal"):
                if s.lower().endswith(suf):
                    return s[: -len(suf)].strip()
            return s

        rng: dict[str, dict[str, tuple[str, Any, Any]]] = {}  # base -> suffix -> (label,path,default)
        by_path: dict[str, tuple[str, str, Any, Any]] = {}
        for label, path, default, choices in specs:
            by_path[str(path)] = (str(label), str(path), default, choices)
            try:
                base, suf = str(path).rsplit(".", 1)
            except Exception:
                continue
            suf = suf.lower().strip()
            if suf in ("min", "nom", "max") and isinstance(default, float) and not choices:
                rng.setdefault(base, {})[suf] = (str(label), str(path), default)

        # Only group when all three exist.
        grouped_bases = {b for b, m in rng.items() if all(k in m for k in ("min", "nom", "max"))}
        grouped_paths = set()
        for b in grouped_bases:
            grouped_paths.update({rng[b]["min"][1], rng[b]["nom"][1], rng[b]["max"][1]})

        # Track range groups for dynamic nominal hints.
        self._range_groups: dict[str, dict[str, wx.TextCtrl]] = {}
        self._range_defaults: dict[str, dict[str, float]] = {}

        # Header row for range entries.
        hdr_left = wx.StaticText(self._fields_scroll, label="")
        hdr_panel = wx.Panel(self._fields_scroll)
        hdr = wx.BoxSizer(wx.HORIZONTAL)
        for t in ("Minimum", "Nominal", "Maximum"):
            hdr_txt = wx.StaticText(hdr_panel, label=t)
            hdr.Add(hdr_txt, 1, wx.EXPAND)
        hdr_panel.SetSizer(hdr)
        self._fields_sizer.Add(hdr_left, 0, wx.ALIGN_CENTER_VERTICAL)
        self._fields_sizer.Add(hdr_panel, 1, wx.EXPAND)

        # Render specs in order, but collapse grouped range triplets into one row.
        rendered_ranges: set[str] = set()
        for label, path, default, choices in specs:
            path = str(path)
            if path in grouped_paths:
                try:
                    base, suf = path.rsplit(".", 1)
                except Exception:
                    base, suf = ("", "")
                if base in grouped_bases and base not in rendered_ranges:
                    rendered_ranges.add(base)
                    # One row: label + 3 slots (min/nom/max)
                    base_label = _strip_range_suffix(rng[base]["nom"][0] or label)
                    self._fields_sizer.Add(wx.StaticText(self._fields_scroll, label=str(base_label)), 0, wx.ALIGN_CENTER_VERTICAL)
                    row = wx.Panel(self._fields_scroll)
                    row_s = wx.BoxSizer(wx.HORIZONTAL)

                    def _mk_box(sfx: str, initial: str) -> wx.TextCtrl:
                        c = wx.TextCtrl(row, value=initial, style=wx.TE_PROCESS_ENTER)
                        c.Bind(wx.EVT_TEXT_ENTER, lambda _e: (self._schedule_hints_update(), self._schedule_preview_update()))
                        c.Bind(wx.EVT_KILL_FOCUS, self._on_commit_focus_loss)
                        row_s.Add(c, 1, wx.EXPAND | wx.RIGHT, 6 if sfx != "max" else 0)
                        return c

                    # Min/max use schema defaults; Nominal starts empty with a mean hint.
                    dmin = float(rng[base]["min"][2])
                    dmax = float(rng[base]["max"][2])
                    dnom = float(rng[base]["nom"][2])
                    self._range_defaults[base] = {"min": dmin, "max": dmax, "nom": dnom}
                    cmin = _mk_box("min", str(dmin))
                    cnom = _mk_box("nom", "")
                    cmax = _mk_box("max", str(dmax))
                    try:
                        cnom.SetHint(f"{((dmin + dmax) / 2.0):.3f}")
                    except Exception:
                        pass

                    row.SetSizer(row_s)
                    self._fields_sizer.Add(row, 1, wx.EXPAND)

                    self._field_ctrls[rng[base]["min"][1]] = cmin
                    self._field_ctrls[rng[base]["nom"][1]] = cnom
                    self._field_ctrls[rng[base]["max"][1]] = cmax
                    self._range_groups[base] = {"min": cmin, "nom": cnom, "max": cmax}
                continue

            # Non-range field (or ungrouped).
            self._fields_sizer.Add(wx.StaticText(self._fields_scroll, label=str(label)), 0, wx.ALIGN_CENTER_VERTICAL)

            ctrl: wx.Control
            if isinstance(default, bool):
                c = wx.CheckBox(self._fields_scroll)
                c.SetValue(bool(default))
                c.Bind(wx.EVT_CHECKBOX, lambda _e: (self._schedule_hints_update(), self._schedule_preview_update()))
                ctrl = c
            elif choices:
                c = wx.Choice(self._fields_scroll, choices=[str(x) for x in choices])
                try:
                    c.SetStringSelection(str(default))
                except Exception:
                    if c.GetCount() > 0:
                        c.SetSelection(0)
                c.Bind(wx.EVT_CHOICE, lambda _e: (self._schedule_hints_update(), self._schedule_preview_update()))
                ctrl = c
            elif isinstance(default, int):
                c = wx.SpinCtrl(self._fields_scroll, min=-1000000, max=1000000, initial=int(default))
                c.Bind(wx.EVT_SPINCTRL, lambda _e: (self._schedule_hints_update(), self._schedule_preview_update()))
                c.Bind(wx.EVT_KILL_FOCUS, self._on_commit_focus_loss)
                ctrl = c
            else:
                c = wx.TextCtrl(self._fields_scroll, value=str(default), style=wx.TE_PROCESS_ENTER)
                c.Bind(wx.EVT_TEXT_ENTER, lambda _e: (self._schedule_hints_update(), self._schedule_preview_update()))
                c.Bind(wx.EVT_KILL_FOCUS, self._on_commit_focus_loss)
                ctrl = c

            self._field_ctrls[path] = ctrl
            self._fields_sizer.Add(ctrl, 1, wx.EXPAND)

        self._fields_scroll.Layout()
        self._fields_scroll.FitInside()

    def _reset_kind_ui_to_defaults_best_effort(self) -> None:
        """
        Ensure nothing "leaks" visually between kinds.

        Dynamic fields are rebuilt with defaults in `_build_fields()`.
        These shared controls must be reset when switching to a kind with no saved state.
        """
        self._restoring = True
        try:
            # Density default: N only
            try:
                self.den_L.SetValue(False)
                self.den_N.SetValue(True)
                self.den_M.SetValue(False)
                self.den_all.SetValue(False)
            except Exception:
                pass

            # Clear per-density overrides (not persisted)
            try:
                for d, ctrl in list(self._nd_name.items()):
                    ctrl.ChangeValue("")
                for d, ctrl in list(self._nd_desc.items()):
                    ctrl.ChangeValue("")
            except Exception:
                pass

            # Preview status reset
            try:
                self.prev_status.SetLabel("(preview updates on Enter or when leaving a field)")
            except Exception:
                pass
            try:
                self._update_preview_density_choices()
            except Exception:
                pass
        finally:
            self._restoring = False

    def _restore_global_state_best_effort(self) -> None:
        """
        Restore last-used kind and (optionally) global output directory.
        """
        self._restoring = True
        try:
            g = self._state.get("global", {}) if isinstance(self._state, dict) else {}
            last_kind = g.get("last_kind")
            if last_kind and isinstance(last_kind, str):
                try:
                    self.kind.SetStringSelection(last_kind)
                except Exception:
                    pass
            # Prevent cross-project leakage:
            # - Prefer an out_dir saved for THIS repo_path (stable for per-project submodules)
            # - Otherwise, only restore the global out_dir if it belongs to this repo's Footprints/
            out_dir = ""
            try:
                repo_key = os.path.abspath(str(getattr(self, "_repo_path", "") or "").strip())
            except Exception:
                repo_key = ""
            try:
                by_repo = g.get("out_dir_by_repo")
                if repo_key and isinstance(by_repo, dict):
                    cand = by_repo.get(repo_key)
                    if isinstance(cand, str) and cand.strip():
                        out_dir = cand.strip()
            except Exception:
                out_dir = ""
            if not out_dir:
                cand2 = g.get("out_dir")
                if isinstance(cand2, str) and cand2.strip():
                    out_dir = cand2.strip()
            if out_dir:
                self._set_out_dir_best_effort(out_dir, allow_external=False)
        finally:
            self._restoring = False

    def _out_dir_belongs_to_this_repo(self, out_dir: str) -> bool:
        """
        True if out_dir is within this repo's Footprints/ folder.
        """
        p = str(out_dir or "").strip()
        if not p:
            return False
        try:
            root = os.path.abspath(str(getattr(self, "_fp_root", "") or "").strip())
            ap = os.path.abspath(p)
            if not (root and ap):
                return False
            # commonpath raises on different drives (Windows).
            return os.path.commonpath([ap, root]) == root
        except Exception:
            return False

    def _set_out_dir_best_effort(self, out_dir: str, *, allow_external: bool = True) -> None:
        if not out_dir:
            return
        if not allow_external and not self._out_dir_belongs_to_this_repo(out_dir):
            # Ignore persisted out_dir from another project/repo.
            return
        # Ensure choice list has an entry for this directory.
        label = os.path.basename(out_dir.rstrip(os.sep)) or out_dir
        try:
            labels = [self.out_choice.GetString(i) for i in range(self.out_choice.GetCount())]
            if label not in labels:
                self.out_choice.Append(label)
                self._pretty_dirs.append(out_dir)
            self.out_choice.SetStringSelection(label)
        except Exception:
            return

    def _restore_kind_state_best_effort(self, kind: str) -> None:
        self._restoring = True
        try:
            kinds = self._state.get("kinds", {}) if isinstance(self._state, dict) else {}
            ks = kinds.get(kind, {}) if isinstance(kinds, dict) else {}
            if not isinstance(ks, dict):
                # No saved state for this kind yet; keep defaults.
                return

            # Restore densities
            dens = ks.get("densities")
            if isinstance(dens, list) and dens:
                self._set_selected_densities_best_effort([str(x) for x in dens])
            else:
                # Back-compat for old state
                den = ks.get("density")
                if isinstance(den, str) and den:
                    self._set_selected_densities_best_effort([den])

            out_dir = ks.get("out_dir")
            if isinstance(out_dir, str) and out_dir:
                self._set_out_dir_best_effort(out_dir, allow_external=False)

            # Restore dynamic fields
            vals = ks.get("fields", {})
            if isinstance(vals, dict):
                for path, v in vals.items():
                    ctrl = self._field_ctrls.get(str(path))
                    if not ctrl:
                        continue
                    try:
                        if isinstance(ctrl, wx.CheckBox):
                            ctrl.SetValue(bool(v))
                        elif isinstance(ctrl, wx.SpinCtrl):
                            ctrl.SetValue(int(v))
                        elif isinstance(ctrl, wx.Choice):
                            ctrl.SetStringSelection(str(v))
                        elif isinstance(ctrl, wx.TextCtrl):
                            ctrl.ChangeValue("" if v is None else str(v))
                    except Exception:
                        continue
        finally:
            self._restoring = False
        self._rebuild_name_desc_overrides()
        self._update_preview_density_choices()
        self._schedule_hints_update()

    def _persist_current_kind_state_best_effort(self) -> None:
        """
        Save state for the currently selected kind.
        """
        kind = self.kind.GetStringSelection() or "soic"
        self._persist_kind_state_best_effort(kind, update_global=True)

    def _persist_kind_state_best_effort(self, kind: str, update_global: bool = True) -> None:
        """
        Save state for an explicit kind. This avoids "leakage" on Kind switch events where the
        selection has already changed but the controls still reflect the previous kind.
        """
        if not kind:
            kind = "soic"
        if not isinstance(self._state, dict):
            self._state = {"version": _STATE_VERSION, "global": {}, "kinds": {}}
        self._state.setdefault("global", {})
        self._state.setdefault("kinds", {})

        densities = self._selected_densities()
        out_dir = self._selected_out_dir()
        # Persistence should NOT bake in implied `.nom` values; keep them empty unless user overrides.
        fields = self._gather_fields(include_nominal_mean=False)

        if update_global:
            g = self._state.get("global", {})
            if isinstance(g, dict):
                g["last_kind"] = self.kind.GetStringSelection() or kind
                if out_dir:
                    g["out_dir"] = out_dir
                    # Also persist per-repo so switching projects doesn't leak across repos.
                    try:
                        repo_key = os.path.abspath(str(getattr(self, "_repo_path", "") or "").strip())
                    except Exception:
                        repo_key = ""
                    if repo_key:
                        try:
                            m = g.get("out_dir_by_repo")
                            if not isinstance(m, dict):
                                m = {}
                                g["out_dir_by_repo"] = m
                            m[repo_key] = out_dir
                        except Exception:
                            pass
            else:
                self._state["global"] = {"last_kind": (self.kind.GetStringSelection() or kind), "out_dir": out_dir}

        kinds = self._state.get("kinds", {})
        if not isinstance(kinds, dict):
            kinds = {}
            self._state["kinds"] = kinds
        kinds[kind] = {
            "densities": list(densities),
            "out_dir": out_dir,
            "fields": fields,
        }

        _save_state_best_effort(self._state)

    def _on_close_button(self, _evt: wx.CommandEvent) -> None:
        try:
            k = str(getattr(self, "_active_kind", "") or (self.kind.GetStringSelection() or "soic"))
            self._persist_kind_state_best_effort(k, update_global=True)
        except Exception:
            pass
        try:
            if self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            else:
                self.Close()
        except Exception:
            try:
                self.Destroy()
            except Exception:
                pass

    def _parse_float_best_effort(self, txt: str, fallback: float) -> float:
        """
        Accept common user inputs like "5.8mm" or "5,8" and extract a float.
        """
        s = (txt or "").strip().lower()
        if not s:
            return float(fallback)
        s = s.replace(",", ".")
        # Strip a common unit suffix.
        if s.endswith("mm"):
            s = s[:-2].strip()
        m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", s)
        if not m:
            return float(fallback)
        try:
            return float(m.group(0))
        except Exception:
            return float(fallback)

    def _gather_fields(self, *, include_nominal_mean: bool) -> Dict[str, Any]:
        """
        Gather current UI fields into the dict passed to `element_from_fields`.

        - When `include_nominal_mean=True`, empty `.nom` range values are treated as mean(min,max).
          This is what generation/preview/hints need.
        - When `include_nominal_mean=False`, empty `.nom` values are omitted so persistence does not
          "bake in" implied nominal values (prevents leakage on reopen/reset).
        """
        out: Dict[str, Any] = {}
        kind = self.kind.GetStringSelection() or "soic"
        specs = schema_for_kind(kind)

        defaults_by_path: dict[str, float] = {}
        for _lbl, pth, dflt, _ch in specs:
            if isinstance(dflt, float):
                defaults_by_path[str(pth)] = float(dflt)

        for _label, path, default, choices in specs:
            ctrl = self._field_ctrls.get(path)
            if not ctrl:
                continue
            try:
                if isinstance(ctrl, wx.CheckBox):
                    out[path] = bool(ctrl.GetValue())
                elif isinstance(ctrl, wx.SpinCtrl):
                    out[path] = int(ctrl.GetValue())
                elif isinstance(ctrl, wx.Choice):
                    sel = ctrl.GetStringSelection()
                    if choices and isinstance(choices[0], int):
                        try:
                            out[path] = int(sel)
                        except Exception:
                            out[path] = int(default) if isinstance(default, int) else sel
                    else:
                        out[path] = sel
                elif isinstance(ctrl, wx.TextCtrl):
                    txt = (ctrl.GetValue() or "").strip()
                    if isinstance(default, float):
                        p = str(path)
                        if txt:
                            out[path] = self._parse_float_best_effort(txt, float(default))
                        else:
                            if p.endswith(".nom"):
                                if not include_nominal_mean:
                                    # Don't persist implied nominal.
                                    continue
                                base = p[:-4]
                                cmin = self._field_ctrls.get(base + ".min")
                                cmax = self._field_ctrls.get(base + ".max")
                                tmin = (cmin.GetValue() or "").strip() if isinstance(cmin, wx.TextCtrl) else ""
                                tmax = (cmax.GetValue() or "").strip() if isinstance(cmax, wx.TextCtrl) else ""
                                vmin = self._parse_float_best_effort(tmin, defaults_by_path.get(base + ".min", float(default)))
                                vmax = self._parse_float_best_effort(tmax, defaults_by_path.get(base + ".max", float(default)))
                                out[path] = (vmin + vmax) / 2.0
                            else:
                                out[path] = float(default)
                    elif isinstance(default, int):
                        out[path] = int(self._parse_float_best_effort(txt, float(default))) if txt else int(default)
                    else:
                        out[path] = txt
                else:
                    out[path] = default
            except Exception:
                out[path] = default
        return out

    def _selected_densities(self) -> list[str]:
        """
        Return selected densities in stable order.
        """
        out: list[str] = []
        try:
            if self.den_L.GetValue():
                out.append("L")
            if self.den_N.GetValue():
                out.append("N")
            if self.den_M.GetValue():
                out.append("M")
        except Exception:
            pass
        return out or ["N"]

    def _set_selected_densities_best_effort(self, dens: list[str]) -> None:
        ds = {(d or "").strip().upper() for d in (dens or [])}
        self._restoring = True
        try:
            self.den_L.SetValue("L" in ds)
            self.den_N.SetValue(("N" in ds) or (not ds))
            self.den_M.SetValue("M" in ds)
            self.den_all.SetValue(self.den_L.GetValue() and self.den_N.GetValue() and self.den_M.GetValue())
        except Exception:
            pass
        finally:
            self._restoring = False

    def _on_toggle_all_densities(self, _evt: wx.CommandEvent) -> None:
        if self._restoring:
            return
        val = bool(self.den_all.GetValue())
        try:
            self.den_L.SetValue(val)
            self.den_N.SetValue(val)
            self.den_M.SetValue(val)
        except Exception:
            pass
        self._rebuild_name_desc_overrides()
        self._update_preview_density_choices()
        self._schedule_hints_update()
        self._schedule_preview_update()

    def _on_density_toggle(self, _evt: wx.CommandEvent) -> None:
        if self._restoring:
            return
        # Ensure at least one density is selected.
        dens = self._selected_densities()
        if not dens:
            try:
                self.den_N.SetValue(True)
            except Exception:
                pass
        try:
            self.den_all.SetValue(self.den_L.GetValue() and self.den_N.GetValue() and self.den_M.GetValue())
        except Exception:
            pass
        self._rebuild_name_desc_overrides()
        self._update_preview_density_choices()
        self._schedule_hints_update()
        self._schedule_preview_update()

    def _rebuild_name_desc_overrides(self) -> None:
        """
        Rebuild the per-density override rows based on selected densities.
        """
        # Preserve current (non-persisted) values while rebuilding.
        prev_name: dict[str, str] = {}
        prev_desc: dict[str, str] = {}
        try:
            for d, c in list(self._nd_name.items()):
                prev_name[d] = (c.GetValue() or "")
            for d, c in list(self._nd_desc.items()):
                prev_desc[d] = (c.GetValue() or "")
        except Exception:
            pass

        parent = getattr(self, "_left_panel", None) or self
        try:
            try:
                parent.Freeze()
            except Exception:
                pass

            try:
                self._nd_grid.Clear(delete_windows=True)
            except Exception:
                return
            self._nd_name.clear()
            self._nd_desc.clear()

            # Header
            self._nd_grid.Add(wx.StaticText(parent, label="Density"), 0, wx.ALIGN_CENTER_VERTICAL)
            self._nd_grid.Add(wx.StaticText(parent, label="Name"), 0, wx.ALIGN_CENTER_VERTICAL)
            self._nd_grid.Add(wx.StaticText(parent, label="Description"), 0, wx.ALIGN_CENTER_VERTICAL)
        finally:
            pass

        for d in self._selected_densities():
            self._nd_grid.Add(wx.StaticText(parent, label=self._density_label(d)), 0, wx.ALIGN_CENTER_VERTICAL)
            n = wx.TextCtrl(parent, value="", style=wx.TE_PROCESS_ENTER)
            n.Bind(wx.EVT_TEXT_ENTER, lambda _e: self._schedule_preview_update())
            n.Bind(wx.EVT_KILL_FOCUS, self._on_commit_focus_loss)
            self._nd_grid.Add(n, 1, wx.EXPAND)
            ds = wx.TextCtrl(parent, value="", style=wx.TE_PROCESS_ENTER)
            ds.Bind(wx.EVT_TEXT_ENTER, lambda _e: self._schedule_preview_update())
            ds.Bind(wx.EVT_KILL_FOCUS, self._on_commit_focus_loss)
            self._nd_grid.Add(ds, 1, wx.EXPAND)
            self._nd_name[d] = n
            self._nd_desc[d] = ds
            try:
                if d in prev_name:
                    n.ChangeValue(prev_name.get(d, ""))
                if d in prev_desc:
                    ds.ChangeValue(prev_desc.get(d, ""))
            except Exception:
                pass

        # Force immediate layout (otherwise new controls can appear at 0,0 until a resize event).
        try:
            lp = getattr(self, "_left_panel", None)
            ls = getattr(self, "_left_sizer", None)
            if ls:
                ls.Layout()
            if lp:
                lp.Layout()
            try:
                sp = getattr(self, "_splitter", None)
                if sp:
                    sp.Layout()
                    # Nudge splitter to recompute child sizes.
                    try:
                        sp.SetSashPosition(sp.GetSashPosition())
                    except Exception:
                        pass
            except Exception:
                pass
            self.Layout()
            try:
                self.SendSizeEvent()
            except Exception:
                pass
        except Exception:
            pass
        finally:
            try:
                parent.Thaw()
            except Exception:
                pass

    def _density_label(self, density: str) -> str:
        d = (density or "").strip().upper()
        return {"L": "Least", "N": "Nominal", "M": "Most"}.get(d, d or "?")

    def _update_preview_density_choices(self) -> None:
        """
        Keep the preview-density dropdown in sync with currently selected densities.
        """
        try:
            dens = self._selected_densities()
            cur = self._get_preview_density_selected_code()
            self.prev_density_choice.Clear()
            for d in dens:
                # Store code as client data.
                try:
                    self.prev_density_choice.Append(self._density_label(d), d)
                except Exception:
                    self.prev_density_choice.Append(self._density_label(d))

            # Prefer keeping current choice if still present, else prefer Nominal.
            target = cur if cur in dens else ("N" if "N" in dens else dens[0])
            for i in range(self.prev_density_choice.GetCount()):
                try:
                    cd = self.prev_density_choice.GetClientData(i)
                except Exception:
                    cd = None
                if (cd or "").strip().upper() == target:
                    self.prev_density_choice.SetSelection(i)
                    break
            if self.prev_density_choice.GetSelection() < 0 and self.prev_density_choice.GetCount() > 0:
                self.prev_density_choice.SetSelection(0)
        except Exception:
            pass

    def _get_preview_density_selected_code(self) -> str:
        try:
            idx = int(self.prev_density_choice.GetSelection())
        except Exception:
            idx = -1
        if idx < 0:
            return ""
        try:
            cd = self.prev_density_choice.GetClientData(idx)
        except Exception:
            cd = None
        s = str(cd or "").strip().upper()
        return s

    def _density_for_preview(self) -> str:
        dens = self._selected_densities()
        chosen = (self._get_preview_density_selected_code() or "").strip().upper()
        if chosen and chosen in dens:
            return chosen
        return "N" if "N" in dens else dens[0]

    def _override_name_for(self, density: str) -> str:
        c = self._nd_name.get(density)
        if not c:
            return ""
        return (c.GetValue() or "").strip()

    def _override_desc_for(self, density: str) -> str:
        c = self._nd_desc.get(density)
        if not c:
            return ""
        return (c.GetValue() or "").strip()

    def _update_hints_best_effort(self) -> None:
        """
        Update hint text (auto-generated name/description) for each selected density.
        """
        try:
            kind = self.kind.GetStringSelection() or "soic"
            fields = self._gather_fields(include_nominal_mean=True)
            dens = self._selected_densities()
        except Exception:
            return

        # Range nominal hints (mean(min,max)) should update dynamically when min/max change.
        try:
            for base, ctrls in (getattr(self, "_range_groups", {}) or {}).items():
                cnom = ctrls.get("nom")
                cmin = ctrls.get("min")
                cmax = ctrls.get("max")
                if not (isinstance(cnom, wx.TextCtrl) and isinstance(cmin, wx.TextCtrl) and isinstance(cmax, wx.TextCtrl)):
                    continue
                if (cnom.GetValue() or "").strip():
                    continue  # user override; keep hint irrelevant
                try:
                    tmin = (cmin.GetValue() or "").strip()
                    tmax = (cmax.GetValue() or "").strip()
                    vmin = self._parse_float_best_effort(tmin, 0.0) if tmin else None
                    vmax = self._parse_float_best_effort(tmax, 0.0) if tmax else None
                    if vmin is None or vmax is None:
                        dflt = (getattr(self, "_range_defaults", {}) or {}).get(base, {})
                        vmin = vmin if vmin is not None else float(dflt.get("min", 0.0))
                        vmax = vmax if vmax is not None else float(dflt.get("max", 0.0))
                    cnom.SetHint(f"{((vmin + vmax) / 2.0):.3f}")
                except Exception:
                    pass
        except Exception:
            pass

        # Name hints are cheap.
        name_hints: dict[str, str] = {}
        for d in dens:
            try:
                name_hints[d] = compute_auto_name(kind=kind, density=d, name="", fields=fields) or ""
            except Exception:
                name_hints[d] = ""

        for d, ctrl in list(self._nd_name.items()):
            try:
                ctrl.SetHint(name_hints.get(d, ""))
            except Exception:
                pass

        # Description hints: compute in background (pattern build).
        self._hint_job_id += 1
        job_id = int(self._hint_job_id)

        def work():
            out: dict[str, str] = {}
            for d in dens:
                nm = name_hints.get(d) or compute_auto_name(kind=kind, density=d, name="", fields=fields) or ""
                element = element_from_fields(kind=kind, density=d, name=nm, fields=fields)
                pat = build_pattern(kind, element)
                out[d] = str(getattr(pat, "description", "") or "")
            return out

        def done(res, err):
            if err or not isinstance(res, dict):
                return
            if job_id != self._hint_job_id:
                return
            for d, ctrl in list(self._nd_desc.items()):
                try:
                    ctrl.SetHint(str(res.get(d, "") or ""))
                except Exception:
                    pass

        _run_in_bg(work, done)

    def _selected_out_dir(self) -> str:
        idx = int(self.out_choice.GetSelection())
        if idx < 0:
            return ""
        if idx >= len(self._pretty_dirs):
            return ""
        return str(self._pretty_dirs[idx] or "")

    def _on_commit_focus_loss(self, evt: wx.FocusEvent) -> None:
        """
        When leaving a textbox/spinctrl, update preview.
        """
        try:
            self._schedule_hints_update()
            self._schedule_preview_update()
        finally:
            try:
                evt.Skip()
            except Exception:
                pass

    def _schedule_preview_update(self) -> None:
        try:
            if getattr(self, "_preview_debouncer", None):
                self._preview_debouncer.trigger(delay_ms=250)
            self._preview_pending = True
        except Exception:
            pass

    def _on_preview_size(self, evt: wx.SizeEvent) -> None:
        # Re-render at new size (debounced).
        try:
            self._schedule_preview_update()
        finally:
            try:
                evt.Skip()
            except Exception:
                pass

    def _on_preview_timer(self, _evt=None) -> None:
        if not self._preview_pending:
            return
        self._preview_pending = False
        self._start_preview_render()

    def _start_preview_render(self) -> None:
        """
        Generate footprint into a temp `.pretty` and render it to a bitmap.
        """
        if self._closing:
            return

        # Reuse preview cache helpers from the parts manager UI (v2).
        try:
            from library_manager.ui.assets.preview import PREVIEW_CACHE_VERSION, hash_key, hires_target_px  # type: ignore
        except Exception:
            try:
                self.prev_status.SetLabel("Preview unavailable (missing preview helpers).")
            except Exception:
                pass
            return

        # Build a stable key for caching / skipping rerenders.
        kind = self.kind.GetStringSelection() or "soic"
        density = self._density_for_preview()
        fields = self._gather_fields(include_nominal_mean=True)
        name = self._override_name_for(density) or (compute_auto_name(kind=kind, density=density, name="", fields=fields) or "")
        desc_override = self._override_desc_for(density)

        try:
            pw, ph = self.prev_bmp.GetClientSize()
        except Exception:
            pw, ph = (520, 320)
        png_w, png_h = hires_target_px(self.prev_bmp, pw, ph, quality_scale=2.5)

        key = hash_key(
            "fpgen_prev:"
            + PREVIEW_CACHE_VERSION
            + ":"
            + kind
            + ":"
            + density
            + ":"
            + name
            + ":"
            + desc_override
            + ":"
            + repr(sorted(fields.items()))
            + f":{png_w}x{png_h}"
        )
        if key == self._preview_last_key:
            return
        self._preview_last_key = key

        try:
            self.prev_status.SetLabel("Rendering preview…")
        except Exception:
            pass

        # Preferred path: PreviewPanel handles async + caching; generator supplies SVG renderer.
        if getattr(self, "_preview_panel", None):
            ref = f"{kind}:{density}:{name}"

            def _render_svg(_ref: str, out_svg_path: str) -> None:
                self._render_preview_svg(
                    kind=kind,
                    density=density,
                    name=name,
                    desc_override=desc_override,
                    fields=fields,
                    out_svg_path=out_svg_path,
                )

            self._preview_panel.render_cached_svg_async(
                kind_dir="fpgen",
                cache_key_prefix="fpgen_prev",
                ref=ref,
                source_mtime=key,  # stable key for parameterized preview
                render_svg=_render_svg,
                quality_scale=2.5,
            )
            return

        # Fallback path (no PreviewPanel available): keep old behavior, but still render SVG with kicad-cli.
        self._preview_job_id += 1
        job_id = int(self._preview_job_id)

        def work():
            # Worst-case fallback: render PNG by calling into the v2 preview pipeline.
            from library_manager.ui.assets.preview import cached_svg_and_png  # type: ignore

            ref = f"{kind}:{density}:{name}"

            def _render_svg(_ref: str, out_svg_path: str) -> None:
                self._render_preview_svg(
                    kind=kind,
                    density=density,
                    name=name,
                    desc_override=desc_override,
                    fields=fields,
                    out_svg_path=out_svg_path,
                )

            raster = cached_svg_and_png(
                kind_dir="fpgen",
                cache_key_prefix="fpgen_prev",
                ref=ref,
                source_mtime=key,
                png_w=png_w,
                png_h=png_h,
                render_svg=_render_svg,
            )
            return raster.png_path

        def done(bmp, err):
            if self._closing or job_id != self._preview_job_id:
                return
            if err or not bmp:
                try:
                    self.prev_status.SetLabel(f"Preview unavailable: {err}")
                    self.prev_bmp.SetBitmap(wx.NullBitmap)
                    self.prev_bmp.Refresh()
                except Exception:
                    pass
                return
            try:
                img = wx.Image(str(bmp))
                if not img.IsOk():
                    raise RuntimeError("PNG load failed")
                real_bmp = wx.Bitmap(img)
                w, h = self.prev_bmp.GetClientSize()
                from library_manager.ui.assets.preview import letterbox_bitmap  # type: ignore

                boxed = letterbox_bitmap(real_bmp, w, h)
                self.prev_bmp.SetBitmap(boxed or real_bmp)
                self.prev_bmp.Refresh()
                self.prev_status.SetLabel("")
            except Exception:
                pass

        _run_in_bg(work, done)

    def _render_preview_svg(
        self,
        *,
        kind: str,
        density: str,
        name: str,
        desc_override: str,
        fields: dict[str, str],
        out_svg_path: str,
    ) -> None:
        """
        Generate a footprint into a temp `.pretty`, export SVG via kicad-cli, and write to out_svg_path.
        Used by the shared preview pipeline.
        """
        import shutil as _shutil

        pretty_dir = tempfile.mkdtemp(prefix="fpgen_prev_", suffix=".pretty", dir=tempfile.gettempdir())
        tmp_dir = tempfile.mkdtemp(prefix="fpgen_", dir=tempfile.gettempdir())
        try:
            element = element_from_fields(kind=kind, density=density, name=name, fields=fields)
            if desc_override:
                element["description_override"] = desc_override
            mod_path = generate_footprint(kind, element, pretty_dir)
            fp_name = os.path.splitext(os.path.basename(mod_path))[0]

            layers = "F.Cu,F.Mask,F.SilkS,F.Fab,F.CrtYd"
            exe = "kicad-cli"
            try:
                if resolve_kicad_cli:
                    exe = resolve_kicad_cli()
            except Exception:
                exe = "kicad-cli"
            cp = subprocess.run(
                [exe, "fp", "export", "svg", "-o", tmp_dir, "--fp", fp_name, "--layers", layers, pretty_dir],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                **SUBPROCESS_NO_WINDOW,
            )
            if cp.returncode != 0:
                raise RuntimeError((cp.stdout or "").strip() or "kicad-cli fp export failed")
            svgs = sorted(glob.glob(os.path.join(tmp_dir, "*.svg")))
            if not svgs:
                raise RuntimeError("No SVG produced for footprint preview")
            os.makedirs(os.path.dirname(out_svg_path), exist_ok=True)
            os.replace(svgs[0], out_svg_path)
        finally:
            try:
                _shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
            try:
                _shutil.rmtree(pretty_dir, ignore_errors=True)
            except Exception:
                pass

    # --------------------------
    # Actions
    # --------------------------

    def _on_generate(self, _evt: wx.CommandEvent) -> None:
        try:
            out_dir = self._selected_out_dir()
            if not out_dir:
                self.status.SetLabel("Select an output .pretty directory first.")
                return
            if not out_dir.endswith(".pretty"):
                self.status.SetLabel("Output directory should be a .pretty folder (KiCad footprint library).")
                return

            kind = self.kind.GetStringSelection() or "soic"
            fields = self._gather_fields(include_nominal_mean=True)
            densities = self._selected_densities()

            # Pre-flight: detect overwrites and warn once.
            planned: list[tuple[str, str]] = []  # (density, name)
            collisions: list[str] = []
            for d in densities:
                name = self._override_name_for(d) or (compute_auto_name(kind=kind, density=d, name="", fields=fields) or "")
                if not name:
                    raise RuntimeError(f"Could not compute name for density {d}")
                planned.append((d, name))
                out_path = os.path.join(out_dir, f"{name}.kicad_mod")
                if os.path.exists(out_path):
                    collisions.append(f"- {self._density_label(d)}: {os.path.basename(out_path)}")

            if collisions:
                msg = (
                    "One or more footprints already exist in the selected output folder.\n\n"
                    "If you continue, they will be overwritten:\n"
                    + "\n".join(collisions)
                    + "\n\nContinue?"
                )
                if wx.MessageBox(msg, "Overwrite existing footprints?", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING) != wx.YES:
                    self.status.SetLabel("Generate cancelled (would overwrite existing footprint).")
                    return

            paths: list[str] = []
            for d, name in planned:
                element = element_from_fields(kind=kind, density=d, name=name, fields=fields)
                desc_override = self._override_desc_for(d)
                if desc_override:
                    element["description_override"] = desc_override
                p = generate_footprint(kind, element, out_dir)
                paths.append(p)

            if len(paths) == 1:
                self.status.SetLabel(f"Generated: {paths[0]}")
            else:
                self.status.SetLabel(f"Generated {len(paths)} footprints.")

            # Tell parent (main plugin) to refresh asset status if it can.
            try:
                owner = getattr(self, "_owner", None) or self.GetParent()
                if owner and hasattr(owner, "_append_log"):
                    for p in paths:
                        owner._append_log(f"Generated footprint: {p}")  # type: ignore[misc]
                if owner and hasattr(owner, "_refresh_assets_status"):
                    owner._refresh_assets_status()  # type: ignore[misc]
                if owner and hasattr(owner, "_refresh_sync_status"):
                    owner._refresh_sync_status()  # type: ignore[misc]
            except Exception:
                pass

        except Exception as e:
            self.status.SetLabel(f"Generate failed: {e}")
            wx.MessageBox(
                "Footprint generation failed:\n\n" + str(e) + "\n\n" + traceback.format_exc(),
                "Create footprint",
                wx.OK | wx.ICON_ERROR,
            )

