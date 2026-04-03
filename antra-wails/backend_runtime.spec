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
    + collect_submodules("yt_dlp")
    + collect_submodules("mutagen")
    + collect_submodules("requests")
    + collect_submodules("urllib3")
    + collect_submodules("slskd_api")
    + collect_submodules("lyricsgenius")
    + collect_submodules("platformdirs")
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
    ]
)

# ── Data files ───────────────────────────────────────────────────────────────
datas = []
for package in ("imageio_ffmpeg", "certifi", "lyricsgenius", "spotipy"):
    try:
        datas += collect_data_files(package)
    except Exception:
        pass

# ── Analysis ─────────────────────────────────────────────────────────────────
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
