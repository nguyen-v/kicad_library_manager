#!/usr/bin/env python3
import argparse, json, os, time, hashlib, zipfile, shutil
from datetime import datetime, timezone

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def file_size(path: str) -> int:
    return os.path.getsize(path)

def zip_install_size(path: str) -> int:
    with zipfile.ZipFile(path, "r") as z:
        return sum(i.file_size for i in z.infolist())

def load_json_or_default(path: str, default_obj):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default_obj

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--pages_base_url", required=True)

    ap.add_argument("--zip_path", required=True)
    ap.add_argument("--tag", required=True)      # v0.1.0
    ap.add_argument("--version", required=True)  # 0.1.0
    ap.add_argument("--asset_name", required=True)

    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)

    ap.add_argument("--pkg_identifier", required=True)
    ap.add_argument("--pkg_name", required=True)
    ap.add_argument("--pkg_description", required=True)
    ap.add_argument("--pkg_description_full", required=True)
    ap.add_argument("--author_name", required=True)
    ap.add_argument("--author_web", required=True)
    ap.add_argument("--license", default="GPL-3.0")
    ap.add_argument("--kicad_min", default="9.0")
    ap.add_argument("--status", default="stable")
    ap.add_argument("--runtime", default="ipc")

    # icon handling
    ap.add_argument("--icon_src", default="", help="Path to a PNG icon file to copy into outdir (optional)")
    ap.add_argument("--icon_filename", default="icon.png", help="Filename to publish in outdir if icon_src is used")

    args = ap.parse_args()
    outdir = args.outdir
    os.makedirs(outdir, exist_ok=True)

    packages_path = os.path.join(outdir, "packages.json")
    repo_path = os.path.join(outdir, "repository.json")

    # Compute artifact metadata for this release
    dl_url = f"https://github.com/{args.owner}/{args.repo}/releases/download/{args.tag}/{args.asset_name}"
    dl_sha = sha256_file(args.zip_path)
    dl_size = file_size(args.zip_path)
    inst_size = zip_install_size(args.zip_path)

    version_entry = {
        "version": args.version,
        "status": args.status,
        "kicad_version": args.kicad_min,
        "runtime": args.runtime,
        "platforms": ["windows", "macos", "linux"],
        "download_url": dl_url,
        "download_sha256": dl_sha,
        "download_size": dl_size,
        "install_size": inst_size
    }

    # Optional: publish icon to Pages and reference it in package resources
    icon_url = ""
    if args.icon_src and os.path.exists(args.icon_src):
        dst = os.path.join(outdir, args.icon_filename)
        shutil.copyfile(args.icon_src, dst)
        icon_url = f"{args.pages_base_url.rstrip('/')}/{args.icon_filename}"

    # Load or initialize packages.json
    pkg_array = load_json_or_default(packages_path, {"packages": []})
    if "packages" not in pkg_array or not isinstance(pkg_array["packages"], list):
        pkg_array = {"packages": []}

    # Find or create package object
    pkg = None
    for p in pkg_array["packages"]:
        if p.get("identifier") == args.pkg_identifier:
            pkg = p
            break

    resources = {
        "homepage": args.author_web,
        "repository": f"https://github.com/{args.owner}/{args.repo}"
    }
    if icon_url:
        resources["icon"] = icon_url  # harmless if ignored by older clients

    if pkg is None:
        pkg = {
            "$schema": "https://go.kicad.org/pcm/schemas/v1",
            "name": args.pkg_name,
            "description": args.pkg_description,
            "description_full": args.pkg_description_full,
            "identifier": args.pkg_identifier,
            "type": "plugin",
            "author": {
                "name": args.author_name,
                "contact": {"web": args.author_web}
            },
            "license": args.license,
            "resources": resources,
            "versions": []
        }
        pkg_array["packages"].append(pkg)
    else:
        # update resources (e.g., icon changes)
        pkg["resources"] = resources

    # Upsert version
    versions = [v for v in pkg.get("versions", []) if v.get("version") != args.version]
    versions.insert(0, version_entry)
    pkg["versions"] = versions

    with open(packages_path, "w", encoding="utf-8") as f:
        json.dump(pkg_array, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # repository.json (points at packages.json)
    now = int(time.time())
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    packages_url = f"{args.pages_base_url.rstrip('/')}/packages.json"
    packages_sha = sha256_file(packages_path)

    repo_obj = {
        "$schema": "https://gitlab.com/kicad/code/kicad/-/raw/master/kicad/pcm/schemas/pcm.v1.schema.json#/definitions/Repository",
        "name": f"{args.owner}'s KiCad PCM repository",
        "maintainer": {
            "name": args.author_name,
            "contact": {"web": args.author_web}
        },
        "packages": {
            "url": packages_url,
            "sha256": packages_sha,
            "update_timestamp": now,
            "update_time_utc": now_str
        }
    }

    with open(repo_path, "w", encoding="utf-8") as f:
        json.dump(repo_obj, f, indent=2, ensure_ascii=False)
        f.write("\n")

if __name__ == "__main__":
    main()
