# Maintainer: guybrush01
# Based on https://github.com/anandprtp/Antra

pkgname=antra
pkgver=1.1.7
pkgrel=1
pkgdesc="Desktop music library builder that turns streaming links into a fully tagged local library in FLAC, ALAC, AAC, or MP3"
arch=("x86_64")
url="https://github.com/anandprtp/Antra"
license=("Apache-2.0")
depends=("webkit2gtk-4.1" "ffmpeg" "chromium")
makedepends=("go" "nodejs" "npm" "python-pip" "python-virtualenv" "upx")
optdepends=(
    "ffmpeg: required for audio processing and downloads"
    "chromium: required for browser-based login flows (Amazon, Apple, Spotify)"
)
provides=("antra")
conflicts=("antra-git")
source=("https://github.com/guybrush01/Antra/archive/refs/tags/v${pkgver}.tar.gz")
sha256sums=("ed46134a36bcef11187d64ae3dfa61824611167dd2bf0a9d950b8b0531a2e9bc")

build() {
  cd "${srcdir}/Antra-${pkgver}"

  # Install Python dependencies into a virtual environment
  python -m venv "${srcdir}/venv"
  source "${srcdir}/venv/bin/activate"
  python -m pip install --upgrade pip
  pip install -r requirements-runtime.txt
  pip install pyinstaller

  # Playwright chromium (used by browser login flows)
  playwright install chromium 2>/dev/null || true

  # Build Python backend as standalone binary
  cd antra-wails
  mkdir -p runtime/backend

  pyinstaller \
    backend_runtime.spec \
    --distpath ./runtime/backend \
    --noconfirm \
    --workpath "${srcdir}/pyinstaller_work"

  if [[ ! -f runtime/backend/AntraBackend ]]; then
    error "PyInstaller failed to produce AntraBackend binary"
    return 1
  fi

  # Install frontend dependencies
  npm --prefix frontend ci

  # Build Wails binary
  export PATH="${HOME}/go/bin:${PATH}"
  go install github.com/wailsapp/wails/v2/cmd/wails@latest
  wails build --tags webkit2_41
}

package() {
  cd "${srcdir}/Antra-${pkgver}/antra-wails"

  local bin_dir="${pkgdir}/usr/bin"
  local share_dir="${pkgdir}/usr/share"
  local icons_dir="${share_dir}/icons/hicolor"
  local apps_dir="${share_dir}/applications"
  local manidir="${share_dir}/metainfo"

  # Binary
  install -Dm755 "build/bin/Antra" "${bin_dir}/${pkgname}"

  # Desktop entry
  mkdir -p "${apps_dir}"
  cat > "${apps_dir}/antra.desktop" <<'EOF'
[Desktop Entry]
Name=Antra
Comment=Desktop music library builder
GenericName=Music Library Manager
Exec=antra
Icon=antra
Terminal=false
Type=Application
Categories=AudioVideo;Audio;Player;
StartupWMClass=Antra
Keywords=music;spotify;downloads;library;flac;
EOF

  # AppData / metainfo
  mkdir -p "${manidir}"
  cat > "${manidir}/io.github.anandprtp.Antra.appdata.xml" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>io.github.anandprtp.Antra</id>
  <name>Antra</name>
  <summary>Desktop music library builder</summary>
  <developer_name>Hoshiyaar Singh</developer_name>
  <url type="homepage">https://github.com/anandprtp/Antra</url>
  <url type="bugtracker">https://github.com/anandprtp/Antra/issues</url>
  <metadata_license>CC0-1.0</metadata_license>
  <project_license>Apache-2.0</project_license>
  <description>
    <p>
      Antra turns Spotify, YouTube Music, Apple Music, Amazon Music, Tidal,
      Qobuz, and Deezer links into a fully tagged local library in FLAC,
      ALAC, AAC, or MP3 format.
    </p>
  </description>
  <launchable type="desktop-id">antra.desktop</launchable>
  <content_rating type="oars-1.1" />
</component>
EOF

  # Icon
  if [[ -f "../assets/antra-header.svg" ]]; then
    mkdir -p "${icons_dir}/scalable/apps"
    install -Dm644 "../assets/antra-header.svg" "${icons_dir}/scalable/apps/antra.svg"
  fi
}
