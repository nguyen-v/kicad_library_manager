from __future__ import annotations

import threading
from typing import Any, Callable, Optional

import wx


ResultHandler = Callable[[Any, Optional[Exception]], None]


def is_window_alive(win: wx.Window | None) -> bool:
    """
    Best-effort guard used before touching wx objects from deferred callbacks.
    """
    if not win:
        return False
    try:
        if not bool(win):
            return False
    except Exception:
        return False
    try:
        if win.IsBeingDeleted():
            return False
    except Exception:
        return False
    return True


class UiDebouncer:
    """
    Debounce helper that does NOT use wx.Timer.

    Rationale: native wx timer dispatch can crash if it targets a freed wxEvtHandler.
    This uses `threading.Timer` and only touches wx via `wx.CallAfter`, guarded by
    `is_window_alive`.
    """

    def __init__(self, owner: wx.Window, *, delay_ms: int, callback: Callable[[], None]):
        self._owner = owner
        self._delay_ms = int(delay_ms)
        self._callback = callback
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._gen = 0
        self._closed = False

        try:
            owner.Bind(wx.EVT_CLOSE, self._on_owner_close)
            owner.Bind(wx.EVT_WINDOW_DESTROY, self._on_owner_destroy)
        except Exception:
            pass

    def trigger(self, *, delay_ms: int | None = None) -> None:
        if self._closed:
            return
        dms = int(self._delay_ms if delay_ms is None else delay_ms)
        if dms < 0:
            dms = 0
        delay_s = float(dms) / 1000.0
        with self._lock:
            self._gen += 1
            gen = self._gen
            if self._timer is not None:
                try:
                    self._timer.cancel()
                except Exception:
                    pass
                self._timer = None

            def fire() -> None:
                try:
                    # Drop stale fires.
                    if self._closed:
                        return
                    with self._lock:
                        if gen != self._gen:
                            return
                    if not is_window_alive(self._owner):
                        return

                    def on_ui() -> None:
                        if self._closed:
                            return
                        if not is_window_alive(self._owner):
                            return
                        try:
                            self._callback()
                        except Exception:
                            return

                    wx.CallAfter(on_ui)
                except Exception:
                    return

            t = threading.Timer(delay_s, fire)
            t.daemon = True
            self._timer = t
            try:
                t.start()
            except Exception:
                self._timer = None

    def cancel(self) -> None:
        with self._lock:
            self._gen += 1
            if self._timer is not None:
                try:
                    self._timer.cancel()
                except Exception:
                    pass
                self._timer = None

    def _on_owner_close(self, evt: wx.CloseEvent) -> None:
        self._closed = True
        self.cancel()
        try:
            evt.Skip()
        except Exception:
            pass

    def _on_owner_destroy(self, evt: wx.WindowDestroyEvent) -> None:
        """
        wx.EVT_WINDOW_DESTROY can be observed by parent windows too when children are destroyed.
        Only close the debouncer if the *owner* itself is being destroyed.
        """
        try:
            w = None
            if evt is not None and hasattr(evt, "GetWindow"):
                w = evt.GetWindow()
            if w is None and evt is not None and hasattr(evt, "GetEventObject"):
                w = evt.GetEventObject()
            if w is not None and w is not self._owner:
                try:
                    evt.Skip()
                except Exception:
                    pass
                return
        except Exception:
            # If we can't reliably tell, err on the side of keeping it alive.
            try:
                evt.Skip()
            except Exception:
                pass
            return
        self._closed = True
        self.cancel()
        try:
            evt.Skip()
        except Exception:
            pass


class UiRepeater:
    """
    Periodic callback helper that does NOT use wx.Timer.

    Calls `callback()` on the UI thread every `interval_ms` while the owner is alive.
    """

    def __init__(self, owner: wx.Window, *, interval_ms: int, callback: Callable[[], None]):
        self._owner = owner
        self._interval_s = max(1, int(interval_ms)) / 1000.0
        self._callback = callback
        self._stop = threading.Event()
        self._closed = False

        try:
            owner.Bind(wx.EVT_CLOSE, self._on_owner_close)
            owner.Bind(wx.EVT_WINDOW_DESTROY, self._on_owner_destroy)
        except Exception:
            pass

        def loop() -> None:
            while not self._stop.wait(self._interval_s):
                if self._closed:
                    break
                if not is_window_alive(self._owner):
                    break

                def on_ui() -> None:
                    if self._closed:
                        return
                    if not is_window_alive(self._owner):
                        return
                    try:
                        self._callback()
                    except Exception:
                        return

                try:
                    wx.CallAfter(on_ui)
                except Exception:
                    break

        t = threading.Thread(target=loop, daemon=True)
        t.start()

    def stop(self) -> None:
        try:
            self._stop.set()
        except Exception:
            pass

    def _on_owner_close(self, evt: wx.CloseEvent) -> None:
        self._closed = True
        self.stop()
        try:
            evt.Skip()
        except Exception:
            pass

    def _on_owner_destroy(self, evt: wx.WindowDestroyEvent) -> None:
        """
        wx.EVT_WINDOW_DESTROY can be observed by parents when children are destroyed.
        Only stop the repeater if the *owner* itself is being destroyed.
        """
        try:
            w = None
            if evt is not None and hasattr(evt, "GetWindow"):
                w = evt.GetWindow()
            if w is None and evt is not None and hasattr(evt, "GetEventObject"):
                w = evt.GetEventObject()
            if w is not None and w is not self._owner:
                try:
                    evt.Skip()
                except Exception:
                    pass
                return
        except Exception:
            try:
                evt.Skip()
            except Exception:
                pass
            return
        self._closed = True
        self.stop()
        try:
            evt.Skip()
        except Exception:
            pass


class WindowTaskRunner:
    """
    Background runner bound to a wx window lifetime.

    - never touches widgets from worker threads
    - ignores stale callbacks after close/destroy
    - swallows callback exceptions to avoid cascading UI failures
    """

    def __init__(self, owner: wx.Window):
        self._owner = owner
        self._generation = 0
        self._closed = False

        owner.Bind(wx.EVT_CLOSE, self._on_owner_close)
        owner.Bind(wx.EVT_WINDOW_DESTROY, self._on_owner_destroy)

    def cancel_pending(self) -> None:
        self._generation += 1

    def run(self, work: Callable[[], Any], on_done: ResultHandler) -> None:
        ticket = self._generation

        def worker() -> None:
            result = None
            err: Exception | None = None
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001
                err = exc

            def done_on_ui() -> None:
                if self._closed or ticket != self._generation:
                    return
                if not is_window_alive(self._owner):
                    return
                try:
                    on_done(result, err)
                except Exception:
                    # UI callback failures should not kill the plugin window.
                    return

            try:
                wx.CallAfter(done_on_ui)
            except Exception:
                return

        threading.Thread(target=worker, daemon=True).start()

    def _on_owner_close(self, evt: wx.CloseEvent) -> None:
        self._closed = True
        self.cancel_pending()
        evt.Skip()

    def _on_owner_destroy(self, _evt: wx.WindowDestroyEvent) -> None:
        self._closed = True
        self.cancel_pending()
