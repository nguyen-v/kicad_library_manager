from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import wx

try:
    import wx.adv as _wxadv  # type: ignore
except Exception:
    _wxadv = None

try:
    import wx.gizmos as _wxgizmos  # type: ignore
except Exception:
    _wxgizmos = None

try:
    import wx.dataview as _wxdv  # type: ignore
except Exception:
    _wxdv = None

from ..async_ui import UiDebouncer, UiRepeater, WindowTaskRunner
from ..git_ops import fetch_stale_threshold_seconds, format_age_minutes, git_fetch_head_age_seconds, git_last_updated_epoch
from ..icons import make_status_bitmap
from .debuglog import log_line as _dbg
from ..preview_panel import PreviewPanel
from .search import norm, search_backend_info, search_hits_by_lib
from .status import asset_change_sets, local_summary_scoped, remote_summary_scoped
from ..window_title import with_library_suffix


class AssetIndexProvider(Protocol):
    def ensure_started(self, repo_path: str) -> None: ...

    def snapshot(self, repo_path: str) -> dict[str, Any]: ...


class AssetBrowserProvider(Protocol):
    # ---- presentation ----
    kind_title: str  # "Browse footprints" / "Browse symbols"
    kind_label: str  # "Footprints" / "Symbols"
    item_label: str  # "footprint" / "symbol"
    tree_col1: str
    tree_col2: str
    search_hint: str
    preview_box_title: str
    empty_preview_label: str

    # ---- status scopes ----
    scope_dirs: list[str]  # e.g. ["Footprints"] / ["Symbols"]
    scope_key: str  # "footprints" / "symbols"
    preview_kind_dir: str  # cache subdir: "fp" / "sym"
    preview_cache_key_prefix: str  # "fp_browse" / "sym_browse"

    # ---- indexing ----
    index: AssetIndexProvider
    snapshot_key_items: str  # key in snapshot dict containing list[str] refs

    def list_local_refs(self, repo_path: str) -> list[str]:
        """Return repo-local refs (fast filesystem scan)."""

    def group_variants(self, refs: list[str]) -> dict[str, list[str]]:
        """Return base->variants mapping. For non-variant assets, base==ref and variants=[ref]."""

    # ---- per-ref helpers ----
    def lib_is_repo_local(self, repo_path: str, lib: str) -> bool:
        """True if this lib is backed by repo-local files (used for ranking + delete safety)."""

    def rel_prefix_for_lib(self, repo_path: str, lib: str) -> str:
        """
        Repo-relative path prefix (ending with '/') or exact file path for this library,
        used to compute aggregate status icon.

        Return "" if unknown / non-repo-local.
        """

    def rel_path_for_ref(self, repo_path: str, ref: str) -> str:
        """Repo-relative path used for status icons and git_last_updated_epoch; return '' if non-local."""

    def source_mtime_for_ref(self, repo_path: str, ref: str) -> str:
        """A string that changes when preview source changes (usually filesystem mtime)."""

    def extract_description_for_ref(self, repo_path: str, ref: str) -> str:
        """Short description/tags string used in UI + search."""

    def render_svg(self, repo_path: str, ref: str, out_svg_path: str) -> None:
        """Render an SVG for preview (typically via kicad-cli)."""

    # ---- actions ----
    def can_delete_ref(self, repo_path: str, base_ref: str) -> tuple[bool, str]:
        """Return (ok, reason_if_not_ok)."""

    def delete_ref_and_variants(self, repo_path: str, base_ref: str, variants: list[str]) -> list[str]:
        """Perform deletion. Return list of failure strings (empty means success)."""

    # Optional create button (footprints)
    create_button_label: str | None
    on_create: Callable[[wx.CommandEvent], None] | None


@dataclass(frozen=True)
class _SearchResult:
    q: str
    hits_by_lib: dict[str, list[str]]
    truncated: bool
    shown: int
    lib_best: dict[str, float]


class AssetBrowserDialogBase(wx.Dialog):
    """
    Generic asset browser dialog used by both footprints and symbols.
    Provider is responsible for domain-specific operations.
    """

    def __init__(self, parent: wx.Window, repo_path: str, provider: AssetBrowserProvider, *, picker_mode: bool = False):
        super().__init__(
            parent,
            title=with_library_suffix(provider.kind_title, repo_path),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )
        self._repo_path = repo_path
        self._p = provider
        self._tasks = WindowTaskRunner(self)
        self._picker_mode = bool(picker_mode)
        self._picked_ref: str = ""
        self._picked_refs: list[str] = []

        _dbg(f"{self._p.kind_label}: init repo_path={repo_path!r}")
        try:
            self._p.index.ensure_started(repo_path)
        except Exception:
            pass

        # Sources + derived indices
        self._groups: dict[str, list[str]] = {}
        self._variant_to_base: dict[str, str] = {}
        self._bases_all: list[str] = []
        self._bases_lc: list[str] = []
        self._bases_lib: list[str] = []
        self._lib_to_bases: dict[str, list[str]] = {}

        # Tree bookkeeping
        self._lib_nodes: dict[str, object] = {}
        self._lib_populated: set[str] = set()
        self._base_to_item: dict[str, object] = {}

        # Descriptions + prefetch-all
        self._descr_cache: dict[str, str] = {}
        self._descr_prefetch_all_started = False
        self._descr_prefetch_all_done = False
        self._descr_prefetch_all_cancel = False

        # Search state
        self._search_pending_q = ""
        self._search_pending_q_last = ""
        self._search_q = ""
        self._search_gen = 0
        self._search_result: dict[str, list[str]] | None = None
        self._search_result_q = ""
        self._search_lib_best: dict[str, float] = {}
        self._search_inflight = False
        self._search_debouncer = UiDebouncer(self, delay_ms=500, callback=lambda: self._on_search_timer(None))

        # Asset status sets for icons
        self._asset_local: set[str] = set()
        self._asset_remote: set[str] = set()
        self._asset_remote_known = False

        self._updated_cache: dict[str, int] = {}
        self._col_dragging = False
        self._pending_descr_updates: dict[str, str] = {}
        self._closing = False

        def _mark_closing(evt=None):
            self._closing = True
            self._descr_prefetch_all_cancel = True
            self._stop_timers_best_effort()
            if evt is not None:
                evt.Skip()

        self.Bind(wx.EVT_CLOSE, _mark_closing)
        try:
            self.Bind(wx.EVT_WINDOW_DESTROY, _mark_closing)
        except Exception:
            pass
        try:
            self.Bind(wx.EVT_ACTIVATE, self._on_activate)
        except Exception:
            pass
        # ESC should close these modal "subwindows" (macOS especially expects this).
        try:
            self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        except Exception:
            pass

        # Build UI
        v = wx.BoxSizer(wx.VERTICAL)

        top = wx.BoxSizer(wx.HORIZONTAL)
        self._assets_icon = wx.StaticBitmap(self, bitmap=make_status_bitmap(wx.Colour(160, 160, 160)))
        self._assets_label = wx.StaticText(self, label=f"{self._p.kind_label}: loading…")
        top.Add(self._assets_icon, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        top.Add(self._assets_label, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        top.AddStretchSpacer(1)
        self.fetch_btn = wx.Button(self, label="↓  Fetch remote")
        self.fetch_btn.Bind(wx.EVT_BUTTON, lambda _e: self._fetch_remote_async_notify_parent())
        top.Add(self.fetch_btn, 0, wx.ALL, 6)
        self.sync_btn = wx.Button(self, label="↻  Sync library")
        self.sync_btn.Bind(wx.EVT_BUTTON, self._on_sync)
        top.Add(self.sync_btn, 0, wx.ALL, 6)
        v.Add(top, 0, wx.EXPAND)

        # Optional extra status line (no icon), e.g. for symbol index/meta progress.
        self._index_line = wx.StaticText(self, label="")
        try:
            self._index_line.SetForegroundColour(wx.Colour(90, 90, 90))
        except Exception:
            pass
        try:
            show_index_line = bool(getattr(self._p, "show_index_line", False))
        except Exception:
            show_index_line = False
        self._index_line.Show(bool(show_index_line))
        if show_index_line:
            v.Add(self._index_line, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        self.filter = wx.TextCtrl(self)
        self.filter.SetHint(self._p.search_hint)
        self.filter.Bind(wx.EVT_TEXT, self._on_filter)
        v.Add(self.filter, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        # Picker-only options (e.g. footprints: "All densities").
        self._picker_all_densities_cb: wx.CheckBox | None = None
        try:
            show_all_dens = bool(getattr(self._p, "picker_show_all_densities", False))
        except Exception:
            show_all_dens = False
        if self._picker_mode and show_all_dens:
            try:
                label = str(getattr(self._p, "picker_all_densities_label", "") or "").strip() or "All densities (N;L;M) when available"
            except Exception:
                label = "All densities (N;L;M) when available"
            try:
                default_val = bool(getattr(self._p, "picker_all_densities_default", True))
            except Exception:
                default_val = True
            row = wx.BoxSizer(wx.HORIZONTAL)
            self._picker_all_densities_cb = wx.CheckBox(self, label=label)
            try:
                self._picker_all_densities_cb.SetValue(bool(default_val))
            except Exception:
                pass
            row.Add(self._picker_all_densities_cb, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL, 8)
            row.AddStretchSpacer(1)
            v.Add(row, 0, wx.EXPAND)

        # Splitter: make preview pane user-resizable (drag sash).
        splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)
        self._splitter = splitter
        left = wx.Panel(splitter)
        right = wx.Panel(splitter)

        # Tree
        if _wxadv and hasattr(_wxadv, "TreeListCtrl"):
            self.tree = _wxadv.TreeListCtrl(
                left,
                style=wx.TR_DEFAULT_STYLE | wx.TR_HAS_BUTTONS | wx.TR_LINES_AT_ROOT | wx.TR_FULL_ROW_HIGHLIGHT | wx.TR_HIDE_ROOT,
            )
            self.tree.AppendColumn(self._p.tree_col1, width=520)
            self.tree.AppendColumn(self._p.tree_col2, width=820)
            self.tree.Bind(_wxadv.EVT_TREELIST_SELECTION_CHANGED, self._on_select_tree)
            self.tree.Bind(_wxadv.EVT_TREELIST_ITEM_ACTIVATED, self._on_item_activated)
            try:
                self.tree.Bind(_wxadv.EVT_TREELIST_ITEM_EXPANDING, self._on_item_expanding)
            except Exception:
                pass
            try:
                self.tree.Bind(_wxadv.EVT_TREELIST_COLUMN_DRAGGING, self._on_column_dragging)
                self.tree.Bind(_wxadv.EVT_TREELIST_COLUMN_END_DRAG, self._on_column_end_drag)
            except Exception:
                pass
        elif _wxdv and hasattr(_wxdv, "TreeListCtrl"):
            # IMPORTANT: Avoid wxPython's pure-Python TreeListCtrl wrappers (wx.lib.gizmos/AGW),
            # which create internal wx.Timer instances (see gdb: wx/lib/agw/hypertreelist.py).
            # Those timers are a known crash source in our workflow (use-after-free in wx timer dispatch).
            self.tree = _wxdv.TreeListCtrl(left, style=_wxdv.TL_DEFAULT_STYLE)
            self.tree.AppendColumn(self._p.tree_col1, width=520)
            self.tree.AppendColumn(self._p.tree_col2, width=820)
            self.tree.Bind(_wxdv.EVT_TREELIST_SELECTION_CHANGED, self._on_select_tree)
            self.tree.Bind(_wxdv.EVT_TREELIST_ITEM_ACTIVATED, self._on_item_activated)
            try:
                self.tree.Bind(_wxdv.EVT_TREELIST_ITEM_EXPANDING, self._on_item_expanding)
            except Exception:
                pass
        else:
            self.tree = wx.TreeCtrl(
                left,
                style=wx.TR_HAS_BUTTONS | wx.TR_LINES_AT_ROOT | wx.TR_HIDE_ROOT | wx.TR_FULL_ROW_HIGHLIGHT,
            )
            self.tree.Bind(wx.EVT_TREE_SEL_CHANGED, self._on_select_tree)
            self.tree.Bind(wx.EVT_TREE_ITEM_ACTIVATED, self._on_item_activated)
            self.tree.Bind(wx.EVT_TREE_ITEM_EXPANDING, self._on_item_expanding)

        # Status icon imagelist
        self._img = wx.ImageList(12, 12)
        self._img_green = self._img.Add(make_status_bitmap(wx.Colour(46, 160, 67)))
        self._img_red = self._img.Add(make_status_bitmap(wx.Colour(220, 53, 69)))
        self._img_yellow = self._img.Add(make_status_bitmap(wx.Colour(255, 193, 7)))
        self._img_gray = self._img.Add(make_status_bitmap(wx.Colour(160, 160, 160)))
        try:
            try:
                self.tree.SetImageList(self._img)  # type: ignore[attr-defined]
            except Exception:
                self.tree.AssignImageList(self._img)  # type: ignore[attr-defined]
        except Exception:
            pass

        left_s = wx.BoxSizer(wx.VERTICAL)
        left_s.Add(self.tree, 1, wx.ALL | wx.EXPAND, 8)
        left.SetSizer(left_s)

        # Preview pane (reusable widget)
        prev_box = wx.StaticBoxSizer(wx.VERTICAL, right, self._p.preview_box_title)
        self._preview = PreviewPanel(
            right,
            empty_label=self._p.empty_preview_label,
            show_choice=True,
            # Allow the right pane to shrink horizontally; keep a sane minimum height.
            min_bitmap_size=(-1, 320),
            # Symbols often include large whitespace; cropping to alpha makes them feel centered.
            crop_to_alpha=bool(getattr(self._p, "preview_kind_dir", "") == "sym"),
        )
        # Keep existing attribute names for minimal changes elsewhere.
        self.prev_choice = self._preview.choice
        self.prev_status = self._preview.status
        self.prev_updated = self._preview.updated
        self.prev_bmp = self._preview.bmp
        self.prev_choice.Bind(wx.EVT_CHOICE, lambda _e: (self._update_last_updated_label(), self._render_selected()))
        prev_box.Add(self._preview, 1, wx.EXPAND)
        right_s = wx.BoxSizer(wx.VERTICAL)
        right_s.Add(prev_box, 1, wx.ALL | wx.EXPAND, 8)
        right.SetSizer(right_s)

        splitter.SplitVertically(left, right, sashPosition=1080)
        splitter.SetMinimumPaneSize(320)
        try:
            splitter.SetSashGravity(0.75)
        except Exception:
            pass
        # Prefer keeping a generous preview width by default.
        # Avoid negative sash positions (can assert on some wx ports, notably macOS).
        def _set_default_sash() -> None:
            try:
                w = int(splitter.GetClientSize().GetWidth() or 0)
            except Exception:
                w = 0
            right_w = 520
            try:
                pos = max(320, w - right_w) if w else 1080
                splitter.SetSashPosition(pos)
            except Exception:
                pass

        wx.CallAfter(_set_default_sash)
        v.Add(splitter, 1, wx.EXPAND)

        # Bottom buttons
        btns = wx.BoxSizer(wx.HORIZONTAL)
        if self._p.create_button_label and self._p.on_create:
            self.create_btn = wx.Button(self, label=self._p.create_button_label)
            self.create_btn.Bind(wx.EVT_BUTTON, self._p.on_create)
            btns.Add(self.create_btn, 0, wx.ALL, 6)

        self.delete_btn = wx.Button(self, label="Delete")
        self.delete_btn.Bind(wx.EVT_BUTTON, self._on_delete_selected)
        btns.Add(self.delete_btn, 0, wx.ALL, 6)

        self.pick_btn: wx.Button | None = None
        if self._picker_mode:
            # Picker mode: hide destructive actions; provide an OK/Cancel flow.
            try:
                if getattr(self, "create_btn", None):
                    self.create_btn.Hide()  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                self.delete_btn.Hide()
            except Exception:
                pass
            self.pick_btn = wx.Button(self, label=f"Use selected {self._p.item_label}")
            self.pick_btn.Bind(wx.EVT_BUTTON, self._on_pick_ok)
            self.pick_btn.Enable(False)
            btns.Add(self.pick_btn, 0, wx.ALL, 6)

        btns.AddStretchSpacer(1)
        close = wx.Button(self, label="Cancel" if self._picker_mode else "Close")
        # If we are shown modally (as a child window of the main plugin), always end the modal
        # loop so callers can safely Destroy() us in a finally block.
        close.Bind(
            wx.EVT_BUTTON,
            lambda _e: (self.EndModal(wx.ID_CANCEL) if self.IsModal() else self.Close()),
        )
        btns.Add(close, 0, wx.ALL, 6)
        v.Add(btns, 0, wx.ALL | wx.EXPAND, 8)

        self.SetSizerAndFit(v)
        self.SetMinSize((1500, 850))
        self.SetSize((1700, 950))

        # Load initial
        self._reload_sources()
        self._update_status_strip()
        self._populate()
        self._start_index_watch_timer()
        self._refresh_asset_sets_async()

        # Prefetch everything experiment (enabled by default; can be disabled via env var)
        try:
            self._start_prefetch_all_descriptions_if_possible()
        except Exception:
            pass

        # If provider wants it, show symbol index/meta progress here (not in the preview).
        try:
            self._start_index_line_updater_if_needed()
        except Exception:
            pass

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

    def _start_index_line_updater_if_needed(self) -> None:
        """
        Periodically update the optional index line (no icon).

        Used by Symbol browser to display:
        - total symbols indexed (local + global)
        - description/meta load progress across libs
        """
        try:
            if not bool(getattr(self._p, "show_index_line", False)):
                return
        except Exception:
            return
        if getattr(self, "_index_line_repeater", None):
            return

        def tick() -> None:
            if self._closing:
                return
            line = getattr(self, "_index_line", None)
            if not line:
                return
            try:
                snap = self._index_snapshot() or {}
            except Exception:
                snap = {}

            loading = bool(snap.get("loading"))
            loaded = bool(snap.get("loaded"))
            err = str(snap.get("error") or "").strip()
            sym_files = dict(snap.get("sym_lib_files") or {})
            loaded_libs = snap.get("sym_meta_loaded_libs")
            if not isinstance(loaded_libs, set):
                loaded_libs = set()
            sym_refs = snap.get("symbols")
            if not isinstance(sym_refs, list):
                sym_refs = []
            sym_meta = snap.get("sym_meta")
            if not isinstance(sym_meta, dict):
                sym_meta = {}

            if err:
                try:
                    line.SetLabel(f"Symbol index failed: {err}")
                except Exception:
                    pass
                return
            if loading and not loaded:
                try:
                    line.SetLabel("Indexing symbols…")
                except Exception:
                    pass
                return

            # Count local vs global libs.
            lib_keys: set[str] = set([str(k or "").strip() for k in sym_files.keys()])
            lib_keys.discard("")
            total_libs = len(lib_keys)
            local_libs: set[str] = set()
            try:
                root = os.path.abspath(os.path.join(self._repo_path, "Symbols")) + os.sep
            except Exception:
                root = os.path.join(self._repo_path, "Symbols") + os.sep
            for lib, p in sym_files.items():
                nick = str(lib or "").strip()
                if not nick:
                    continue
                try:
                    ap = os.path.abspath(str(p or ""))
                except Exception:
                    ap = str(p or "")
                if ap.startswith(root):
                    local_libs.add(nick)
            total_local = len(local_libs)
            total_global = max(0, total_libs - total_local)
            meta_loaded_libs = set([str(x or "").strip() for x in loaded_libs]) & lib_keys
            local_meta_loaded = len(meta_loaded_libs & local_libs)
            global_meta_loaded = max(0, len(meta_loaded_libs) - local_meta_loaded)

            total_symbols = len(sym_refs)
            meta_symbols = len(sym_meta)
            try:
                sym_part = f"Symbols indexed: {total_symbols:,} ({total_libs} libs: {total_local} local, {total_global} global)"
                meta_part = f"Descriptions loaded: {meta_symbols:,}/{total_symbols:,}"
                lib_part = ""
                if total_libs > 0:
                    lib_part = (
                        f" (meta libs: {len(meta_loaded_libs)}/{total_libs}"
                        f"; local {local_meta_loaded}/{total_local}, global {global_meta_loaded}/{total_global})"
                    )
                line.SetLabel(f"{sym_part} — {meta_part}{lib_part}")
            except Exception:
                pass

            # Stop once meta for all libs is loaded (and index isn't loading).
            if total_libs > 0 and len(meta_loaded_libs) < total_libs:
                return
            try:
                rep = getattr(self, "_index_line_repeater", None)
                if rep:
                    rep.stop()
                self._index_line_repeater = None
            except Exception:
                pass

        self._index_line_repeater = UiRepeater(self, interval_ms=500, callback=tick)

    def _stop_timers_best_effort(self) -> None:
        """
        Stop all timers owned by this dialog.

        IMPORTANT: Your gdb backtraces show the native crash happens in wx timer dispatch
        when a timer tries to send an event to a freed handler. In our UI we often close
        modal dialogs via `dlg.Destroy()` without a user-driven close event, so we must
        stop timers explicitly and early.
        """
        try:
            if getattr(self, "_search_debouncer", None):
                self._search_debouncer.cancel()
        except Exception:
            pass
        try:
            if getattr(self, "_libcache_repeater", None):
                self._libcache_repeater.stop()
        except Exception:
            pass

    def Destroy(self) -> bool:  # type: ignore[override]
        # Ensure timers are stopped before wx schedules C++ deletion.
        try:
            self._closing = True
            self._descr_prefetch_all_cancel = True
            self._stop_timers_best_effort()
        except Exception:
            pass
        return super().Destroy()

    # ---------- activation ----------

    def _on_activate(self, evt: wx.ActivateEvent) -> None:
        try:
            if evt.GetActive() and not self._closing:
                self._repopulate_preserve_expansion()
                self._refresh_asset_sets_async()
                if (self.filter.GetValue() or "").strip():
                    self._schedule_search_recompute()
        except Exception:
            pass
        try:
            evt.Skip()
        except Exception:
            pass

    # ---------- git actions ----------

    def _on_sync(self, _evt: wx.CommandEvent) -> None:
        try:
            self._assets_label.SetLabel("Syncing library...")
        except Exception:
            pass
        try:
            self.sync_btn.Enable(False)
            self.fetch_btn.Enable(False)
        except Exception:
            pass

        # Legacy behavior: if there are local asset changes, publish (commit+push) them first.
        try:
            from ..git_ops import git_status_entries, paths_changed_under, suggest_assets_commit_message
            from ...config import Config
            from ..requests import prompt_commit_message
        except Exception:
            git_status_entries = None  # type: ignore[assignment]
            paths_changed_under = None  # type: ignore[assignment]
            suggest_assets_commit_message = None  # type: ignore[assignment]
            Config = None  # type: ignore[assignment]
            prompt_commit_message = None  # type: ignore[assignment]

        publish_cm: str | None = None
        br0 = "main"
        try:
            if Config is not None:
                br0 = (Config.load_effective(self._repo_path).github_base_branch or "main").strip() or "main"
        except Exception:
            br0 = "main"

        try:
            if git_status_entries and paths_changed_under:
                entries0 = git_status_entries(self._repo_path)
                assets0 = paths_changed_under(entries0, ["Symbols", "Footprints"])
                others0 = [p for _st, p in entries0 if p not in set(assets0)]
                if others0:
                    preview = "\n".join(f"- {p}" for p in others0[:20])
                    raise RuntimeError(
                        "Local changes exist outside Symbols/ and Footprints/.\n"
                        "Please commit or revert them manually before syncing.\n\n" + preview
                    )
                if assets0 and suggest_assets_commit_message and prompt_commit_message:
                    default = suggest_assets_commit_message(entries0)
                    cm = prompt_commit_message(self, default=default)
                    if not cm:
                        # Cancelled by user.
                        try:
                            self._assets_label.SetLabel(f"{self._p.kind_label}: sync cancelled")
                        except Exception:
                            pass
                        try:
                            self.sync_btn.Enable(True)
                            self.fetch_btn.Enable(True)
                        except Exception:
                            pass
                        return
                    publish_cm = str(cm)
        except Exception as e:
            try:
                self.sync_btn.Enable(True)
                self.fetch_btn.Enable(True)
            except Exception:
                pass
            try:
                self._assets_label.SetLabel(f"{self._p.kind_label}: sync blocked")
            except Exception:
                pass
            wx.MessageBox(str(e), "Sync blocked", wx.OK | wx.ICON_WARNING)
            return

        def work():
            from ..git_ops import git_status_entries, git_sync_ff_only, paths_changed_under
            from ..git_ops import git_commit_and_push_assets
            from ...config import Config

            try:
                br = (Config.load_effective(self._repo_path).github_base_branch or "main").strip() or "main"
            except Exception:
                br = "main"

            entries = git_status_entries(self._repo_path)
            assets = paths_changed_under(entries, ["Symbols", "Footprints"])
            others = [p for _st, p in entries if p not in set(assets)]
            if others:
                preview = "\n".join(f"- {p}" for p in others[:20])
                raise RuntimeError(
                    "Local changes exist outside Symbols/ and Footprints/.\n"
                    "Please commit or revert them manually before syncing.\n\n" + preview
                )

            pub_txt = ""
            if publish_cm:
                pub_txt = git_commit_and_push_assets(
                    self._repo_path,
                    commit_message=str(publish_cm),
                    prefixes=["Symbols", "Footprints"],
                    branch=br,
                )
            out = git_sync_ff_only(self._repo_path, branch=br)
            return {"branch": br, "out": out, "pub": pub_txt}

        def done(_res, err):
            if self._closing:
                return
            try:
                self.sync_btn.Enable(True)
                self.fetch_btn.Enable(True)
            except Exception:
                pass
            if err:
                try:
                    self._assets_label.SetLabel("Sync failed")
                except Exception:
                    pass
                wx.MessageBox(str(err), "Sync failed", wx.OK | wx.ICON_WARNING)
                return

            br = ""
            out = ""
            pub = ""
            try:
                br = str((_res or {}).get("branch") or "")
                out = str((_res or {}).get("out") or "")
                pub = str((_res or {}).get("pub") or "")
            except Exception:
                br = ""
                out = ""
                pub = ""

            # Restore normal status strip after sync.
            # Refresh this dialog's icon sets + tree.
            try:
                self._update_status_strip()
            except Exception:
                pass
            try:
                self._repopulate_preserve_expansion()
            except Exception:
                pass
            try:
                self._refresh_asset_sets_async()
            except Exception:
                pass

            # Also refresh main window status (if present).
            try:
                parent = self.GetParent()
                if parent and hasattr(parent, "_append_log"):
                    try:
                        if (pub or "").strip():
                            parent._append_log(str(pub).strip())  # type: ignore[misc]
                        msg = (out or "").strip() or "Sync completed."
                        parent._append_log(msg)  # type: ignore[misc]
                    except Exception:
                        pass
                if parent and hasattr(parent, "_refresh_sync_status"):
                    parent._refresh_sync_status()  # type: ignore[misc]
                if parent and hasattr(parent, "_refresh_assets_status"):
                    parent._refresh_assets_status()  # type: ignore[misc]
                if parent and hasattr(parent, "_reload_category_statuses"):
                    parent._reload_category_statuses()  # type: ignore[misc]
            except Exception:
                pass

        self._tasks.run(work, done)

    def _fetch_remote_async_notify_parent(self) -> None:
        try:
            self._assets_label.SetLabel("Fetching remote...")
        except Exception:
            pass
        try:
            self.fetch_btn.Enable(False)
            self.sync_btn.Enable(False)
        except Exception:
            pass

        def work():
            from ..git_ops import run_git
            from ...config import Config

            try:
                br = (Config.load_effective(self._repo_path).github_base_branch or "main").strip() or "main"
            except Exception:
                br = "main"
            run_git(["git", "fetch", "origin", br, "--quiet"], cwd=self._repo_path)
            return {"branch": br}

        def done(_res, _err):
            if self._closing:
                return
            try:
                self.fetch_btn.Enable(True)
                self.sync_btn.Enable(True)
            except Exception:
                pass
            if _err:
                try:
                    self._assets_label.SetLabel("Fetch failed")
                except Exception:
                    pass
                try:
                    wx.MessageBox(str(_err), "Fetch remote failed", wx.OK | wx.ICON_WARNING)
                except Exception:
                    pass
                return
            # Restore normal status strip after fetch.
            try:
                self._update_status_strip()
            except Exception:
                pass
            try:
                self._refresh_asset_sets_async()
            except Exception:
                pass
            try:
                parent = self.GetParent()
                if parent and hasattr(parent, "_append_log"):
                    try:
                        parent._append_log(f"Fetched origin/{br} from remote." if br else "Fetched remote.")  # type: ignore[misc]
                    except Exception:
                        pass
                if parent and hasattr(parent, "_refresh_assets_status"):
                    parent._refresh_assets_status()  # type: ignore[misc]
                if parent and hasattr(parent, "_refresh_remote_cat_updated_times_async"):
                    parent._refresh_remote_cat_updated_times_async()  # type: ignore[misc]
                if parent and hasattr(parent, "_refresh_sync_status"):
                    parent._refresh_sync_status()  # type: ignore[misc]
                if parent and hasattr(parent, "_reload_category_statuses"):
                    parent._reload_category_statuses()  # type: ignore[misc]
            except Exception:
                pass

        self._tasks.run(work, done)

    # ---------- sources + status ----------

    def _index_snapshot(self) -> dict[str, Any]:
        try:
            return self._p.index.snapshot(self._repo_path)
        except Exception:
            return {}

    def _reload_sources(self) -> None:
        """
        Merge local scan with indexed libs, avoiding stale shadowed entries.
        """
        snap = self._index_snapshot()
        try:
            local_refs = self._p.list_local_refs(self._repo_path)
        except Exception:
            local_refs = []
        local_set = set(local_refs)
        local_libs = set([r.split(":", 1)[0] for r in local_refs if ":" in r])
        snap_refs = list(snap.get(self._p.snapshot_key_items) or [])
        snap_keep: list[str] = []
        for r in snap_refs:
            if ":" in r:
                lib = r.split(":", 1)[0]
                if lib in local_libs and r not in local_set:
                    continue
            snap_keep.append(r)
        refs = sorted(set(snap_keep) | local_set)

        self._groups = dict(self._p.group_variants(refs))
        # Reverse map for quick "variant -> base" lookups (picker options, etc.)
        v2b: dict[str, str] = {}
        for base, vs in (self._groups or {}).items():
            bb = str(base or "").strip()
            if not bb:
                continue
            for v in list(vs or []):
                vv = str(v or "").strip()
                if vv and vv not in v2b:
                    v2b[vv] = bb
        self._variant_to_base = v2b
        self._bases_all = sorted(self._groups.keys())
        self._bases_lc = [b.lower() for b in self._bases_all]
        self._bases_lib = [b.split(":", 1)[0] if ":" in b else "Other" for b in self._bases_all]

        self._lib_to_bases = {}
        self._lib_nodes = {}
        self._lib_populated = set()
        self._base_to_item = {}

        _dbg(f"{self._p.kind_label}: reload_sources refs={len(refs)} bases={len(self._bases_all)} local={len(local_refs)} snap={len(snap_refs)}")

    def _start_index_watch_timer(self) -> None:
        """
        Show a message while project/global libs are indexing, and refresh once loaded.
        """
        st = self._index_snapshot()
        loading = bool(st.get("loading"))
        err = str(st.get("error") or "")
        if loading:
            try:
                self._assets_label.SetLabel(f"{self._p.kind_label}: indexing libraries (project + global)…")
            except Exception:
                pass
        if err and not loading:
            try:
                self._assets_label.SetLabel(f"{self._p.kind_label}: library index failed: {err}")
            except Exception:
                pass
            return
        if not loading:
            return

        self._libcache_repeater = UiRepeater(self, interval_ms=600, callback=lambda: self._on_index_timer(None))

    def _on_index_timer(self, _evt: wx.TimerEvent) -> None:
        if self._closing:
            try:
                if getattr(self, "_libcache_repeater", None):
                    self._libcache_repeater.stop()
            except Exception:
                pass
            return
        st = self._index_snapshot()
        loading = bool(st.get("loading"))
        err = str(st.get("error") or "")
        if loading:
            return
        try:
            if getattr(self, "_libcache_repeater", None):
                self._libcache_repeater.stop()
        except Exception:
            pass
        if err:
            try:
                self._assets_label.SetLabel(f"{self._p.kind_label}: library index failed: {err}")
            except Exception:
                pass
            return
        try:
            self._repopulate_preserve_expansion()
            if (self.filter.GetValue() or "").strip():
                self._schedule_search_recompute()
            self._start_prefetch_all_descriptions_if_possible()
        except Exception:
            pass

    def _refresh_asset_sets_async(self) -> None:
        def work():
            return asset_change_sets(self._repo_path)

        def done(res, err):
            if self._closing:
                return
            if err or not res:
                return
            self._asset_local, self._asset_remote, self._asset_remote_known = res
            self._update_status_strip()
            self._repopulate_preserve_expansion()

        self._tasks.run(work, done)

    def _update_status_strip(self) -> None:
        try:
            local = local_summary_scoped(self._repo_path, self._p.scope_dirs, self._p.scope_key)
        except Exception:
            local = None
        age = git_fetch_head_age_seconds(self._repo_path)
        stale = (age is None) or (age > fetch_stale_threshold_seconds(self._repo_path))
        if stale:
            suffix = f" (last fetch {format_age_minutes(age)})" if age is not None else ""
            msg = f"{(local.msg if local else f'Local {self._p.scope_key}: unavailable')} — Remote {self._p.scope_key}: unknown / stale{suffix}"
            bmp = make_status_bitmap(wx.Colour(255, 193, 7) if (local.count if local else 0) else wx.Colour(160, 160, 160))
            # Keep icon sets consistent with what we can actually know.
            try:
                self._asset_local = set(local.files) if local else set()
            except Exception:
                self._asset_local = set()
            self._asset_remote = set()
            self._asset_remote_known = False
        else:
            remote = remote_summary_scoped(self._repo_path, self._p.scope_dirs, self._p.scope_key)
            msg = f"{(local.msg if local else f'Local {self._p.scope_key}: unavailable')} — {remote.msg}"
            if remote.files:
                bmp = make_status_bitmap(wx.Colour(220, 53, 69))
            elif (local.count if local else 0):
                bmp = make_status_bitmap(wx.Colour(255, 193, 7))
            else:
                bmp = make_status_bitmap(wx.Colour(46, 160, 67))
            # Make per-lib/per-item icons use the same underlying diff as the status strip.
            try:
                self._asset_local = set(local.files) if local else set()
            except Exception:
                self._asset_local = set()
            try:
                self._asset_remote = {p for _st, p in (remote.files or []) if p}
            except Exception:
                self._asset_remote = set()
            self._asset_remote_known = True
        self._assets_icon.SetBitmap(bmp)
        self._assets_label.SetLabel(str(msg))
        try:
            self._assets_label.Wrap(max(200, self.GetClientSize().width - 80))
        except Exception:
            pass
        # Tree icons are refreshed by callers after updating the underlying asset sets.

    # ---------- tree helpers ----------

    def _tree_kind(self) -> str:
        if _wxadv and hasattr(_wxadv, "TreeListCtrl") and isinstance(self.tree, _wxadv.TreeListCtrl):
            return "adv"
        if _wxdv and hasattr(_wxdv, "TreeListCtrl") and isinstance(self.tree, _wxdv.TreeListCtrl):
            return "dv"
        if _wxgizmos and hasattr(_wxgizmos, "TreeListCtrl") and isinstance(self.tree, _wxgizmos.TreeListCtrl):
            return "gizmos"
        return "tree"

    def _delete_children_best_effort(self, item) -> None:
        """
        Delete all children for an item across different tree implementations.

        Note: wx.dataview.TreeListCtrl does not provide DeleteChildren(), so we must
        iterate and DeleteItem() manually.
        """
        kind = self._tree_kind()
        if kind in ("adv", "gizmos", "tree"):
            try:
                self.tree.DeleteChildren(item)  # type: ignore[attr-defined]
                return
            except Exception:
                return
        if kind == "dv":
            try:
                child = self.tree.GetFirstChild(item)  # type: ignore[attr-defined]
            except Exception:
                return
            # Delete siblings one by one.
            while True:
                try:
                    ok = child.IsOk()
                except Exception:
                    ok = bool(child)
                if not ok:
                    break
                try:
                    nxt = self.tree.GetNextSibling(child)  # type: ignore[attr-defined]
                except Exception:
                    nxt = None
                try:
                    self.tree.DeleteItem(child)  # type: ignore[attr-defined]
                except Exception:
                    pass
                child = nxt

    def _ensure_lib_index(self) -> None:
        if self._lib_to_bases:
            return
        libs: dict[str, list[str]] = {}
        for base in self._bases_all:
            lib = base.split(":", 1)[0] if ":" in base else "Other"
            libs.setdefault(lib, []).append(base)
        for k in list(libs.keys()):
            libs[k] = sorted(libs[k])
        self._lib_to_bases = dict(sorted(libs.items(), key=lambda kv: kv[0].lower()))

    def _clear_tree(self) -> object:
        kind = self._tree_kind()
        self._lib_nodes = {}
        self._lib_populated = set()
        self._base_to_item = {}
        if kind == "adv":
            self.tree.DeleteAllItems()
            try:
                return self.tree.AddRoot("")
            except Exception:
                return self.tree.GetRootItem()
        if kind == "dv":
            try:
                self.tree.DeleteAllItems()
            except Exception:
                pass
            try:
                return self.tree.GetRootItem()
            except Exception:
                # Best effort: some builds expose RootItem as a property.
                try:
                    return self.tree.RootItem  # type: ignore[attr-defined]
                except Exception:
                    return object()
        if kind == "gizmos":
            try:
                self.tree.DeleteAllItems()
            except Exception:
                pass
            return self.tree.AddRoot("")
        try:
            self.tree.DeleteAllItems()
        except Exception:
            try:
                self.tree.DeleteChildren(self.tree.GetRootItem())
            except Exception:
                pass
        return self.tree.AddRoot("")

    def _append_lib_node(self, root, lib: str, count: int) -> object:
        kind = self._tree_kind()
        label = f"{lib} ({count})" if count >= 0 else lib
        if kind in ("adv", "dv"):
            it = self.tree.AppendItem(root, lib)
            try:
                self.tree.SetItemText(it, 1, f"({count})")
            except Exception:
                pass
            return it
        if kind == "gizmos":
            it = self.tree.AppendItem(root, label)
            return it
        it = self.tree.AppendItem(root, label)
        return it

    def _add_placeholder_child(self, parent) -> None:
        try:
            self.tree.AppendItem(parent, "…")  # type: ignore[attr-defined]
        except Exception:
            pass

    def _set_item_icon(self, item, idx: int | None) -> None:
        if idx is None:
            return
        if self._tree_kind() == "dv":
            try:
                # wx.dataview.TreeListCtrl expects closed/opened image ids.
                self.tree.SetItemImage(item, idx, idx)  # type: ignore[attr-defined]
            except Exception:
                pass
            return
        try:
            self.tree.SetItemImage(item, idx)  # type: ignore[attr-defined]
        except Exception:
            try:
                self.tree.SetItemImage(item, idx, wx.TreeItemIcon_Normal)  # type: ignore[attr-defined]
            except Exception:
                pass

    def _icon_for_ref(self, base: str) -> int | None:
        if ":" not in (base or ""):
            return None
        # For variants: consider any ref that maps to a repo-relative path and is in
        # local/remote change sets (do not require the file to exist on disk; we still
        # want to show remote deletions).
        variants = list(self._groups.get(base) or []) or [base]
        any_exists = False
        has_remote = False
        has_local = False
        for v in variants:
            rel = self._p.rel_path_for_ref(self._repo_path, v)
            if not rel:
                continue
            any_exists = True
            if rel in self._asset_remote:
                has_remote = True
            if rel in self._asset_local:
                has_local = True
        if not any_exists:
            return None
        if has_remote:
            return self._img_red
        if has_local:
            return self._img_yellow
        if not self._asset_remote_known:
            return self._img_gray
        return self._img_green

    def _local_deleted_variant_count(self, base: str) -> int:
        """
        For footprints, show a DELETED marker when some variants are deleted locally.
        """
        try:
            if str(getattr(self._p, "scope_key", "") or "") != "footprints":
                return 0
        except Exception:
            return 0
        if ":" not in (base or ""):
            return 0
        variants = list(self._groups.get(base) or []) or [base]
        n = 0
        for v in variants:
            rel = self._p.rel_path_for_ref(self._repo_path, v)
            if not rel:
                continue
            if rel not in (self._asset_local or set()):
                continue
            abs_p = os.path.join(self._repo_path, rel)
            if not os.path.exists(abs_p):
                n += 1
        return int(n)

    def _icon_for_lib(self, lib: str) -> int | None:
        """
        Aggregate icon for a library node based on local/remote change sets.
        Only shown for repo-local libraries (provider supplies a rel prefix/file).
        """
        lib = (lib or "").strip()
        if not lib:
            return None
        try:
            relp = (self._p.rel_prefix_for_lib(self._repo_path, lib) or "").strip()
        except Exception:
            relp = ""
        if not relp:
            return None

        has_remote = False
        has_local = False

        try:
            if relp.endswith("/"):
                for p in self._asset_remote:
                    if p.startswith(relp):
                        has_remote = True
                        break
                for p in self._asset_local:
                    if p.startswith(relp):
                        has_local = True
                        break
            else:
                has_remote = relp in self._asset_remote
                has_local = relp in self._asset_local
        except Exception:
            pass

        if has_remote:
            return self._img_red
        if has_local:
            return self._img_yellow
        if not self._asset_remote_known:
            return self._img_gray
        return self._img_green

    def _append_child(self, parent, base: str, descr: str) -> object | None:
        kind = self._tree_kind()
        # Add a DELETED marker for locally deleted footprint variants.
        try:
            if descr and "DELETED" in descr:
                dcount = 0
            else:
                dcount = int(self._local_deleted_variant_count(base) or 0)
            if dcount > 0:
                tag = "DELETED" if dcount == 1 else f"DELETED ({dcount})"
                descr = (f"{descr} — {tag}" if (descr or "").strip() else tag)
        except Exception:
            pass
        if kind in ("adv", "dv"):
            it = self.tree.AppendItem(parent, base)
            try:
                self.tree.SetItemText(it, 1, descr or "")
            except Exception:
                pass
            self._set_item_icon(it, self._icon_for_ref(base))
            return it
        if kind == "gizmos":
            it = self.tree.AppendItem(parent, base)
            try:
                self.tree.SetItemText(it, descr or "", 1)
            except Exception:
                pass
            self._set_item_icon(it, self._icon_for_ref(base))
            return it
        label = base if not descr else f"{base} — {descr}"
        it = self.tree.AppendItem(parent, label)
        self._set_item_icon(it, self._icon_for_ref(base))
        return it

    def _expanded_libs(self) -> set[str]:
        expanded: set[str] = set()
        try:
            for lib, item in (self._lib_nodes or {}).items():
                try:
                    if self.tree.IsExpanded(item):  # type: ignore[attr-defined]
                        expanded.add(lib)
                except Exception:
                    continue
        except Exception:
            pass
        return expanded

    def _restore_expanded_libs(self, libs: set[str]) -> None:
        if not libs:
            return
        for lib in libs:
            item = (self._lib_nodes or {}).get(lib)
            if not item:
                continue
            try:
                self.tree.Expand(item)  # type: ignore[attr-defined]
            except Exception:
                pass

    def _repopulate_preserve_expansion(self) -> None:
        expanded = self._expanded_libs()
        try:
            self._reload_sources()
        except Exception:
            pass
        try:
            self._populate()
        except Exception:
            pass
        try:
            self._restore_expanded_libs(expanded)
        except Exception:
            pass

    # ---------- populate + search ----------

    def _populate(self) -> None:
        q = (self._search_q or "").strip().lower()
        self._ensure_lib_index()
        root = self._clear_tree()

        if not q:
            for lib, bases in self._lib_to_bases.items():
                it = self._append_lib_node(root, lib, len(bases))
                self._lib_nodes[lib] = it
                self._set_item_icon(it, self._icon_for_lib(lib))
                self._add_placeholder_child(it)
            try:
                self.tree.Expand(root)  # type: ignore[attr-defined]
            except Exception:
                pass
            return

        hits_by_lib = (self._search_result or {}) if (self._search_result_q == q) else {}
        if not hits_by_lib:
            try:
                self.tree.Expand(root)  # type: ignore[attr-defined]
            except Exception:
                pass
            return

        def _lib_sort_key(lib: str) -> tuple[float, float, str]:
            best = float((self._search_lib_best or {}).get(lib, 0.0))
            is_local = False
            try:
                is_local = self._p.lib_is_repo_local(self._repo_path, lib)
            except Exception:
                is_local = False
            return (0.0 if is_local else 1.0, -best, lib.lower())

        for lib in sorted(hits_by_lib.keys(), key=_lib_sort_key):
            hits = hits_by_lib.get(lib) or []
            if not hits:
                continue
            it = self._append_lib_node(root, lib, len(hits))
            self._lib_nodes[lib] = it
            self._set_item_icon(it, self._icon_for_lib(lib))
            self._add_placeholder_child(it)
        try:
            self.tree.Expand(root)  # type: ignore[attr-defined]
        except Exception:
            pass

    def _on_filter(self, _evt: wx.CommandEvent) -> None:
        if self._closing:
            return
        self._search_pending_q = (self.filter.GetValue() or "").strip().lower()
        if self._search_pending_q == self._search_pending_q_last:
            return
        self._search_pending_q_last = self._search_pending_q
        try:
            self._search_debouncer.trigger(delay_ms=500)
        except Exception:
            self._on_search_timer(None)

    def _schedule_search_recompute(self) -> None:
        if self._closing:
            return
        try:
            self._search_pending_q = (self.filter.GetValue() or "").strip().lower()
        except Exception:
            self._search_pending_q = ""
        self._search_pending_q_last = self._search_pending_q
        try:
            self._search_debouncer.trigger(delay_ms=1)
        except Exception:
            pass

    def _on_search_timer(self, _evt: wx.TimerEvent) -> None:
        q = (self._search_pending_q or "").strip().lower()
        if not q:
            self._search_q = ""
            self._search_result = None
            self._search_result_q = ""
            self._search_lib_best = {}
            self._search_inflight = False
            self._populate()
            return

        self._search_gen += 1
        gen = self._search_gen
        self._search_q = q
        self._search_inflight = True
        try:
            self.prev_status.SetLabel("Searching…")
        except Exception:
            pass

        bases_all = list(self._bases_all)
        bases_lc = list(self._bases_lc)
        bases_lib = list(self._bases_lib)
        descr_cache = dict(self._descr_cache)

        def work():
            t0 = time.perf_counter()
            res = search_hits_by_lib(
                q=q,
                bases_all=bases_all,
                bases_lc=bases_lc,
                bases_lib=bases_lib,
                descr_cache=descr_cache,
                max_total=800,
            )
            dt_ms = (time.perf_counter() - t0) * 1000.0
            qq, hits, truncated, shown, lib_best = res
            return (_SearchResult(qq, hits, bool(truncated), int(shown), dict(lib_best or {})), dt_ms, search_backend_info())

        def done(res, err):
            if self._closing:
                return
            if err or not res:
                self._search_inflight = False
                return
            if gen != self._search_gen:
                return
            try:
                inner, dt_ms, backend = res
            except Exception:
                return
            if inner.q != self._search_q:
                return
            self._search_result = inner.hits_by_lib or {}
            self._search_result_q = inner.q
            self._search_lib_best = dict(inner.lib_best or {})
            self._search_inflight = False
            try:
                dt_str = f"{dt_ms:.0f}" if (dt_ms is not None) else "?"
                self._assets_label.SetLabel(f"{self._p.kind_label}: search ({backend}) — {dt_str} ms")
            except Exception:
                pass
            self._populate()
            try:
                if not (self.prev_choice.GetStringSelection() or "").strip():
                    self.prev_status.SetLabel(self._p.empty_preview_label)
            except Exception:
                pass

        self._tasks.run(work, done)

    # ---------- tree events ----------

    def _on_item_activated(self, evt) -> None:
        try:
            item = evt.GetItem()
        except Exception:
            item = None
        if not item:
            return
        kind = self._tree_kind()
        if kind in ("adv", "dv"):
            try:
                label = (self.tree.GetItemText(item, 0) or "").strip()
            except Exception:
                label = ""
        else:
            try:
                label = (self.tree.GetItemText(item) or "").strip()
            except Exception:
                label = ""
        if ":" not in label:
            try:
                if self.tree.IsExpanded(item):  # type: ignore[attr-defined]
                    self.tree.Collapse(item)  # type: ignore[attr-defined]
                else:
                    self.tree.Expand(item)  # type: ignore[attr-defined]
            except Exception:
                pass
            return

        # In picker mode, double-click selects the current ref.
        if self._picker_mode:
            try:
                self._pick_current_and_close()
            except Exception:
                pass
            return

    def _on_item_expanding(self, evt) -> None:
        try:
            item = evt.GetItem()
        except Exception:
            item = None
        if not item:
            return
        kind = self._tree_kind()
        if kind in ("adv", "dv"):
            try:
                lib = (self.tree.GetItemText(item, 0) or "").strip()
            except Exception:
                lib = ""
        else:
            try:
                lib = (self.tree.GetItemText(item) or "").strip()
            except Exception:
                lib = ""
            if lib.endswith(")") and "(" in lib:
                lib = lib.rsplit("(", 1)[0].strip()

        if not lib or lib in self._lib_populated:
            return

        q = (self._search_q or "").strip().lower()
        if q and (self._search_result_q == q) and self._search_result:
            bases = list((self._search_result.get(lib) or []))
        else:
            bases = self._lib_to_bases.get(lib) or []

        self._delete_children_best_effort(item)
        self._base_to_item = {}
        max_children = 600 if q else 4000
        for b in bases[:max_children]:
            it2 = self._append_child(item, b, descr=self._descr_cache.get(b, ""))
            if it2:
                self._base_to_item[b] = it2
        self._lib_populated.add(lib)
        self._start_load_descriptions(bases[:max_children])

    def _repr_ref_for_base(self, base: str) -> str:
        """
        For providers that group variants (footprints), base keys might not correspond to a real file.
        Use a stable representative ref (first variant) for description extraction, while caching the
        result under the base key.
        """
        try:
            vs = self._groups.get(base) or []
            if vs:
                return str(vs[0] or base)
        except Exception:
            pass
        return base

    def _start_load_descriptions(self, bases: list[str]) -> None:
        bases = [b for b in bases if b and b not in self._descr_cache]
        if not bases:
            return
        if len(bases) > 1200:
            bases = bases[:1200]

        def work():
            out: dict[str, str] = {}
            for b in bases:
                try:
                    rr = self._repr_ref_for_base(b)
                    out[b] = (self._p.extract_description_for_ref(self._repo_path, rr) or "").replace("\n", " ").strip()[:180]
                except Exception:
                    out[b] = ""
            return out

        def done(res, err):
            if self._closing:
                return
            if err or not res:
                return
            mp: dict[str, str] = res or {}
            for b, d in mp.items():
                self._descr_cache[b] = d
            kind = self._tree_kind()
            if kind in ("adv", "dv"):
                for b, it in list(self._base_to_item.items()):
                    if b in mp:
                        if self._col_dragging:
                            self._pending_descr_updates[b] = mp[b] or ""
                            continue
                        try:
                            self.tree.SetItemText(it, 1, mp[b] or "")
                        except Exception:
                            pass
            elif kind == "gizmos":
                for b, it in list(self._base_to_item.items()):
                    if b in mp:
                        if self._col_dragging:
                            self._pending_descr_updates[b] = mp[b] or ""
                            continue
                        try:
                            self.tree.SetItemText(it, mp[b] or "", 1)
                        except Exception:
                            pass

        self._tasks.run(work, done)

    def _on_column_dragging(self, _evt) -> None:
        self._col_dragging = True

    def _on_column_end_drag(self, _evt) -> None:
        self._col_dragging = False
        if not self._pending_descr_updates:
            return
        kind = self._tree_kind()
        mp = dict(self._pending_descr_updates)
        self._pending_descr_updates.clear()
        if kind in ("adv", "dv"):
            for b, it in list(self._base_to_item.items()):
                if b in mp:
                    try:
                        self.tree.SetItemText(it, 1, mp[b] or "")
                    except Exception:
                        pass
        elif kind == "gizmos":
            for b, it in list(self._base_to_item.items()):
                if b in mp:
                    try:
                        self.tree.SetItemText(it, mp[b] or "", 1)
                    except Exception:
                        pass

    # ---------- selection + preview ----------

    def _selected_base(self) -> str:
        kind = self._tree_kind()
        if kind in ("adv", "dv"):
            item = self.tree.GetSelection()
            try:
                ok = item.IsOk()
            except Exception:
                ok = bool(item)
            if not ok:
                return ""
            try:
                base = (self.tree.GetItemText(item, 0) or "").strip()
            except Exception:
                base = ""
            return base if ":" in base else ""
        item = self.tree.GetSelection()
        if not item or not item.IsOk():
            return ""
        label = (self.tree.GetItemText(item) or "").strip()
        if ":" not in label:
            return ""
        return label.split("—", 1)[0].strip()

    def _on_select_tree(self, _evt) -> None:
        base = self._selected_base()
        if not base:
            return
        variants = self._groups.get(base) or [base]
        self.prev_choice.Clear()
        for vref in variants:
            self.prev_choice.Append(vref)
        self.prev_choice.Enable(True)
        self.prev_choice.SetSelection(0)
        # Hide the variants dropdown if there is only one.
        try:
            self.prev_choice.Show(len(variants) > 1)
            self.Layout()
        except Exception:
            pass
        self._update_last_updated_label()
        self._render_selected()
        if self._picker_mode and getattr(self, "pick_btn", None):
            try:
                self.pick_btn.Enable(bool(self._current_pick_ref()))  # type: ignore[union-attr]
            except Exception:
                pass

    def _update_last_updated_label(self) -> None:
        try:
            ref = (self.prev_choice.GetStringSelection() or "").strip()
        except Exception:
            ref = ""
        if not ref:
            self.prev_updated.SetLabel("")
            return

        age = git_fetch_head_age_seconds(self._repo_path)
        stale = (age is None) or (age > fetch_stale_threshold_seconds(self._repo_path))
        if stale:
            suffix = f" (last fetch {format_age_minutes(age)})" if age is not None else ""
            self.prev_updated.SetLabel("Last updated (remote): unknown / stale — fetch remote" + suffix)
            return

        rel = self._p.rel_path_for_ref(self._repo_path, ref)
        if not rel:
            self.prev_updated.SetLabel("")
            return

        if rel in self._updated_cache:
            ts = self._updated_cache.get(rel)
            if ts:
                import datetime as _dt

                self.prev_updated.SetLabel(f"Last updated (remote): {_dt.datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M')}")
            return

        self.prev_updated.SetLabel("Last updated (remote): loading…")

        def work():
            return git_last_updated_epoch(self._repo_path, rel, ref=None)

        def done(ts, err):
            if self._closing:
                return
            if err or not ts:
                self.prev_updated.SetLabel("Last updated (remote): unavailable")
                return
            self._updated_cache[rel] = int(ts)
            import datetime as _dt

            self.prev_updated.SetLabel(f"Last updated (remote): {_dt.datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M')}")

        self._tasks.run(work, done)

    def _render_selected(self) -> None:
        if self.prev_choice.GetSelection() == wx.NOT_FOUND:
            return
        ref = (self.prev_choice.GetStringSelection() or "").strip()
        if not ref:
            return
        mtime = "0"
        try:
            mtime = self._p.source_mtime_for_ref(self._repo_path, ref)
        except Exception:
            mtime = "0"
        self._preview.render_cached_svg_async(
            kind_dir=self._p.preview_kind_dir,
            cache_key_prefix=self._p.preview_cache_key_prefix,
            ref=ref,
            source_mtime=mtime,
            render_svg=lambda r, p: self._p.render_svg(self._repo_path, r, p),
            quality_scale=2.5,
        )

    def get_picked_ref(self) -> str:
        return str(getattr(self, "_picked_ref", "") or "").strip()

    def get_picked_refs(self) -> list[str]:
        """
        Picker-mode helper: return all selected refs (may be >1 when picker options expand variants).
        """
        refs = list(getattr(self, "_picked_refs", []) or [])
        out: list[str] = []
        seen: set[str] = set()
        for r in refs:
            rr = str(r or "").strip()
            if not rr or rr in seen:
                continue
            seen.add(rr)
            out.append(rr)
        if out:
            return out
        one = self.get_picked_ref()
        return [one] if one else []

    def _current_pick_ref(self) -> str:
        # Prefer currently selected variant in preview dropdown (real file).
        try:
            ref = (self.prev_choice.GetStringSelection() or "").strip()
            if ref and ":" in ref:
                return ref
        except Exception:
            pass
        # Fallback to selected base.
        try:
            base = (self._selected_base() or "").strip()
            return base if ":" in base else ""
        except Exception:
            return ""

    def _on_pick_ok(self, _evt: wx.CommandEvent) -> None:
        self._pick_current_and_close()

    def _pick_current_and_close(self) -> None:
        ref = self._current_pick_ref()
        if not ref:
            wx.MessageBox(f"Select a {self._p.item_label} first.", self._p.kind_title, wx.OK | wx.ICON_INFORMATION)
            return
        refs: list[str] = [ref]
        # Apply picker options (if any).
        try:
            if self._picker_mode and self._picker_all_densities_cb and bool(self._picker_all_densities_cb.GetValue()):
                base = ref
                if base not in (self._groups or {}):
                    base = (self._variant_to_base or {}).get(ref) or ref
                if base in (self._groups or {}):
                    refs = list(self._groups.get(base) or [ref])
        except Exception:
            refs = [ref]
        self._picked_refs = [str(r or "").strip() for r in (refs or []) if str(r or "").strip()]
        self._picked_ref = (self._picked_refs[0] if self._picked_refs else ref)
        try:
            if self.IsModal():
                # Stop timers before we hand control back to the caller which will `Destroy()`.
                self._closing = True
                self._descr_prefetch_all_cancel = True
                self._stop_timers_best_effort()
                self.EndModal(wx.ID_OK)
                return
        except Exception:
            pass
        try:
            self.Close()
        except Exception:
            pass

    # ---------- delete ----------

    def _on_delete_selected(self, _evt: wx.CommandEvent) -> None:
        expanded = self._expanded_libs()
        base = (self._selected_base() or "").strip()
        if not base:
            base = (self.prev_choice.GetStringSelection() or "").strip()
        if not base or ":" not in base:
            wx.MessageBox(f"Select a {self._p.item_label} first.", f"Delete {self._p.item_label}", wx.OK | wx.ICON_INFORMATION)
            return

        ok, reason = self._p.can_delete_ref(self._repo_path, base)
        if not ok:
            wx.MessageBox(reason, f"Delete {self._p.item_label}", wx.OK | wx.ICON_WARNING)
            return

        variants = list(self._groups.get(base) or [base])
        lines = "\n".join([f"- {ref}" for ref in variants][:50])
        extra = f"\n... and {len(variants) - 50} more" if len(variants) > 50 else ""
        msg = f"Delete this {self._p.item_label} from your local library?\n\n{lines}{extra}\n\nContinue?"
        if wx.MessageBox(msg, f"Delete {self._p.item_label}", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING) != wx.YES:
            return

        failed = self._p.delete_ref_and_variants(self._repo_path, base, variants)
        if failed:
            wx.MessageBox(
                "Some items could not be deleted:\n\n" + "\n".join(failed[:50]),
                f"Delete {self._p.item_label}",
                wx.OK | wx.ICON_ERROR,
            )

        try:
            self._reload_sources()
        except Exception:
            pass
        try:
            self.prev_choice.Clear()
            self.prev_choice.Enable(False)
            self.prev_choice.Show(False)
            self.prev_status.SetLabel(self._p.empty_preview_label)
            self.prev_updated.SetLabel("")
            self.prev_bmp.SetBitmap(wx.NullBitmap)
            self.prev_bmp.Refresh()
        except Exception:
            pass
        try:
            self._populate()
            self._restore_expanded_libs(expanded)
        except Exception:
            pass

        # Refresh local/remote status icons after local deletion.
        try:
            self._update_status_strip()
        except Exception:
            pass
        self._refresh_asset_sets_async()

        # Notify main window (if present) to refresh; do not auto-fetch here.
        try:
            parent = self.GetParent()
            if parent and hasattr(parent, "_refresh_assets_status"):
                parent._refresh_assets_status()  # type: ignore[misc]
            if parent and hasattr(parent, "_refresh_sync_status"):
                parent._refresh_sync_status()  # type: ignore[misc]
            if parent and hasattr(parent, "_reload_category_statuses"):
                parent._reload_category_statuses()  # type: ignore[misc]
        except Exception:
            pass

    # ---------- description prefetch (all) ----------

    def _start_prefetch_all_descriptions_if_possible(self) -> None:
        """
        Prefetch descriptions for ALL bases in background (repo-local first).

        Controlled by env var:
          KICAD_LIBRARY_MANAGER_PREFETCH_ALL_DESCR=0  -> disable
        """
        if self._closing or self._descr_prefetch_all_cancel:
            return
        if self._descr_prefetch_all_started or self._descr_prefetch_all_done:
            return
        try:
            if (
                str(os.environ.get("KICAD_LIBRARY_MANAGER_PREFETCH_ALL_DESCR", "1"))
                .strip()
                .lower()
                in ("0", "false", "no", "off")
            ):
                return
        except Exception:
            pass

        st = self._index_snapshot()
        if bool(st.get("loading")):
            return

        bases_all = list(getattr(self, "_bases_all", []) or [])
        if not bases_all:
            return

        # Build a full TODO list, repo-local libs first.
        local_libs: set[str] = set()
        try:
            libs = sorted({b.split(":", 1)[0] for b in bases_all if ":" in b})
            for lib in libs:
                if self._p.lib_is_repo_local(self._repo_path, lib):
                    local_libs.add(lib)
        except Exception:
            local_libs = set()

        todo_local: list[str] = []
        todo_global: list[str] = []
        for b in bases_all:
            if ":" not in b:
                continue
            if b in self._descr_cache:
                continue
            lib = b.split(":", 1)[0]
            if lib in local_libs:
                todo_local.append(b)
            else:
                todo_global.append(b)

        todo = todo_local + todo_global
        total = len(todo)
        if total <= 0:
            self._descr_prefetch_all_done = True
            return

        self._descr_prefetch_all_started = True
        _dbg(f"{self._p.kind_label}: descr_prefetch_all_start total={total} local={len(todo_local)} global={len(todo_global)} local_libs={len(local_libs)}")

        def ui_set_status(msg: str) -> None:
            if self._closing:
                return
            # For some dialogs (e.g. symbol browser), avoid spamming the preview/status area.
            try:
                target = str(getattr(self._p, "descr_prefetch_status_target", "") or "").strip().lower()
            except Exception:
                target = ""
            if target == "index_line":
                try:
                    line = getattr(self, "_index_line", None)
                    if line and line.IsShown():
                        line.SetLabel(str(msg))
                        return
                except Exception:
                    pass
            try:
                self.prev_status.SetLabel(msg)
            except Exception:
                pass

        wx.CallAfter(ui_set_status, f"Indexing descriptions (0/{total})…")

        def worker() -> None:
            done_n = 0
            nonempty_n = 0
            chunk: dict[str, str] = {}
            last_flush = 0.0

            def flush() -> None:
                if not chunk:
                    return
                mp = dict(chunk)
                chunk.clear()

                def apply_on_ui() -> None:
                    if self._closing or self._descr_prefetch_all_cancel:
                        return
                    for k, v in mp.items():
                        if k not in self._descr_cache:
                            self._descr_cache[k] = v
                    ui_set_status(f"Indexing descriptions ({done_n}/{total})…")

                wx.CallAfter(apply_on_ui)

            for b in todo:
                if self._closing or self._descr_prefetch_all_cancel:
                    break
                try:
                    rr = self._repr_ref_for_base(b)
                    d = self._p.extract_description_for_ref(self._repo_path, rr) or ""
                except Exception:
                    d = ""
                dd = (d or "").replace("\n", " ").strip()
                if len(dd) > 180:
                    dd = dd[:177] + "…"
                chunk[b] = dd
                done_n += 1
                if dd:
                    nonempty_n += 1

                now = time.monotonic()
                if len(chunk) >= 200 or (now - last_flush) >= 0.8:
                    last_flush = now
                    flush()

            flush()

            def finish_on_ui() -> None:
                if self._closing:
                    return
                if self._descr_prefetch_all_cancel:
                    _dbg(f"{self._p.kind_label}: descr_prefetch_all_cancelled done={done_n}/{total} nonempty={nonempty_n}")
                    return
                self._descr_prefetch_all_done = True
                _dbg(f"{self._p.kind_label}: descr_prefetch_all_done done={done_n}/{total} nonempty={nonempty_n} descr_cache={len(self._descr_cache)}")
                try:
                    self.prev_status.SetLabel(f"Descriptions indexed ({nonempty_n}/{total})")
                except Exception:
                    pass
                try:
                    if (self.filter.GetValue() or "").strip():
                        self._schedule_search_recompute()
                except Exception:
                    pass

            wx.CallAfter(finish_on_ui)

        threading.Thread(target=worker, daemon=True).start()

