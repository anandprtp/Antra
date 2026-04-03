#!/usr/bin/env python3
"""
build_desktop.py — one-command build for the Antra desktop app.

Usage:
    python build_desktop.py

Steps:
  1. Build AntraBackend.exe (Python + all deps via PyInstaller)
  2. Copy it into antra-wails/runtime/backend/
  3. Build Antra.exe (Wails embeds the backend into the Go binary)

Output: antra-wails/build/bin/Antra.exe  (~80-120 MB, fully self-contained)
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
WAILS_DIR = ROOT / "antra-wails"
SPEC_FILE = WAILS_DIR / "backend_runtime.spec"
BACKEND_DEST = WAILS_DIR / "runtime" / "backend"
FINAL_EXE = WAILS_DIR / "build" / "bin" / "Antra.exe"


def run(cmd: list, cwd=None, desc=""):
    label = desc or " ".join(str(c) for c in cmd[:3])
    print(f"\n{'-'*60}")
    print(f"  {label}")
    print(f"{'-'*60}")
    result = subprocess.run(cmd, cwd=cwd or ROOT)
    if result.returncode != 0:
        print(f"\n[FAIL] '{label}' exited with code {result.returncode}")
        sys.exit(result.returncode)


def check_tools():
    missing = []
    for tool in ("pyinstaller", "wails"):
        if not shutil.which(tool):
            missing.append(tool)
    if missing:
        print(f"[ERROR] Missing required tools: {', '.join(missing)}")
        if "pyinstaller" in missing:
            print("  Install: pip install pyinstaller")
        if "wails" in missing:
            print("  Install: https://wails.io/docs/gettingstarted/installation")
        sys.exit(1)


def check_upx():
    if not shutil.which("upx"):
        print("[WARN] UPX not found — binary will not be compressed (install UPX to reduce size)")
        print("       Download: https://github.com/upx/upx/releases")


def main():
    print("\n" + "=" * 60)
    print("  ANTRA Desktop Build")
    print("=" * 60)

    check_tools()
    check_upx()

    # ── Step 1: Build AntraBackend.exe via PyInstaller ──────────────────────
    print("\n[1/3] Building Python backend (PyInstaller)...")
    BACKEND_DEST.mkdir(parents=True, exist_ok=True)

    # PyInstaller work dir (cache + temp) — keep it local so it doesn't pollute source
    work_dir = WAILS_DIR / "_pyinstaller_work"
    work_dir.mkdir(exist_ok=True)

    run(
        [
            sys.executable, "-m", "PyInstaller",
            str(SPEC_FILE),
            "--distpath", str(BACKEND_DEST),
            "--workpath", str(work_dir),
            "--noconfirm",
        ],
        cwd=WAILS_DIR,
        desc="PyInstaller - bundling Python backend",
    )

    backend_exe = BACKEND_DEST / "AntraBackend.exe"
    if not backend_exe.exists():
        print(f"[FAIL] Expected backend at {backend_exe} — not found after PyInstaller")
        sys.exit(1)

    size_mb = backend_exe.stat().st_size / (1024 * 1024)
    print(f"\n  [OK] AntraBackend.exe  ({size_mb:.1f} MB)")

    # Clean up PyInstaller work dir (not needed after build)
    shutil.rmtree(work_dir, ignore_errors=True)

    # ── Step 2+3: Build Antra.exe via Wails (installs frontend + embeds backend) ──
    print("\n[2/2] Building Wails desktop app (embeds backend + frontend)...")
    run(
        ["wails", "build"],
        cwd=WAILS_DIR,
        desc="wails build — packaging Antra.exe",
    )

    if not FINAL_EXE.exists():
        print(f"[FAIL] Expected Antra.exe at {FINAL_EXE} — not found")
        sys.exit(1)

    total_mb = FINAL_EXE.stat().st_size / (1024 * 1024)

    print("\n" + "=" * 60)
    print("  Build Complete")
    print("=" * 60)
    print(f"  Output : {FINAL_EXE}")
    print(f"  Size   : {total_mb:.1f} MB")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
