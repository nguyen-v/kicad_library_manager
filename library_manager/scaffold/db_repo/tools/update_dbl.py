#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys


def read_headers(csv_path: str) -> list[str]:
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        return next(reader)


def table_from_csv_path(csv_path: str) -> str:
    base = os.path.basename(csv_path)
    if base.startswith("db-") and base.endswith(".csv"):
        return base[3:-4]
    return os.path.splitext(base)[0]


def default_visible_on_add(col: str) -> bool:
    return col in {"Value", "Package"}


def default_visible_in_chooser(col: str) -> bool:
    # Keep chooser reasonably informative by default.
    return col in {
        "IPN",
        "Value",
        "Manufacturer",
        "MPN",
        "Description",
        "Package",
        "Tolerance",
        "Voltage Rating",
        "Current Rating",
        "Power Rating",
        "Frequency",
    }


def make_library_entry(table: str, headers: list[str]) -> dict:
    name = table.replace("_", " ")
    fields = []
    for h in headers:
        # KiCad DBL has dedicated "symbols"/"footprints" mappings. Including these columns
        # again in the generic "fields" list can cause odd UI behavior, e.g. when the
        # Footprint column contains multiple refs separated by semicolons:
        # KiCad may show A;B;C as an extra "footprint" choice alongside A, B, C.
        if h in {"Symbol", "Footprint"}:
            continue
        fields.append(
            {
                "column": h,
                "name": h,
                "visible_on_add": bool(default_visible_on_add(h)),
                "visible_in_chooser": bool(default_visible_in_chooser(h)),
                "show_name": False,
            }
        )
    return {
        "name": name,
        "table": table,
        "key": "IPN",
        "symbols": "Symbol",
        "footprints": "Footprint",
        "fields": fields,
    }


def _load_category_fields_config(repo: str, table: str) -> list[dict] | None:
    """
    Optional per-category override for field visibility.
    Stored at Database/category_fields/<Table>.json
    """
    path = os.path.join(os.path.abspath(repo), "Database", "category_fields", f"{table}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if int(obj.get("schema_version", 0)) != 1:
            return None
        fields = obj.get("fields")
        if not isinstance(fields, list):
            return None
        out = []
        for it in fields:
            if not isinstance(it, dict):
                continue
            col = str(it.get("column") or it.get("name") or "").strip()
            if not col:
                continue
            out.append(
                {
                    "column": col,
                    "name": str(it.get("name") or col),
                    "visible_on_add": bool(it.get("visible_on_add", False)),
                    "visible_in_chooser": bool(it.get("visible_in_chooser", True)),
                    "show_name": bool(it.get("show_name", False)),
                }
            )
        return out or None
    except Exception:
        return None


def _fields_for_table(repo: str, table: str, headers: list[str]) -> list[dict]:
    cfg = _load_category_fields_config(repo, table)
    if not cfg:
        return [
            {
                "column": h,
                "name": h,
                "visible_on_add": bool(default_visible_on_add(h)),
                "visible_in_chooser": bool(default_visible_in_chooser(h)),
                "show_name": False,
            }
            for h in headers
            if h not in {"Symbol", "Footprint"}
        ]
    # Ensure it contains all headers (append missing with defaults)
    by_col = {f.get("column"): f for f in cfg if isinstance(f, dict)}
    out = []
    for h in headers:
        if h in {"Symbol", "Footprint"}:
            continue
        if h in by_col:
            out.append(by_col[h])
        else:
            out.append(
                {
                    "column": h,
                    "name": h,
                    "visible_on_add": bool(default_visible_on_add(h)),
                    "visible_in_chooser": bool(default_visible_in_chooser(h)),
                    "show_name": False,
                }
            )
    return out


def update(repo: str, *, dbl_filename: str = "") -> bool:
    repo = os.path.abspath(repo)
    db_dir = os.path.join(repo, "Database")
    dbl_filename = str(dbl_filename or "").strip()
    if dbl_filename:
        dbl_path = os.path.join(db_dir, dbl_filename)
        if not os.path.exists(dbl_path):
            raise RuntimeError(f"Missing DBL file: {dbl_path}")
    else:
        dbl_candidates = sorted(glob.glob(os.path.join(db_dir, "*.kicad_dbl")))
        if not dbl_candidates:
            raise RuntimeError(f"No DBL file found under: {db_dir!r} (expected Database/*.kicad_dbl)")
        if len(dbl_candidates) > 1:
            raise RuntimeError(
                "Multiple DBL files found under Database/. "
                "Please pass --dbl with the desired DBL filename.\n"
                + "\n".join(f"- {os.path.basename(p)}" for p in dbl_candidates)
            )
        dbl_path = dbl_candidates[0]

    with open(dbl_path, "r", encoding="utf-8") as f:
        dbl = json.load(f)

    libs: list[dict] = dbl.get("libraries") or []
    by_table = {lib.get("table"): lib for lib in libs if isinstance(lib, dict)}

    changed = False
    csv_paths = sorted(glob.glob(os.path.join(db_dir, "db-*.csv")))
    existing_tables: set[str] = set()
    for csv_path in csv_paths:
        table = table_from_csv_path(csv_path)
        existing_tables.add(table)
        headers = read_headers(csv_path)
        if table in by_table:
            # Update fields if config exists (or headers changed)
            lib = by_table[table]
            new_fields = _fields_for_table(repo, table, headers)
            if lib.get("fields") != new_fields:
                lib["fields"] = new_fields
                changed = True
            continue
        entry = make_library_entry(table, headers)
        entry["fields"] = _fields_for_table(repo, table, headers)
        libs.append(entry)
        changed = True

    # Remove libraries for deleted CSVs
    new_libs: list[dict] = []
    for lib in libs:
        t = lib.get("table")
        if t and t not in existing_tables:
            changed = True
            continue
        new_libs.append(lib)
    libs = new_libs

    if changed:
        dbl["libraries"] = libs
        with open(dbl_path, "w", encoding="utf-8") as f:
            json.dump(dbl, f, indent=4, sort_keys=False)
            f.write("\n")

    return changed


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Ensure Database/*.kicad_dbl contains entries for all db-*.csv tables.")
    ap.add_argument("--repo", default=".", help="Repo root (default: .)")
    ap.add_argument(
        "--dbl",
        default="",
        help="Optional DBL filename under Database/ (use when multiple *.kicad_dbl exist).",
    )
    args = ap.parse_args(argv)

    changed = update(args.repo, dbl_filename=str(args.dbl or "").strip())
    print("DBL updated" if changed else "DBL already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

