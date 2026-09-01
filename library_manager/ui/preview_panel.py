from __future__ import annotations

import sys
import threading
from typing import Callable

import wx

from .async_ui import UiDebouncer
from .assets.preview import cached_svg_and_png, hires_target_px, letterbox_bitmap, letterbox_image, wx_image_silent


class PreviewPanel(wx.Panel):
    """
    Reusable preview widget (choice + status + updated + bitmap).

    - Can be used by asset browsers, pickers, generators, etc.
    - Provides a cached SVG->PNG->Bitmap render helper using `assets.preview`.
    """

    def __init__(
        self,
        parent: wx.Window,
        *,
        empty_label: str = "(select an item)",
        show_choice: bool = True,
        choice_parent: wx.Window | None = None,
        min_bitmap_size: tuple[int, int] = (520, 320),
        crop_to_alpha: bool = False,
        letterbox_padding: float = 0.92,
    ):
        super().__init__(parent)
        self._empty_label = empty_label
        self._gen = 0
        self._closed = False
        self._crop_to_alpha = bool(crop_to_alpha)
        self._install_hint_shown = False
        try:
            self._letterbox_padding = float(letterbox_padding)
        except Exception:
            self._letterbox_padding = 0.92
        self._last_render: tuple[str, str, str, str, Callable[[str, str], None]] | None = None

        # IMPORTANT: avoid wx.Timer (native crash risk if handler is freed).
        self._rerender_debouncer = UiDebouncer(self, delay_ms=250, callback=lambda: self._on_rerender_timer(None))

        def _on_destroy(_evt=None):
            self._closed = True
            try:
                if getattr(self, "_rerender_debouncer", None):
                    self._rerender_debouncer.cancel()
            except Exception:
                pass

        try:
            self.Bind(wx.EVT_WINDOW_DESTROY, _on_destroy)
        except Exception:
            pass

        s = wx.BoxSizer(wx.VERTICAL)

        # Some callers (e.g. footprint generator) want the dropdown outside this panel
        # (in a separate sizer row). In that case, create the Choice with the containing
        # window as parent so wx sizers don't assert on mismatched parents.
        cp = choice_parent or self
        self.choice = wx.Choice(cp, choices=[])
        self.choice.Enable(False)
        if show_choice:
            # Only valid when the choice is parented to this panel.
            if cp is self:
                s.Add(self.choice, 0, wx.ALL | wx.EXPAND, 6)
        else:
            # Important: if the caller wants to place `choice` in an external sizer,
            # do not add it to ours (wx asserts if a window belongs to two sizers).
            try:
                if cp is self:
                    self.choice.Hide()
            except Exception:
                pass

        self.status = wx.StaticText(self, label=self._empty_label)
        s.Add(self.status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 6)

        self.updated = wx.StaticText(self, label="")
        s.Add(self.updated, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 6)

        self.bmp = wx.StaticBitmap(self, size=(-1, -1))
        try:
            self.bmp.SetMinSize(min_bitmap_size)
        except Exception:
            pass
        s.Add(self.bmp, 1, wx.ALL | wx.EXPAND, 6)

        self.SetSizer(s)

        # Re-render at new size (debounced) if we have a last render request.
        try:
            self.bmp.Bind(wx.EVT_SIZE, self._on_bmp_size)
        except Exception:
            pass

    def _maybe_prompt_preview_tools(self, err: Exception) -> None:
        """
        Best-effort one-time prompt when the host system lacks an SVG rasterizer.

        We only prompt when the error indicates "no converter", to avoid spamming users for other failures.
        """
        if self._closed:
            return
        try:
            if bool(self._install_hint_shown):
                return
        except Exception:
            return
        msg = str(err or "").strip()
        # Also treat the common wxGTK ConvertToBitmap failure as "need external converter",
        # in case an older build still hits that path.
        if ("No SVG->PNG converter found" not in msg) and (
            "Failed to gain raw access to bitmap data" not in msg
        ):
            return
        try:
            self._install_hint_shown = True
        except Exception:
            pass

        plat = str(sys.platform or "").lower()
        if plat.startswith("linux"):
            # Offer one-click install (pkexec/sudo) instead of a static hint.
            try:
                from .linux_deps import offer_install_linux_host_deps

                offer_install_linux_host_deps(self, force=True)
                return
            except Exception:
                pass
            body = (
                "Preview rendering needs an SVG rasterizer.\n\n"
                "Debian/Ubuntu:\n"
                "  sudo ./scripts/setup_linux.sh\n"
                "or:\n"
                "  sudo apt install librsvg2-bin\n\n"
                "Then restart KiCad."
            )
        elif plat.startswith("darwin"):
            body = (
                "Preview rendering needs an SVG rasterizer.\n\n"
                "Recommended (Homebrew):\n"
                "  brew install librsvg\n\n"
                "Alternative:\n"
                "  brew install --cask inkscape\n\n"
                "Then restart KiCad."
            )
        elif plat.startswith("win"):
            body = (
                "Preview rendering needs an SVG rasterizer.\n\n"
                "Recommended:\n"
                "- Install Inkscape, then ensure `inkscape` is available on PATH.\n\n"
                "Then restart KiCad."
            )
        else:
            body = (
                "Preview rendering needs an SVG rasterizer.\n\n"
                "Try one of:\n"
                "- Debian/Ubuntu:  sudo apt install librsvg2-bin\n"
                "- Fedora:         sudo dnf install librsvg2-tools\n"
                "- Arch:           sudo pacman -S librsvg\n\n"
                "Then restart KiCad."
            )
        try:
            wx.MessageBox(body, "Enable previews", wx.OK | wx.ICON_INFORMATION)
        except Exception:
            pass

    def Destroy(self) -> bool:  # type: ignore[override]
        # Stop debouncers before C++ deletion is scheduled (Destroy may not emit EVT_CLOSE).
        try:
            self._closed = True
        except Exception:
            pass
        try:
            if getattr(self, "_rerender_debouncer", None):
                self._rerender_debouncer.cancel()
        except Exception:
            pass
        return super().Destroy()

    def set_choice_visible(self, vis: bool) -> None:
        try:
            # If the choice is wrapped in a "field box" panel, hide/show the wrapper.
            box = getattr(self, "choice_box", None)
            if box:
                box.Show(bool(vis))
            else:
                self.choice.Show(bool(vis))
        except Exception:
            pass
        try:
            self.Layout()
        except Exception:
            pass

    def set_empty(self) -> None:
        # Cancel any in-flight async render: if a background render finishes after the
        # selection was cleared, it would otherwise re-apply the old bitmap.
        try:
            self._gen += 1
        except Exception:
            pass
        try:
            self._last_render = None
        except Exception:
            pass
        try:
            if getattr(self, "_rerender_debouncer", None):
                self._rerender_debouncer.cancel()
        except Exception:
            pass
        try:
            self.status.SetLabel(self._empty_label)
        except Exception:
            pass
        try:
            self.updated.SetLabel("")
        except Exception:
            pass
        try:
            self.bmp.SetBitmap(wx.NullBitmap)
            self.bmp.Refresh()
        except Exception:
            pass

    def _on_bmp_size(self, evt: wx.SizeEvent) -> None:
        try:
            self._schedule_rerender()
        finally:
            try:
                evt.Skip()
            except Exception:
                pass

    def _schedule_rerender(self) -> None:
        if not self._last_render:
            return
        try:
            if getattr(self, "_rerender_debouncer", None):
                self._rerender_debouncer.cancel()
        except Exception:
            pass
        try:
            if getattr(self, "_rerender_debouncer", None):
                self._rerender_debouncer.trigger(delay_ms=250)
        except Exception:
            pass

    def _on_rerender_timer(self, _evt=None) -> None:
        if not self._last_render:
            return
        kind_dir, cache_key_prefix, ref, source_mtime, render_svg = self._last_render
        self.render_cached_svg_async(
            kind_dir=kind_dir,
            cache_key_prefix=cache_key_prefix,
            ref=ref,
            source_mtime=source_mtime,
            render_svg=render_svg,
            set_status=False,
        )

    def render_cached_svg_async(
        self,
        *,
        kind_dir: str,
        cache_key_prefix: str,
        ref: str,
        source_mtime: str,
        render_svg: Callable[[str, str], None],
        quality_scale: float = 2.5,
        set_status: bool = True,
    ) -> None:
        """
        Render a ref to the bitmap using the shared SVG->PNG cache.

        `render_svg(ref, out_svg_path)` must create an SVG.
        `source_mtime` can be a real mtime or a stable hash/key for parameterized previews.
        """
        if self._closed:
            return
        self._gen += 1
        gen = self._gen
        self._last_render = (kind_dir, cache_key_prefix, ref, source_mtime, render_svg)

        if set_status:
            try:
                self.status.SetLabel("Rendering…")
            except Exception:
                pass

        try:
            pw, ph = self.bmp.GetClientSize()
        except Exception:
            pw, ph = (520, 300)
        # When called during initial layout, the bitmap client size can be (0, 0) or very small.
        # Rendering at that size produces a tiny preview until the user resizes the window.
        # Defer the first real render until after layout has produced a sane size.
        try:
            if int(pw) < 50 or int(ph) < 50:
                wx.CallAfter(self._schedule_rerender)
                return
        except Exception:
            pass
        # wx/GTK can change the bitmap's client size over a few layout ticks without
        # delivering a reliable EVT_SIZE to the bitmap. This makes the first render
        # slightly too small (cropped) until the user manually resizes the window.
        # Re-check size for a few ticks and trigger a rerender if it changes.
        try:
            pw0, ph0 = int(pw), int(ph)
            gen0 = int(gen)

            def _tick_check(pw_prev: int, ph_prev: int, remaining: int) -> None:
                if self._closed or gen0 != self._gen:
                    return
                if remaining <= 0:
                    return
                try:
                    pw2, ph2 = self.bmp.GetClientSize()
                    pw2i, ph2i = int(pw2), int(ph2)
                except Exception:
                    return
                if pw2i <= 0 or ph2i <= 0:
                    return
                if abs(pw2i - pw_prev) >= 2 or abs(ph2i - ph_prev) >= 2:
                    self._schedule_rerender()
                    pw_prev, ph_prev = pw2i, ph2i
                wx.CallAfter(lambda: _tick_check(pw_prev, ph_prev, remaining - 1))

            wx.CallAfter(lambda: _tick_check(pw0, ph0, 4))
        except Exception:
            pass
        png_w, png_h = hires_target_px(self.bmp, pw, ph, quality_scale=quality_scale)

        def worker() -> None:
            png_path = ""
            svg_path = ""
            err: Exception | None = None
            try:
                raster = cached_svg_and_png(
                    kind_dir=kind_dir,
                    cache_key_prefix=cache_key_prefix,
                    ref=ref,
                    source_mtime=str(source_mtime or "0"),
                    png_w=png_w,
                    png_h=png_h,
                    render_svg=render_svg,
                )
                png_path = str(getattr(raster, "png_path", "") or "")
                svg_path = str(getattr(raster, "svg_path", "") or "")
                if not png_path and not svg_path:
                    raise RuntimeError("Preview render failed")
            except Exception as e:  # noqa: BLE001
                err = e

            def done_on_ui() -> None:
                if self._closed or gen != self._gen:
                    return
                if err:
                    try:
                        self.status.SetLabel(f"Preview unavailable: {err}")
                    except Exception:
                        pass
                    try:
                        self._maybe_prompt_preview_tools(err)
                    except Exception:
                        pass
                    return
                try:
                    # IMPORTANT: wx objects must be created on the UI thread (KiCad/wx can segfault otherwise).
                    # Prefer Image-space letterboxing: wxGTK often fails Bitmap.ConvertToImage /
                    # alpha MemoryDC with "Failed to gain raw access to bitmap data".
                    bmp: wx.Bitmap | None = None
                    w, h = self.bmp.GetClientSize()
                    if png_path:
                        img = wx_image_silent(png_path)
                        if not img.IsOk():
                            raise RuntimeError("PNG load failed")
                        boxed_img = letterbox_image(
                            img,
                            w,
                            h,
                            padding=self._letterbox_padding,
                            crop_to_alpha=self._crop_to_alpha,
                        )
                        use_img = boxed_img if (boxed_img is not None and boxed_img.IsOk()) else img
                        try:
                            bmp = wx.Bitmap(use_img)
                        except Exception:
                            # Fallback: some wx ports fail to create a bitmap from a wx.Image.
                            try:
                                bmp = wx.Bitmap(png_path, wx.BITMAP_TYPE_PNG)
                            except Exception as e:
                                raise RuntimeError(f"PNG bitmap creation failed: {e}") from e
                            bmp = letterbox_bitmap(
                                bmp,
                                w,
                                h,
                                padding=self._letterbox_padding,
                                crop_to_alpha=self._crop_to_alpha,
                            ) or bmp
                    elif svg_path:
                        # External SVG->PNG converter was missing. On wxGTK,
                        # wx.svg.SVGimage.ConvertToBitmap() commonly fails with
                        # "Failed to gain raw access to bitmap data" (FromBufferRGBA),
                        # so do not use it as a silent fallback — ask for rsvg/inkscape.
                        if str(sys.platform or "").lower().startswith("linux"):
                            raise RuntimeError(
                                "No SVG->PNG converter found (install rsvg-convert or inkscape)"
                            )
                        try:
                            import wx.svg as _wxsvg  # type: ignore
                        except Exception:
                            _wxsvg = None  # type: ignore
                        if not _wxsvg:
                            raise RuntimeError(
                                "No SVG->PNG converter found (install rsvg-convert or inkscape)"
                            )
                        try:
                            svg_img = _wxsvg.SVGimage.CreateFromFile(svg_path)
                        except Exception as e:
                            raise RuntimeError(f"SVG load failed: {e}") from e
                        try:
                            try:
                                bmp = svg_img.ConvertToBitmap(int(png_w), int(png_h))
                            except Exception:
                                # Some wx builds provide ConvertToBitmap() without sizing.
                                bmp = svg_img.ConvertToBitmap()
                        except Exception as e:
                            raise RuntimeError(
                                "No SVG->PNG converter found (install rsvg-convert or inkscape)"
                            ) from e
                        if bmp and bmp.IsOk():
                            bmp = letterbox_bitmap(
                                bmp,
                                w,
                                h,
                                padding=self._letterbox_padding,
                                crop_to_alpha=self._crop_to_alpha,
                            ) or bmp
                    if not bmp or not bmp.IsOk():
                        raise RuntimeError("Bitmap render failed")
                    self.bmp.SetBitmap(bmp)
                    self.bmp.Refresh()
                    # Even for resize-triggered rerenders (set_status=False), clear a stale
                    # "Rendering…" label if we successfully updated the bitmap.
                    if set_status:
                        self.status.SetLabel("")
                    else:
                        try:
                            cur = str(self.status.GetLabel() or "")
                            if cur.strip().lower().startswith("rendering"):
                                self.status.SetLabel("")
                        except Exception:
                            pass
                except Exception as e:
                    # IMPORTANT: never leave the UI stuck at "Rendering…" on failure.
                    try:
                        self.status.SetLabel(f"Preview unavailable: {e}")
                    except Exception:
                        pass
                    try:
                        self._maybe_prompt_preview_tools(e)
                    except Exception:
                        pass
                    try:
                        self.bmp.SetBitmap(wx.NullBitmap)
                        self.bmp.Refresh()
                    except Exception:
                        pass

            try:
                wx.CallAfter(done_on_ui)
            except Exception:
                return

        threading.Thread(target=worker, daemon=True).start()

