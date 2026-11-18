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

# ── CRITICAL: put ROOT on sys.path BEFORE any collect_submodules() call ──────
# collect_submodules() genuinely IMPORTS the package to walk it, using the live
# sys.path — `pathex` below only affects PyInstaller's own Analysis, not this.
# PyInstaller is run from antra-wails/, so `antra` is NOT importable at spec
# time and collect_submodules("antra") silently returned an EMPTY list. Every
# build therefore relied entirely on static import analysis, which is
# version-dependent: v1.1.8 built fine on PyInstaller 6.19 locally and shipped
# a binary MISSING antra.utils.organizer from CI's 6.22.2, crashing on the first
# download. Measured: 0 submodules before this line, 73 after.
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── Hidden imports ──────────────────────────────────────────────────────────
# PyInstaller misses dynamically-imported modules; list them explicitly.
hiddenimports = (
    collect_submodules("antra")
    + collect_submodules("antra_shared")
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

# ── Belt-and-braces: ship the antra/antra_shared SOURCE tree as data ────────
# v1.1.8 shipped THREE times with antra.utils.organizer absent from the PYZ,
# despite it being an explicit hidden import with no warning emitted. Rather
# than keep fighting the module graph, also place the real .py files at
# _MEIPASS/antra/... : PyInstaller sets a frozen package's __path__ to its
# _MEIPASS directory, so a submodule missing from the PYZ is still importable
# from disk. If the PYZ has it, the PYZ wins and this costs only a few hundred KB.
for _pkg in ("antra", "antra_shared"):
    for _src in sorted((ROOT / _pkg).rglob("*.py")):
        if "__pycache__" in _src.parts:
            continue
        datas.append((str(_src), str(_src.parent.relative_to(ROOT))))

# ── Chromaprint fpcalc (audio fingerprint verification) ─────────────────────
# Optional: when the platform binary has been vendored it ships at
# _MEIPASS/chromaprint/fpcalc[.exe], which antra.utils.runtime.get_fpcalc_exe
# looks for. When it is absent the build still succeeds and the verification
# check degrades to 'skipped' rather than failing downloads.
_fpcalc_name = 'fpcalc.exe' if sys.platform == 'win32' else 'fpcalc'
_fpcalc = ROOT / 'antra-wails' / 'third_party' / 'chromaprint' / _fpcalc_name
if _fpcalc.is_file():
    datas.append((str(_fpcalc), 'chromaprint'))
    print(f'[spec] bundling fpcalc from {_fpcalc}')
else:
    print(f'[spec] fpcalc not vendored at {_fpcalc} - audio verification will be unavailable')

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

# ── Post-Analysis enforcement (v1.1.8 hotfix) ───────────────────────────────
# v1.1.8 shipped twice with antra.utils.organizer missing. It WAS in
# hiddenimports and PyInstaller emitted no warning — the module graph simply
# dropped it, and only inspecting the built archive revealed that. So: verify
# what Analysis actually produced, inject anything missing straight from disk,
# and fail the build if it still is not there. Never trust the graph again.
_CRITICAL = [
    "antra.core.service", "antra.core.engine", "antra.core.resolver",
    "antra.utils.organizer", "antra.utils.tagger", "antra.utils.transcoder",
    "antra_shared.filename_prefs",
]

def _pure_names():
    return {entry[0] for entry in a.pure}

_have = _pure_names()
for _mod in _CRITICAL:
    if _mod in _have:
        continue
    _src = ROOT / (_mod.replace(".", "/") + ".py")
    if not _src.is_file():
        raise SystemExit(f"backend_runtime.spec: {_mod} missing from the checkout at {_src}")
    print(f"[spec] FORCING missing module into archive: {_mod} <- {_src}")
    a.pure.append((_mod, str(_src), "PYMODULE"))

# Every antra/antra_shared module on disk should ship; inject any other strays too.
for _pkg in ("antra", "antra_shared"):
    for _src in sorted((ROOT / _pkg).rglob("*.py")):
        _rel = _src.relative_to(ROOT).with_suffix("")
        _name = ".".join(_rel.parts)
        if _name.endswith(".__init__"):
            _name = _name[: -len(".__init__")]
        if _name not in _pure_names():
            print(f"[spec] injecting {_name}")
            a.pure.append((_name, str(_src), "PYMODULE"))

_still = [m for m in _CRITICAL if m not in _pure_names()]
if _still:
    raise SystemExit(f"backend_runtime.spec: refusing to build, still missing: {_still}")
print(f"[spec] archive verified: {len(_pure_names())} pure modules, all critical present")

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
