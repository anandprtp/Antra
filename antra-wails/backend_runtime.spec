# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for AntraBackend.exe — the self-contained Python backend
that gets embedded into the Wails desktop app via runtime_assets.go.

Build from the antra-wails/ directory:
    pyinstaller backend_runtime.spec --distpath ./runtime/backend --noconfirm

The output AntraBackend.exe lands in runtime/backend/ and is automatically
embedded by `wails build` via the //go:embed directive in runtime_assets.go.
"""

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_dynamic_libs

ROOT = Path.cwd().parent  # Antra/
ENTRY = ROOT / "antra" / "json_cli.py"

# ── Hidden imports ──────────────────────────────────────────────────────────
# PyInstaller misses dynamically-imported modules; list them explicitly.
hiddenimports = (
    collect_submodules("antra")
    + collect_submodules("spotipy")
    + collect_submodules("mutagen")
    + collect_submodules("requests")
    + collect_submodules("urllib3")
    + collect_submodules("slskd_api")
    + collect_submodules("lyricsgenius")
    + collect_submodules("platformdirs")
    + collect_submodules("Cryptodome")  # pycryptodomex — used for Python CENC fallback in Amazon adapter
    + [
        # dotenv
        "dotenv", "dotenv.main", "dotenv.compat", "dotenv.variables",
        # async runtime
        "asyncio", "asyncio.runners", "asyncio.tasks", "asyncio.events",
        # stdlib extras sometimes missed
        "email.mime.text", "email.mime.multipart", "email.mime.base",
        "xml.etree.ElementTree",
        "http.cookiejar",
        "zipfile", "tarfile",
        # spotipy internals
        "spotipy.oauth2", "spotipy.cache_handler",
        # imageio_ffmpeg — bundled ffmpeg used by Python sources
        "imageio_ffmpeg",
        # New fetchers added: SoundCloud, Amazon Music, SpotFetch
        "antra.core.soundcloud_fetcher",
        "antra.core.amazon_music_fetcher",
        "antra.core.spotfetch_fetcher",
        "antra.core.apple_fetcher",
        # Mirror server adapters
        "antra.sources.tidal_mirror",
        "antra.sources.qobuz_mirror",
        "antra.sources.deezer_mirror",
    ]
)

# ── Data files ───────────────────────────────────────────────────────────────
datas = []
for package in ("imageio_ffmpeg", "certifi", "lyricsgenius", "spotipy"):
    try:
        datas += collect_data_files(package)
    except Exception:
        pass

# Explicitly exclude playwright's driver directory (node.exe + JS bundle = ~97 MB).
# We replaced the playwright session API with raw websockets CDP calls, so node.exe
# is never executed. The playwright Python package itself is still imported only for
# `playwright install chromium` (run as a subprocess), so we keep the Python code
# but strip the 97 MB data payload.
datas = [(src, dst) for src, dst in datas
         if "playwright" not in src.replace("\\", "/").lower()]

# ── Analysis ─────────────────────────────────────────────────────────────────
# NOTE: collect_data_files("imageio_ffmpeg") already collects the ffmpeg binary
# into datas (as imageio_ffmpeg/binaries/ffmpeg-*.exe). Do NOT also add it to
# binaries= — that would cause PyInstaller to UPX-compress it, which conflicts
# with the datas copy and can cause the binary to silently fail on some machines.
a = Analysis(
    [str(ENTRY)],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # GUI toolkits — never needed in a CLI/subprocess binary
        "PySide6", "PyQt5", "PyQt6", "tkinter", "wx",
        # Test frameworks
        "pytest", "pytest_mock", "_pytest",
        # Jupyter / IPython
        "IPython", "jupyter", "notebook",
        # Matplotlib / numpy / scipy — not used
        "matplotlib", "numpy", "scipy", "pandas",
        # playwright.async_api and playwright.sync_api are NOT imported at runtime
        # (we use raw websockets CDP). Only playwright._impl is needed for the
        # `playwright install chromium` subprocess call path, but even that is
        # optional. Excluding the whole package drops node.exe (~86 MB) from the build.
        # The `playwright install chromium` subprocess call works because it invokes
        # the system Python's playwright, not the bundled one.
        "playwright",
    ],
    noarchive=False,
    optimize=1,  # compile .pyc with basic optimisations (strips docstrings)
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AntraBackend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,           # UPX compression — shaves ~20-30% off size
    upx_exclude=[
        "vcruntime140.dll",
        "python3*.dll",
    ],
    runtime_tmpdir=None,
    console=True,       # Must be True — the Go parent reads stdout via pipe
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
