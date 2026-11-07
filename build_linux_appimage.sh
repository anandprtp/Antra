#!/usr/bin/env bash
# Build Antra-Linux.AppImage (mirrors upstream release.yml linux job).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
WAILS_DIR="$ROOT/antra-wails"
OUT_APPIMAGE="$ROOT/Antra-Linux.AppImage"
TOOLS="$ROOT/.tools"
SYSROOT="$TOOLS/sysroot"
PCDIR="$TOOLS/pkgconfig"
VENV="${VENV:-$ROOT/.venv}"

export PATH="${TOOLS}/go/bin:${TOOLS}/gopath/bin:${PATH}"
export GOPATH="${TOOLS}/gopath"
export GOMODCACHE="${GOPATH}/pkg/mod"
export GOCACHE="${TOOLS}/gocache"
export GOSUMDB="${GOSUMDB:-off}"

if [[ -d "$PCDIR" ]]; then
  export PKG_CONFIG_PATH="$PCDIR:/usr/lib/x86_64-linux-gnu/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
fi
# Local -dev extraction puts unversioned .so linker names under sysroot.
if [[ -d "$SYSROOT/usr/lib/x86_64-linux-gnu" ]]; then
  export LIBRARY_PATH="$SYSROOT/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu${LIBRARY_PATH:+:$LIBRARY_PATH}"
  export CGO_LDFLAGS="-L$SYSROOT/usr/lib/x86_64-linux-gnu -L/usr/lib/x86_64-linux-gnu ${CGO_LDFLAGS:-}"
fi

PYTHON="${VENV}/bin/python"
PIP="${VENV}/bin/pip"
PYINSTALLER="${VENV}/bin/pyinstaller"

die() { echo "[FAIL] $*" >&2; exit 1; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"; }

echo "============================================================"
echo "  ANTRA Linux AppImage Build"
echo "============================================================"

need_cmd pkg-config
need_cmd cc
need_cmd wget
need_cmd file

[[ -x "${TOOLS}/go/bin/go" ]] || die "Go toolchain missing under .tools/go (install go 1.23+)"
[[ -x "${TOOLS}/gopath/bin/wails" ]] || die "wails missing; run: go install github.com/wailsapp/wails/v2/cmd/wails@v2.12.0"
[[ -x "$PYTHON" ]] || die "venv python missing at $VENV (python -m venv .venv && pip install -r requirements-desktop.txt)"
[[ -x "$PYINSTALLER" ]] || die "pyinstaller missing in venv"

pkg-config --exists webkit2gtk-4.1 gtk+-3.0 \
  || die "webkit2gtk-4.1 / gtk+-3.0 pkg-config not available (install libwebkit2gtk-4.1-dev libgtk-3-dev)"

echo
echo "[1/4] Building Python backend (PyInstaller)..."
mkdir -p "$WAILS_DIR/runtime/backend"
WORK="$WAILS_DIR/_pyinstaller_work"
mkdir -p "$WORK"
# Spec resolves ROOT as Path.cwd().parent — must run from antra-wails/.
(
  cd "$WAILS_DIR"
  "$PYINSTALLER" backend_runtime.spec \
    --distpath runtime/backend \
    --workpath "$WORK" \
    --noconfirm
)
BACKEND="$WAILS_DIR/runtime/backend/AntraBackend"
[[ -x "$BACKEND" ]] || die "expected backend binary at $BACKEND"
echo "  [OK] AntraBackend ($(du -h "$BACKEND" | cut -f1))"

echo
echo "[2/4] Building Wails app (webkit2_41)..."
(
  cd "$WAILS_DIR"
  wails build -clean -tags webkit2_41
)
BIN="$(find "$WAILS_DIR/build/bin" -maxdepth 1 -type f -executable | head -1)"
[[ -n "$BIN" ]] || die "no executable found under antra-wails/build/bin"
echo "  [OK] $(basename "$BIN") ($(du -h "$BIN" | cut -f1))"

echo
echo "[3/4] Packaging AppDir..."
APPDIR="$ROOT/AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
cp "$BIN" "$APPDIR/usr/bin/Antra"
printf '%s\n' \
  '[Desktop Entry]' \
  'Name=Antra' \
  'Exec=Antra' \
  'Icon=Antra' \
  'Type=Application' \
  'Categories=AudioVideo;Audio;' \
  > "$APPDIR/Antra.desktop"
ICON="$WAILS_DIR/frontend/src/assets/images/logo-universal.png"
[[ -f "$ICON" ]] || die "missing icon $ICON"
cp "$ICON" "$APPDIR/Antra.png"
printf '%s\n' '#!/bin/bash' 'exec "$(dirname "$(readlink -f "${0}")")/usr/bin/Antra" "$@"' > "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun" "$APPDIR/usr/bin/Antra"

echo
echo "[4/4] Running appimagetool..."
TOOL=/tmp/appimagetool-x86_64.AppImage
if [[ ! -x "$TOOL" ]]; then
  wget -q https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage -O "$TOOL"
  chmod +x "$TOOL"
fi
rm -f "$OUT_APPIMAGE"
# Works without FUSE (common in containers / restricted hosts).
export APPIMAGE_EXTRACT_AND_RUN=1
ARCH=x86_64 "$TOOL" "$APPDIR" "$OUT_APPIMAGE"
chmod +x "$OUT_APPIMAGE"

echo
echo "============================================================"
echo "  Build Complete"
echo "============================================================"
echo "  Output : $OUT_APPIMAGE"
echo "  Size   : $(du -h "$OUT_APPIMAGE" | cut -f1)"
echo "  Runtime dependency: sudo apt install libwebkit2gtk-4.1-0"
echo "============================================================"
