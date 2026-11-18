"""Vendor the Chromaprint `fpcalc` binary for this platform.

`fpcalc` produces the audio fingerprints used by antra.utils.audio_verify to
check a downloaded file really contains the requested recording. It is an
official release binary from the Chromaprint project, not built here.

    python tools/fetch_fpcalc.py            # fetch for this platform
    python tools/fetch_fpcalc.py --check    # report what is vendored, fetch nothing

The binary lands at antra-wails/third_party/chromaprint/fpcalc[.exe], where
backend_runtime.spec picks it up. Every archive is verified against a pinned
SHA-256 before anything is unpacked; a mismatch aborts without writing.

Bundling is optional: when the binary is absent the build still succeeds and
the verification check reports "skipped" instead of failing downloads.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "1.6.1"
BASE = f"https://github.com/acoustid/chromaprint/releases/download/v{VERSION}"
DEST = Path(__file__).resolve().parents[1] / "antra-wails" / "third_party" / "chromaprint"

# asset suffix -> sha256 of the published archive (verified 2026-09-23)
ASSETS = {
    "windows-x86_64.zip": "735d6182b38e9f364b84ce6f4ccd682c75e2851de89735711d6b762d12b92a4e",
    "linux-x86_64.tar.gz": "fc16cd37a70168040bc9ceb45f1d4d1216f5a75bc4c9cf8564bea70ac6a45733",
    "linux-arm64.tar.gz": "7eaf5d655c4aa172ab28e3c870b8bb61dd2c327ac94de145676f88842cf6215a",
    "macos-x86_64.tar.gz": "0de8947c09dd93c44cece2f5947d408136a3b6692eed726d1f109506500bd773",
    "macos-arm64.tar.gz": "254f23cb2d290069ba1d3d28199414fbf66d2054fc2f6821c2fc62ed39470a95",
}


def asset_for_platform() -> str:
    machine = platform.machine().lower()
    arm = machine in {"arm64", "aarch64"}
    if sys.platform == "win32":
        return "windows-x86_64.zip"
    if sys.platform == "darwin":
        return "macos-arm64.tar.gz" if arm else "macos-x86_64.tar.gz"
    return "linux-arm64.tar.gz" if arm else "linux-x86_64.tar.gz"


def extract(blob: bytes, asset: str, dest: Path) -> Path:
    name = "fpcalc.exe" if asset.startswith("windows") else "fpcalc"
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / name
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            member = next(n for n in zf.namelist() if n.endswith(name))
            data = zf.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            member = next(m for m in tf.getmembers()
                          if m.isfile() and m.name.endswith(name))
            data = tf.extractfile(member).read()  # type: ignore[union-attr]
    target.write_bytes(data)
    if not asset.startswith("windows"):
        # zip/tar extraction does not preserve the execute bit reliably.
        os.chmod(target, 0o755)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="report what is vendored and exit")
    args = parser.parse_args()

    name = "fpcalc.exe" if sys.platform == "win32" else "fpcalc"
    target = DEST / name
    if args.check:
        if target.is_file():
            print(f"vendored: {target} ({target.stat().st_size} bytes)")
            return 0
        print(f"not vendored: {target}")
        return 1
    if target.is_file():
        print(f"already vendored: {target}")
        return 0

    asset = asset_for_platform()
    url = f"{BASE}/chromaprint-fpcalc-{VERSION}-{asset}"
    print(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - fixed https URL
        blob = resp.read()
    digest = hashlib.sha256(blob).hexdigest()
    expected = ASSETS[asset]
    if digest != expected:
        print(f"ERROR: checksum mismatch for {asset}\n  expected {expected}\n  got      {digest}")
        return 2
    print(f"checksum ok ({digest[:16]}...)")
    path = extract(blob, asset, DEST)
    print(f"vendored: {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
