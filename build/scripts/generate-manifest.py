#!/usr/bin/env python3
"""Write the build manifest for a finished Boswas OS image.

Usage: generate-manifest.py --image-info FILE --iso FILE --packages FILE
                            --output-dir DIR --manifest-dir DIR [--set KEY=VALUE ...]

Produces:
  OUTPUT_DIR/<image>.manifest.txt     human-readable manifest + full package list
  MANIFEST_DIR/<build-id>.json        machine-readable build record
  MANIFEST_DIR/<build-id>.packages    package list (name<TAB>version)
  MANIFEST_DIR/latest.json            copy of the most recent build record
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import sys
from pathlib import Path


def parse_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key] = " ".join(shlex.split(value))
    return values


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image-info", type=Path, required=True)
    ap.add_argument("--iso", type=Path, required=True)
    ap.add_argument("--packages", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--manifest-dir", type=Path, required=True)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="extra facts recorded in the manifest")
    args = ap.parse_args()

    info = parse_env(args.image_info)
    extra = dict(item.split("=", 1) for item in args.set)
    packages = []
    for line in args.packages.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            packages.append((parts[0], parts[1]))

    iso_sha = sha256(args.iso)
    record = {
        "schema": "boswas-build-manifest/1",
        "name": info.get("BOSWAS_NAME"),
        "version": info.get("BOSWAS_VERSION"),
        "version_id": info.get("BOSWAS_VERSION_ID"),
        "build_id": info.get("BOSWAS_BUILD_ID"),
        "build_date": info.get("BOSWAS_BUILD_DATE"),
        "source_date_epoch": int(info.get("BOSWAS_SOURCE_DATE_EPOCH", "0")),
        "channel": info.get("BOSWAS_CHANNEL"),
        "architecture": info.get("BOSWAS_ARCH"),
        "debian": {
            "suite": info.get("BOSWAS_DEBIAN_SUITE"),
            "version": extra.pop("debian_version", None),
            "kernel": extra.pop("kernel", None),
        },
        "source": {
            "git_commit": info.get("BOSWAS_GIT_COMMIT"),
            "git_dirty": info.get("BOSWAS_GIT_DIRTY"),
            "config_hash": info.get("BOSWAS_CONFIG_HASH"),
        },
        "tooling": {
            "live_build": info.get("BOSWAS_LIVE_BUILD_VERSION"),
            **{k: v for k, v in extra.items()},
        },
        "iso": {
            "file": args.iso.name,
            "size_bytes": args.iso.stat().st_size,
            "sha256": iso_sha,
        },
        "package_count": len(packages),
    }

    args.manifest_dir.mkdir(parents=True, exist_ok=True)
    build_id = record["build_id"]
    record_json = json.dumps(record, indent=2) + "\n"
    (args.manifest_dir / f"{build_id}.json").write_text(record_json)
    (args.manifest_dir / "latest.json").write_text(record_json)
    (args.manifest_dir / f"{build_id}.packages").write_text(
        "".join(f"{name}\t{version}\n" for name, version in packages))

    stem = args.iso.name[:-len(".iso")] if args.iso.name.endswith(".iso") else args.iso.name
    lines = [
        f"# {record['name']} {record['version']} - build manifest",
        "",
        f"Build ID:            {build_id}",
        f"Build date (UTC):    {record['build_date']}",
        f"Version ID:          {record['version_id']}",
        f"Channel:             {record['channel']}",
        f"Architecture:        {record['architecture']}",
        f"Debian suite:        {record['debian']['suite']}",
        f"Debian version:      {record['debian']['version']}",
        f"Kernel:              {record['debian']['kernel']}",
        f"Git commit:          {record['source']['git_commit']} (dirty: {record['source']['git_dirty']})",
        f"Config hash:         {record['source']['config_hash']}",
        f"live-build:          {record['tooling']['live_build']}",
    ]
    lines += [f"{k + ':':<21}{v}" for k, v in extra.items()]
    lines += [
        f"ISO:                 {args.iso.name}",
        f"ISO size:            {record['iso']['size_bytes']} bytes",
        f"ISO SHA-256:         {iso_sha}",
        f"Packages:            {len(packages)}",
        "",
        "## Installed packages (name, version)",
        "",
    ]
    lines += [f"{name}\t{version}" for name, version in packages]
    (args.output_dir / f"{stem}.manifest.txt").write_text("\n".join(lines) + "\n")
    print(f"manifest: {args.output_dir / (stem + '.manifest.txt')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
