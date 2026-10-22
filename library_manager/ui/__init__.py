"""
Refactored, modular wx UI for the library manager.

This package intentionally replaces the old monolithic `ui.py` while keeping
the same external entry points used by `plugin.py`.
"""

__all__ = ["MainDialog", "RepoSettingsDialog"]


def __getattr__(name):
    # Backend helpers (paths, CLI discovery and library indexing) are also used
    # by headless tools. Import wx only when a window is actually requested.
    from importlib import import_module
    modules = {"MainDialog": ".main_window", "RepoSettingsDialog": ".dialogs"}
    if name not in modules:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(modules[name], __name__), name)
    globals()[name] = value
    return value
