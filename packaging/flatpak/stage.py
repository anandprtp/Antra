#!/usr/bin/env python3
"""Stage the inputs flatpak-builder needs, next to the manifest.

This lives in a script rather than inline in the workflow for one reason: it can
be run and verified on a developer machine, where the surrounding YAML cannot.
Everything it does is asserted, so a silent half-staging fails the build instead
of producing a bundle with a stale version or a missing icon.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_ID = "io.github.anandprtp.Antra"
PLACEHOLDER = "0.0.0-dev"

# Files copied verbatim from packaging/flatpak into the staging directory.
VERBATIM = [f"{APP_ID}.yml", f"{APP_ID}.desktop", f"{APP_ID}.metainfo.xml"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", required=True, help="the built Linux Antra binary")
    ap.add_argument("--icon", default=None, help="512x512 PNG app icon")
    ap.add_argument("--out", required=True, help="staging directory to create")
    ap.add_argument("--version", required=True, help="release version, no leading v")
    ap.add_argument("--date", required=True, help="release date, YYYY-MM-DD")
    args = ap.parse_args()

    icon_src = Path(args.icon) if args.icon else (
        HERE.parent.parent / "antra-wails" / "build" / "linux" / "appicon.png"
    )
    src_bin = Path(args.bin)
    out = Path(args.out)

    if not src_bin.is_file():
        sys.exit(f"binary not found: {src_bin}")
    if not icon_src.is_file():
        sys.exit(f"icon not found: {icon_src}")
    if not re.fullmatch(r"\d+\.\d+\.\d+[0-9A-Za-z.\-]*", args.version):
        sys.exit(f"version does not look like a release version: {args.version!r}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        sys.exit(f"date must be YYYY-MM-DD: {args.date!r}")

    out.mkdir(parents=True, exist_ok=True)

    # The binary. Named "Antra" because that is what the manifest installs.
    dst_bin = out / "Antra"
    shutil.copy2(src_bin, dst_bin)
    os.chmod(dst_bin, 0o755)

    for name in VERBATIM:
        shutil.copy2(HERE / name, out / name)

    # Icons. 512 is shipped as-is; 128 is derived so desktops that prefer a
    # smaller icon are not downscaling a half-megabyte PNG on every paint.
    from PIL import Image

    with Image.open(icon_src) as im:
        im = im.convert("RGBA")
        if im.size != (512, 512):
            sys.exit(f"expected a 512x512 icon, got {im.size}")
        im.save(out / "icon-512.png")
        im.resize((128, 128), Image.LANCZOS).save(out / "icon-128.png")

    # Rewrite the placeholder <release> from the real tag, then prove it took.
    # A stale version in shipped metadata is the quietly-wrong-constant trap this
    # project has been bitten by before, so it is checked rather than trusted.
    meta_path = out / f"{APP_ID}.metainfo.xml"
    meta = meta_path.read_text(encoding="utf-8")
    new_release = f'<release version="{args.version}" date="{args.date}"/>'
    meta, n = re.subn(
        r'<release version="[^"]*" date="[^"]*"\s*/>', new_release, meta
    )
    if n != 1:
        sys.exit(f"expected exactly 1 <release> element to rewrite, replaced {n}")
    if PLACEHOLDER in meta:
        sys.exit("version placeholder survived the rewrite")
    meta_path.write_text(meta, encoding="utf-8", newline="\n")

    # Everything the manifest declares as a source must actually be present, or
    # flatpak-builder fails late with a less obvious message.
    expected = set(VERBATIM) | {"Antra", "icon-512.png", "icon-128.png"}
    missing = sorted(expected - {p.name for p in out.iterdir()})
    if missing:
        sys.exit(f"staging incomplete, missing: {missing}")

    print(f"staged {len(expected)} files into {out}/")
    for p in sorted(out.iterdir()):
        if p.is_file():
            print(f"  {p.name:<44} {p.stat().st_size:>10,} bytes")
    print(f"release: {new_release}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
