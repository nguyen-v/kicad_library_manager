from __future__ import annotations

import gzip as _gzip
import hashlib as _hashlib
import os
import re
import json as _json
import subprocess as _subprocess
import sys as _sys
import threading
import time as _time
from typing import Any

import wx

from ...library_formats import read_library_table

from ...suggest import group_density_variants, list_footprints
from ..kicad_env import expand_kicad_uri, kicad_config_root, kicad_version_dir, prime_kicad_env_vars, project_root_from_repo
from ..cache_dir import plugin_cache_dir


def _parse_lib_table(path: str, repo_path: str) -> dict[str, dict[str, str]]:
    return read_library_table(path, lambda uri: expand_kicad_uri(uri, repo_path))


def _lib_table_paths(repo_path: str) -> list[str]:
    """
    Return fp-lib-table paths in priority order (project first, then user/global).
    Ported from ui.py's approach.
    """
    proj = project_root_from_repo(repo_path)
    out: list[str] = []
    p_fp = os.path.join(proj, "fp-lib-table")
    if os.path.exists(p_fp):
        out.append(p_fp)

    root = kicad_config_root()
    if root:
        vdir = kicad_version_dir(root) or root
        cand_dirs = [vdir]
        if os.path.abspath(vdir) != os.path.abspath(root):
            cand_dirs.append(root)
        for d in cand_dirs:
            c_fp = os.path.join(d, "fp-lib-table")
            if os.path.exists(c_fp) and c_fp not in out:
                out.append(c_fp)
    return out


class FootprintLibraryCache:
    """
    Background-loaded combined (project + global) footprint index.
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
                return
            st = {
                "loading": True,
                "loaded": False,
                "footprints": [],
                "footprint_groups": {},
                "fp_lib_dirs": {},
                "error": "",
            }
            self._state_by_repo[repo_path] = st

        def work():
            try:
                # Prime KiCad env vars so ${KICAD*_FOOTPRINT_DIR} expands correctly.
                try:
                    prime_kicad_env_vars()
                except Exception:
                    pass
                local_fps = list_footprints(repo_path)
                fps = list(local_fps)
                fp_dirs: dict[str, str] = {}

                for t in _lib_table_paths(repo_path):
                    for name, meta in _parse_lib_table(t, repo_path).items():
                        typ = (meta.get("type") or "").lower()
                        uri = meta.get("uri") or ""
                        if typ and "kicad" not in typ:
                            continue
                        if uri and uri.lower().endswith(".pretty") and os.path.isdir(uri):
                            fp_dirs.setdefault(name, uri)

                for lib_name, pretty_dir in fp_dirs.items():
                    try:
                        for fn in os.listdir(pretty_dir):
                            if not fn.endswith(".kicad_mod"):
                                continue
                            fp = os.path.splitext(fn)[0]
                            fps.append(f"{lib_name}:{fp}")
                    except Exception:
                        continue

                fps = sorted(set(fps))
                groups = group_density_variants(fps)
                return (fps, groups, fp_dirs, "")
            except Exception as e:  # noqa: BLE001
                return ([], {}, {}, str(e))

        def done(res, err):
            fps, groups, fp_dirs, err_txt = res if res else ([], {}, {}, "load failed")
            with self._lock:
                st2 = self._state_by_repo.get(repo_path) or {}
                st2["loading"] = False
                st2["loaded"] = True if not err_txt else False
                st2["footprints"] = fps
                st2["footprint_groups"] = groups
                st2["fp_lib_dirs"] = fp_dirs
                st2["error"] = err_txt
                self._state_by_repo[repo_path] = st2

        def runner() -> None:
            res = None
            err = None
            try:
                res = work()
            except Exception as e:  # noqa: BLE001
                err = e
            try:
                wx.CallAfter(done, res, err)
            except Exception:
                return

        threading.Thread(target=runner, daemon=True).start()

    def snapshot(self, repo_path: str) -> dict[str, Any]:
        repo_path = os.path.abspath(repo_path or "")
        with self._lock:
            st = self._state_by_repo.get(repo_path) or {
                "loading": False,
                "loaded": False,
                "footprints": [],
                "footprint_groups": {},
                "fp_lib_dirs": {},
                "error": "",
            }
            return dict(st)

    # -------- description cache (persisted; used by footprint browser) --------

    def _descr_cache_path(self, repo_path: str) -> str:
        rp = os.path.abspath(str(repo_path or "").strip())
        key = _hashlib.sha256(rp.encode("utf-8", errors="ignore")).hexdigest()[:24]
        return os.path.join(plugin_cache_dir(), f"fp_descr_cache_{key}.json.gz")

    def _descr_cache_fingerprint(self, repo_path: str) -> str:
        """
        Fingerprint based on all known `.kicad_mod` files and their mtimes/sizes.
        Computed from:
          - repo-local Footprints/*.pretty
          - fp-lib-table pretty dirs (from index snapshot)
        """
        rp = os.path.abspath(str(repo_path or "").strip())
        st = self.snapshot(rp)
        fp_dirs = dict(st.get("fp_lib_dirs") or {})
        dirs: set[str] = set()
        # Repo-local pretty dirs.
        try:
            root = os.path.join(rp, "Footprints")
            if os.path.isdir(root):
                for name in os.listdir(root):
                    if name.lower().endswith(".pretty"):
                        d = os.path.join(root, name)
                        if os.path.isdir(d):
                            dirs.add(d)
        except Exception:
            pass
        # Global/project pretty dirs.
        for _lib, d in fp_dirs.items():
            dd = str(d or "").strip()
            if dd and os.path.isdir(dd):
                dirs.add(dd)

        rows: list[str] = []
        for d in sorted(dirs):
            try:
                for fn in os.listdir(d):
                    if not fn.endswith(".kicad_mod"):
                        continue
                    p = os.path.join(d, fn)
                    try:
                        st2 = os.stat(p)
                        rows.append(f"{p}\t{int(st2.st_mtime)}\t{int(st2.st_size)}")
                    except Exception:
                        rows.append(f"{p}\t?\t?")
            except Exception:
                continue
        raw = ("\n".join(rows)).encode("utf-8", errors="ignore")
        return _hashlib.sha256(raw).hexdigest()

    def load_description_cache(self, repo_path: str) -> dict[str, str] | None:
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
                if kk:
                    out[kk] = str(v or "")
            except Exception:
                continue
        return out

    def save_description_cache(self, repo_path: str, mp: dict[str, str]) -> None:
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

    def _resolve_pretty_dir_any(self, repo_path: str, lib: str) -> str | None:
        rp = os.path.abspath(str(repo_path or "").strip())
        lib = str(lib or "").strip()
        if not (rp and lib):
            return None
        # Repo-local first.
        try:
            d = os.path.join(rp, "Footprints", f"{lib}.pretty")
            if os.path.isdir(d):
                return d
        except Exception:
            pass
        # Then indexed global/project libs.
        st = self.snapshot(rp)
        try:
            return (st.get("fp_lib_dirs") or {}).get(lib)
        except Exception:
            return None

    def extract_descriptions_subprocess(self, repo_path: str, refs: list[str], *, timeout_s: float | None = None) -> dict[str, str]:
        """
        Extract footprint descriptions for refs via subprocess.
        Returns {ref: descr}.
        """
        rp = os.path.abspath(str(repo_path or "").strip())
        refs = [str(r or "").strip() for r in (refs or []) if str(r or "").strip() and ":" in str(r or "")]
        if not rp or not refs:
            return {}

        items: list[dict[str, str]] = []
        for r in refs:
            lib, fp = r.split(":", 1)
            pretty = self._resolve_pretty_dir_any(rp, lib)
            if not pretty:
                continue
            p = os.path.join(pretty, f"{fp}.kicad_mod")
            if not os.path.exists(p):
                continue
            items.append({"ref": r, "path": p})
        if not items:
            return {}

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

        cmd = [_sys.executable, "-m", "library_manager.ui.footprints.descr_worker"]
        inp = _json.dumps({"items": items})
        try:
            if timeout_s is None:
                timeout_s = min(300.0, max(10.0, 0.05 * float(len(items))))
        except Exception:
            timeout_s = 60.0
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
                timeout=max(5.0, float(timeout_s or 60.0)),
            )
        except Exception:
            return {}
        if int(getattr(cp, "returncode", 1)) != 0:
            return {}
        try:
            resp = _json.loads((cp.stdout or "").strip() or "{}")
        except Exception:
            resp = {}
        if not isinstance(resp, dict):
            return {}
        mp = resp.get("map")
        if not isinstance(mp, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in mp.items():
            try:
                kk = str(k or "").strip()
                if kk:
                    out[kk] = str(v or "")
            except Exception:
                continue
        return out


FP_LIBCACHE = FootprintLibraryCache()


def resolve_footprint_pretty_dir(repo_path: str, lib: str) -> str | None:
    """
    Prefer repo-local Footprints scan later; this is for preview/metadata lookups.
    """
    st = FP_LIBCACHE.snapshot(repo_path)
    return (st.get("fp_lib_dirs") or {}).get((lib or "").strip())

