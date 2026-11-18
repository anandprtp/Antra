#!/usr/bin/env python3
"""Cross-check the staged Flatpak inputs against the manifest.

Run after stage.py and before flatpak-builder. It exists because the manifest,
the desktop file, the metainfo and the staging script all have to agree on the
same app-id and the same filenames, and nothing else forces them to. A rename in
one of them otherwise surfaces as a confusing flatpak-builder failure minutes
into CI - or worse, as a bundle that installs but shows a generic icon.

Everything here is checked against files that actually exist on disk, so it runs
identically on a developer machine and in CI.
"""
from __future__ import annotations

import configparser
import os
import re
import sys
import xml.etree.ElementTree as ET

import yaml

APP_ID = "io.github.anandprtp.Antra"
INSTALL_RE = re.compile(r"install -Dm\d+ (\S+) (\S+)$")

failures: list[str] = []


def ck(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def main(stage: str) -> int:
    with open(os.path.join(stage, f"{APP_ID}.yml"), encoding="utf-8") as fh:
        manifest = yaml.safe_load(fh)
    module = manifest["modules"][0]

    print("1. app identity is consistent across the manifest")
    ck(manifest["app-id"] == APP_ID, f"manifest app-id is {APP_ID}")
    ck(bool(manifest.get("runtime-version")), "runtime-version is pinned")

    print("2. every source the manifest declares exists in the stage")
    for src in module["sources"]:
        ck(os.path.isfile(os.path.join(stage, src["path"])),
           f"source present: {src['path']}")

    print("3. every file an install command reads exists in the stage")
    install_srcs, dests = [], set()
    for cmd in module["build-commands"]:
        m = INSTALL_RE.match(cmd)
        ck(m is not None, f"install command parses: {cmd[:60]}")
        if m:
            install_srcs.append(m.group(1))
            dests.add(m.group(2))
            ck(os.path.isfile(os.path.join(stage, m.group(1))),
               f"install source present: {m.group(1)}")
    declared = {s["path"] for s in module["sources"]}
    ck(set(install_srcs) == declared,
       f"install sources == declared sources ({sorted(set(install_srcs) ^ declared) or 'exact match'})")

    print("4. install destinations are the paths flatpak exports from")
    cmd_name = manifest["command"]
    ck(f"/app/bin/{cmd_name}" in dests, f"command '{cmd_name}' installed to /app/bin/{cmd_name}")
    ck(f"/app/share/applications/{APP_ID}.desktop" in dests,
       "desktop file installed under the app-id name")
    ck(f"/app/share/metainfo/{APP_ID}.metainfo.xml" in dests,
       "metainfo installed under the app-id name")
    icons = {d for d in dests if "/icons/hicolor/" in d}
    ck(len(icons) >= 1, f"icon sizes installed: {sorted(d.split('/')[-3] for d in icons)}")
    ck(all(d.endswith(f"/{APP_ID}.png") for d in icons),
       "icons named after the app-id (required for icon export)")

    print("5. staged metainfo is valid after the version rewrite")
    root = ET.parse(os.path.join(stage, f"{APP_ID}.metainfo.xml")).getroot()
    releases = root.findall("releases/release")
    ck(len(releases) == 1,
       f"exactly one <release>: {[(r.get('version'), r.get('date')) for r in releases]}")
    if releases:
        ck("dev" not in (releases[0].get("version") or ""),
           f"version placeholder replaced (version={releases[0].get('version')})")
    ck(root.findtext("id", "").strip() == APP_ID, "metainfo id matches app-id")
    ck(root.findtext("launchable", "").strip() == f"{APP_ID}.desktop",
       "launchable matches the installed desktop file")
    ck(len(root.findtext("summary", "")) <= 90, "summary within the 90-char AppStream limit")

    print("6. desktop entry agrees with the manifest")
    cfg = configparser.RawConfigParser()
    cfg.optionxform = str
    cfg.read(os.path.join(stage, f"{APP_ID}.desktop"), encoding="utf-8")
    entry = cfg["Desktop Entry"]
    ck(entry["Icon"] == APP_ID, "desktop Icon= is the app-id")
    ck(entry["Exec"] == cmd_name, f"desktop Exec= matches manifest command ('{cmd_name}')")

    print("7. the binary is a real payload, not a stub")
    size = os.path.getsize(os.path.join(stage, "Antra"))
    ck(size > 20_000_000,
       f"binary is {size:,} bytes (the embedded PyInstaller backend is present)")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S) - refusing to build the bundle")
        return 1
    print("all Flatpak staging checks passed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_stage.py <staging-dir>")
    raise SystemExit(main(sys.argv[1]))
