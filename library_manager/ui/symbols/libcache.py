from __future__ import annotations

import json as _json
import os
from pathlib import Path
import re
import gzip as _gzip
import hashlib as _hashlib
import time as _time
import subprocess as _subprocess
import sys as _sys
import threading
from typing import Any

import wx

from ...library_formats import read_library_table, symbol_names, symbol_metadata, symbol_files

from ...suggest import list_symbols
from ..kicad_env import expand_kicad_uri, kicad_config_root, kicad_version_dir, prime_kicad_env_vars, project_root_from_repo
from ..cache_dir import plugin_cache_dir


def _parse_lib_table(path: str, repo_path: str) -> dict[str, dict[str, str]]:
    return read_library_table(path, lambda uri: expand_kicad_uri(uri, repo_path))


def _lib_table_paths(repo_path: str) -> list[str]:
    """
    Return sym-lib-table paths in priority order (project first, then user/global).
    """
    proj = project_root_from_repo(repo_path)
    out: list[str] = []
    p_sym = os.path.join(proj, "sym-lib-table")
    if os.path.exists(p_sym):
        out.append(p_sym)

    root = kicad_config_root()
    if root:
        vdir = kicad_version_dir(root) or root
        cand_dirs = [vdir]
        if os.path.abspath(vdir) != os.path.abspath(root):
            cand_dirs.append(root)
        for d in cand_dirs:
            c_sym = os.path.join(d, "sym-lib-table")
            if os.path.exists(c_sym) and c_sym not in out:
                out.append(c_sym)
    return out


def _repo_symbols_signature(repo_path: str) -> str:
    """
    Fast signature for repo-local symbol libraries.

    Used to detect when Symbols/*.kicad_sym changed so we can refresh the cached
    symbol list without requiring a plugin restart.
    """
    rp = os.path.abspath(str(repo_path or "").strip())
    sym_dir = os.path.join(rp, "Symbols")
    rows: list[str] = []
    try:
        for library in sorted(Path(sym_dir).iterdir()):
            if library.suffix not in ('.kicad_sym', '.kicad_symdir'):
                continue
            for member in symbol_files(library):
                st = member.stat()
                rows.append(f"{member}\t{st.st_mtime_ns}\t{st.st_size}")
    except OSError:
        return "no_symbols_dir"
    rows.sort()
    raw = ("\n".join(rows)).encode("utf-8", errors="ignore")
    return _hashlib.sha256(raw).hexdigest()


def _scan_kicad_sym_file_names(sym_lib_path: str, lib_name: str) -> list[str]:
    return symbol_names(sym_lib_path, lib_name)


def _scan_kicad_sym_file_meta(sym_lib_path: str, lib_name: str) -> dict[str, tuple[str, str]]:
    return symbol_metadata(sym_lib_path, lib_name)


class SymbolLibraryCache:
    """
    Background-loaded combined (project + global) symbol index.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state_by_repo: dict[str, dict[str, Any]] = {}

    def ensure_started(self, repo_path: str) -> None:
        repo_path = os.path.abspath(repo_path or "")
        with self._lock:
            st = self._state_by_repo.get(repo_path)
            if st and st.get("loading"):
                return
            if st and st.get("loaded"):
                # Auto-refresh when repo-local Symbols/*.kicad_sym changed.
                try:
                    prev_sig = str(st.get("repo_symbols_sig") or "")
                    cur_sig = _repo_symbols_signature(repo_path)
                    if prev_sig and cur_sig and prev_sig == cur_sig:
                        return
                except Exception:
                    return
                st["loading"] = True
                st["error"] = ""
            else:
                st = {
                    "loading": True,
                    "loaded": False,
                    "symbols": [],
                    "sym_meta": {},  # ref -> (descr, datasheet) (lazy per library)
                    "sym_lib_files": {},  # lib -> .kicad_sym abs path
                    "sym_meta_loaded_libs": set(),  # libs whose meta has been loaded
                    "sym_meta_events": {},  # lib -> threading.Event for in-flight meta load
                    "error": "",
                    "repo_symbols_sig": "",
                }
            self._state_by_repo[repo_path] = st

        def work():
            try:
                try:
                    prime_kicad_env_vars()
                except Exception:
                    pass

                local_syms = list_symbols(repo_path)
                syms = list(local_syms)

                sym_files: dict[str, str] = {}

                # Repo-local libraries: Symbols/<lib>.kicad_sym
                sym_dir = os.path.join(repo_path, "Symbols")
                try:
                    for fn in os.listdir(sym_dir):
                        if not fn.endswith((".kicad_sym", ".kicad_symdir")):
                            continue
                        lib = os.path.splitext(fn)[0]
                        p = os.path.join(sym_dir, fn)
                        sym_files[lib] = p
                except Exception:
                    pass

                # Global/project libraries from sym-lib-table
                for t in _lib_table_paths(repo_path):
                    for name, meta in _parse_lib_table(t, repo_path).items():
                        uri = (meta.get("uri") or "").strip()
                        if not uri:
                            continue
                        if meta.get("type", "").casefold() == "kicad" and (os.path.isfile(uri) or os.path.isdir(uri)):
                            sym_files.setdefault(name, uri)

                # Scan each lib file once for symbol NAMES only (fast path).
                for lib_name, lib_path in list(sym_files.items()):
                    syms.extend(_scan_kicad_sym_file_names(lib_path, lib_name))

                syms = sorted(set(syms))
                repo_sig = _repo_symbols_signature(repo_path)
                return (syms, sym_files, "", repo_sig)
            except Exception as e:  # noqa: BLE001
                return ([], {}, str(e), "")

        def done(res, _err):
            syms, sym_files, err_txt, repo_sig = res if res else ([], {}, "load failed", "")
            with self._lock:
                st2 = self._state_by_repo.get(repo_path) or {}
                st2["loading"] = False
                st2["loaded"] = True if not err_txt else False
                st2["symbols"] = syms
                st2["sym_lib_files"] = sym_files
                if repo_sig:
                    st2["repo_symbols_sig"] = str(repo_sig)
                st2.setdefault("sym_meta", {})
                st2.setdefault("sym_meta_loaded_libs", set())
                st2.setdefault("sym_meta_events", {})
                st2["error"] = err_txt
                self._state_by_repo[repo_path] = st2

        def runner() -> None:
            res = None
            try:
                res = work()
            except Exception as e:  # noqa: BLE001
                res = ([], {}, str(e), "")
            try:
                wx.CallAfter(done, res, None)
            except Exception:
                return

        threading.Thread(target=runner, daemon=True).start()

    def ensure_meta_loaded(self, repo_path: str, lib: str, *, wait_s: float = 10.0) -> None:
        """
        Ensure Description/Datasheet metadata is loaded for a library nickname.
        Safe to call from background threads; will block up to wait_s if another
        thread is already loading the same library.
        """
        repo_path = os.path.abspath(repo_path or "")
        lib = (lib or "").strip()
        if not repo_path or not lib:
            return

        do_load = False
        ev: threading.Event | None = None
        lib_path = ""
        with self._lock:
            st = self._state_by_repo.get(repo_path) or {}
            loaded_libs = st.get("sym_meta_loaded_libs")
            if isinstance(loaded_libs, set) and lib in loaded_libs:
                return

            lib_path = ((st.get("sym_lib_files") or {}) or {}).get(lib) or ""
            if not lib_path or not os.path.exists(lib_path):
                return

            ev_map = st.get("sym_meta_events")
            if not isinstance(ev_map, dict):
                ev_map = {}
            ev = ev_map.get(lib)
            if isinstance(ev, threading.Event):
                # someone else is loading; we'll wait below
                do_load = False
            else:
                ev = threading.Event()
                ev_map[lib] = ev
                st["sym_meta_events"] = ev_map
                if not isinstance(loaded_libs, set):
                    loaded_libs = set()
                    st["sym_meta_loaded_libs"] = loaded_libs
                self._state_by_repo[repo_path] = st
                do_load = True

        if not ev:
            return

        if not do_load:
            try:
                ev.wait(timeout=max(0.0, float(wait_s)))
            except Exception:
                return
            return

        # We are the loader for this library.
        meta_map: dict[str, tuple[str, str]] = {}
        try:
            # Prefer subprocess worker to avoid starving the wx UI thread via the GIL.
            jobs = [{"lib": lib, "path": lib_path}]
            try:
                # __file__ = .../library_manager/ui/symbols/libcache.py
                # Need PYTHONPATH to include the *parent* of `library_manager/`.
                lm_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))  # .../library_manager
                pkg_parent = os.path.dirname(lm_dir)
            except Exception:
                pkg_parent = ""
            env = dict(os.environ)
            if pkg_parent:
                prev = str(env.get("PYTHONPATH") or "")
                env["PYTHONPATH"] = (pkg_parent + (os.pathsep + prev if prev else ""))
            cp = _subprocess.run(
                [_sys.executable, "-m", "library_manager.ui.symbols.meta_worker"],
                input=_json.dumps({"libs": jobs}),
                check=False,
                stdout=_subprocess.PIPE,
                stderr=_subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=max(1.0, float(wait_s or 10.0)),
            )
            if int(getattr(cp, "returncode", 1)) == 0:
                try:
                    resp = _json.loads((cp.stdout or "").strip() or "{}")
                except Exception:
                    resp = {}
                mm2 = resp.get("meta") if isinstance(resp, dict) else None
                if isinstance(mm2, dict):
                    for k, v in mm2.items():
                        try:
                            if isinstance(v, (list, tuple)) and len(v) >= 2:
                                meta_map[str(k)] = (str(v[0] or ""), str(v[1] or ""))
                        except Exception:
                            continue
            # Fallback: parse in-process if subprocess yielded nothing.
            if not meta_map:
                meta_map = _scan_kicad_sym_file_meta(lib_path, lib)
        except Exception:
            meta_map = {}

        with self._lock:
            st = self._state_by_repo.get(repo_path) or {}
            try:
                mm = st.get("sym_meta")
                if not isinstance(mm, dict):
                    mm = {}
                    st["sym_meta"] = mm
                mm.update(meta_map)
            except Exception:
                pass
            try:
                loaded_libs = st.get("sym_meta_loaded_libs")
                if not isinstance(loaded_libs, set):
                    loaded_libs = set()
                    st["sym_meta_loaded_libs"] = loaded_libs
                loaded_libs.add(lib)
            except Exception:
                pass
            try:
                ev_map = st.get("sym_meta_events")
                if isinstance(ev_map, dict):
                    ev_map.pop(lib, None)
            except Exception:
                pass
            self._state_by_repo[repo_path] = st
        try:
            ev.set()
        except Exception:
            pass

    def prefetch_meta_subprocess(self, repo_path: str, libs: list[str], *, timeout_s: float | None = None) -> None:
        """
        Prefetch symbol (Description, Datasheet) metadata for multiple libs using a subprocess.

        Rationale: CPU-heavy parsing in Python threads can still starve the wx UI thread due to
        the GIL. Running the parsing in a separate process keeps the UI responsive.
        """
        repo_path = os.path.abspath(repo_path or "")
        libs = [str(x or "").strip() for x in (libs or []) if str(x or "").strip()]
        if not repo_path or not libs:
            return

        # Determine which libs are eligible and not already loaded.
        with self._lock:
            st = self._state_by_repo.get(repo_path) or {}
            sym_files = dict(st.get("sym_lib_files") or {})
            loaded_libs = st.get("sym_meta_loaded_libs")
            if not isinstance(loaded_libs, set):
                loaded_libs = set()
                st["sym_meta_loaded_libs"] = loaded_libs
            ev_map = st.get("sym_meta_events")
            if not isinstance(ev_map, dict):
                ev_map = {}
                st["sym_meta_events"] = ev_map
            self._state_by_repo[repo_path] = st

        jobs: list[dict[str, str]] = []
        for lib in libs:
            if lib in loaded_libs:
                continue
            if lib in ev_map:
                # Someone else is loading it.
                continue
            p = str(sym_files.get(lib) or "").strip()
            if not (p and os.path.exists(p)):
                continue
            jobs.append({"lib": lib, "path": p})
        if not jobs:
            return

        # Ensure subprocess can import our package via PYTHONPATH.
        try:
            lm_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))  # .../library_manager
            pkg_parent = os.path.dirname(lm_dir)
        except Exception:
            pkg_parent = ""
        env = dict(os.environ)
        if pkg_parent:
            prev = str(env.get("PYTHONPATH") or "")
            env["PYTHONPATH"] = (pkg_parent + (os.pathsep + prev if prev else ""))

        cmd = [_sys.executable, "-m", "library_manager.ui.symbols.meta_worker"]
        inp = _json.dumps({"libs": jobs})
        try:
            # Heuristic timeout: allow roughly 1.5s per lib, up to 5 minutes.
            if timeout_s is None:
                timeout_s = min(300.0, max(15.0, 1.5 * float(len(jobs))))
        except Exception:
            timeout_s = 120.0

        try:
            cp = _subprocess.run(
                cmd,
                input=inp,
                check=False,
                stdout=_subprocess.PIPE,
                stderr=_subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=max(5.0, float(timeout_s or 120.0)),
            )
        except Exception:
            return
        if int(getattr(cp, "returncode", 1)) != 0:
            return
        try:
            resp = _json.loads((cp.stdout or "").strip() or "{}")
        except Exception:
            resp = {}
        if not isinstance(resp, dict):
            return
        meta = resp.get("meta")
        loaded = resp.get("loaded_libs")
        if not isinstance(meta, dict) or not isinstance(loaded, list):
            return

        # Apply to cache state.
        with self._lock:
            st2 = self._state_by_repo.get(repo_path) or {}
            mm = st2.get("sym_meta")
            if not isinstance(mm, dict):
                mm = {}
                st2["sym_meta"] = mm
            for k, v in meta.items():
                try:
                    if isinstance(v, (list, tuple)) and len(v) >= 2:
                        mm[str(k)] = (str(v[0] or ""), str(v[1] or ""))
                except Exception:
                    continue
            loaded_libs2 = st2.get("sym_meta_loaded_libs")
            if not isinstance(loaded_libs2, set):
                loaded_libs2 = set()
                st2["sym_meta_loaded_libs"] = loaded_libs2
            for lib in loaded:
                try:
                    if isinstance(lib, str) and lib.strip():
                        loaded_libs2.add(lib.strip())
                except Exception:
                    continue
            # Clear any in-flight markers for these libs.
            try:
                ev_map2 = st2.get("sym_meta_events")
                if isinstance(ev_map2, dict):
                    for lib in loaded:
                        ev_map2.pop(lib, None)
            except Exception:
                pass
            self._state_by_repo[repo_path] = st2

    def snapshot(self, repo_path: str) -> dict[str, Any]:
        repo_path = os.path.abspath(repo_path or "")
        with self._lock:
            st = self._state_by_repo.get(repo_path) or {
                "loading": False,
                "loaded": False,
                "symbols": [],
                "sym_meta": {},
                "sym_lib_files": {},
                "sym_meta_loaded_libs": set(),
                "sym_meta_events": {},
                "error": "",
            }
            return dict(st)

    # -------- persisted description cache (for fast symbol browser open) --------

    def _descr_cache_path(self, repo_path: str) -> str:
        rp = os.path.abspath(str(repo_path or "").strip())
        key = _hashlib.sha256(rp.encode("utf-8", errors="ignore")).hexdigest()[:24]
        return os.path.join(plugin_cache_dir(), f"sym_descr_cache_{key}.json.gz")

    def _descr_cache_fingerprint(self, repo_path: str) -> str:
        """
        Fingerprint based on the set of symbol library files + their mtimes/sizes.
        Fast (stats only) and stable enough to invalidate when libraries change.
        """
        rp = os.path.abspath(str(repo_path or "").strip())
        st = self.snapshot(rp)
        sym_files = dict(st.get("sym_lib_files") or {})
        rows: list[str] = []
        for lib, p in sym_files.items():
            nick = str(lib or "").strip()
            ap = str(p or "").strip()
            if not (nick and ap):
                continue
            try:
                for member in symbol_files(ap):
                    st2 = member.stat()
                    rows.append(f"{nick}\t{member}\t{st2.st_mtime_ns}\t{st2.st_size}")
            except Exception:
                rows.append(f"{nick}\t{ap}\t?\t?")
        rows.sort()
        raw = ("\n".join(rows)).encode("utf-8", errors="ignore")
        return _hashlib.sha256(raw).hexdigest()

    def load_description_cache(self, repo_path: str) -> dict[str, str] | None:
        """
        Load cached base->description map if fingerprint matches current libraries.
        """
        rp = os.path.abspath(str(repo_path or "").strip())
        p = self._descr_cache_path(rp)
        if not os.path.isfile(p):
            return None
        try:
            with _gzip.open(p, "rt", encoding="utf-8", errors="ignore") as f:
                d = _json.loads(f.read() or "{}")
        except Exception:
            return None
        if not isinstance(d, dict):
            return None
        want_fp = self._descr_cache_fingerprint(rp)
        if str(d.get("fingerprint") or "") != str(want_fp or ""):
            return None
        mp = d.get("map")
        if not isinstance(mp, dict):
            return None
        out: dict[str, str] = {}
        for k, v in mp.items():
            try:
                kk = str(k or "").strip()
                if not kk:
                    continue
                out[kk] = str(v or "")
            except Exception:
                continue
        return out

    def save_description_cache(self, repo_path: str, mp: dict[str, str]) -> None:
        """
        Save base->description map to a gzip JSON cache keyed by repo_path.
        """
        rp = os.path.abspath(str(repo_path or "").strip())
        if not rp or not isinstance(mp, dict) or not mp:
            return
        fp = self._descr_cache_fingerprint(rp)
        payload = {
            "version": 1,
            "repo_path": rp,
            "fingerprint": fp,
            "created_ts": float(_time.time()),
            "map": dict(mp),
        }
        try:
            with _gzip.open(self._descr_cache_path(rp), "wt", encoding="utf-8", newline="\n") as f:
                f.write(_json.dumps(payload))
                f.write("\n")
        except Exception:
            return


SYMBOL_LIBCACHE = SymbolLibraryCache()


def resolve_symbol_lib_path(repo_path: str, lib: str) -> str | None:
    """
    Resolve a lib nickname to a .kicad_sym file path (repo-local first, else indexed).
    """
    lib = (lib or "").strip()
    if not lib:
        return None
    for suffix in (".kicad_sym", ".kicad_symdir"):
        p = os.path.join(repo_path, "Symbols", f"{lib}{suffix}")
        if os.path.exists(p):
            return p
    st = SYMBOL_LIBCACHE.snapshot(repo_path)
    return (st.get("sym_lib_files") or {}).get(lib)

