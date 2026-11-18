from __future__ import annotations

import os
import sys
import shutil
from pathlib import Path
from typing import Optional


def _scan_meipass_ffmpeg(name_contains: str = "ffmpeg", exclude: str = "ffprobe") -> Optional[str]:
    """Scan sys._MEIPASS/imageio_ffmpeg/binaries/ for the binary directly.

    imageio_ffmpeg's get_ffmpeg_exe() can fail in some PyInstaller environments
    even when the binary is present. This is the hard fallback.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None
    binaries_dir = Path(meipass) / "imageio_ffmpeg" / "binaries"
    if not binaries_dir.is_dir():
        return None
    for f in binaries_dir.iterdir():
        n = f.name.lower()
        if name_contains in n and exclude not in n and f.is_file():
            return str(f)
    return None


def get_ffmpeg_exe() -> Optional[str]:
    """Return the absolute path to the ffmpeg binary, or None if not found.

    Checks system PATH first, then the imageio_ffmpeg bundle (present in the
    PyInstaller-packaged exe), then falls back to a direct _MEIPASS scan in
    case imageio_ffmpeg's own path resolution fails inside the bundle.
    """
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        from imageio_ffmpeg import get_ffmpeg_exe as _get
        exe = Path(_get())
        if exe.exists():
            return str(exe)
    except Exception:
        pass
    # Hard fallback: scan _MEIPASS directly (handles imageio_ffmpeg path
    # resolution failures that occur on some Windows machines in the bundle)
    return _scan_meipass_ffmpeg(name_contains="ffmpeg", exclude="ffprobe")


def get_ffprobe_exe() -> Optional[str]:
    """Return the absolute path to the ffprobe binary, or None if not found.

    imageio_ffmpeg ships ffprobe in the same directory as ffmpeg, so we
    derive the path from get_ffmpeg_exe() when system ffprobe is absent.
    """
    system = shutil.which("ffprobe")
    if system:
        return system
    # imageio_ffmpeg bundles ffprobe alongside ffmpeg
    ffmpeg = get_ffmpeg_exe()
    if ffmpeg:
        ffprobe = Path(ffmpeg).parent / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if ffprobe.exists():
            return str(ffprobe)
    return None


def get_fpcalc_exe() -> Optional[str]:
    """Return the absolute path to the Chromaprint `fpcalc` binary, or None.

    Order: explicit ANTRA_FPCALC override, system PATH, then the copy bundled
    into the PyInstaller backend under `chromaprint/`.  Returning None is a
    normal outcome -- callers must degrade to skipping the check, never fail a
    download because the binary is missing.
    """
    override = os.environ.get("ANTRA_FPCALC", "").strip()
    if override and Path(override).is_file():
        # Absolute: a relative path (or one with forward slashes) is not
        # resolvable by CreateProcess on Windows.
        return str(Path(override).resolve())
    system = shutil.which("fpcalc")
    if system:
        return system
    name = "fpcalc.exe" if os.name == "nt" else "fpcalc"
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        exe = Path(meipass) / "chromaprint" / name
        if exe.is_file():
            return str(exe)
    # Running from source: use the copy vendored for the desktop build.
    vendored = Path(__file__).resolve().parents[2] / "antra-wails" / "third_party" / "chromaprint" / name
    if vendored.is_file():
        return str(vendored.resolve())
    return None


def _is_ephemeral(path: str) -> bool:
    """True when `path` lives inside PyInstaller's _MEIPASS extraction dir.

    That directory is deleted the moment this process exits, so a path under it
    is only valid *while we are running*.  Anything that hands such a path to
    another process (the Go analyzer does exactly this) receives a dangling
    path and silently falls back to bare "ffmpeg" on PATH.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return False
    try:
        Path(path).resolve().relative_to(Path(meipass).resolve())
        return True
    except Exception:
        return False


def _stable_copy(src: Optional[str], dest_dir) -> Optional[str]:
    """Copy `src` out of the bundle into `dest_dir` and return the new path.

    A path that is already permanent (a system install) is returned unchanged —
    copying it would waste ~80 MB for nothing.
    """
    if not src:
        return None
    if not _is_ephemeral(src):
        return src
    try:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / Path(src).name
        # Idempotent: the source filename carries the ffmpeg version
        # (ffmpeg-win-x86_64-v7.1.exe), so a version bump lands as a new name
        # rather than needing an in-place overwrite of a possibly-running exe.
        if dest.exists() and dest.stat().st_size == Path(src).stat().st_size:
            return str(dest)
        tmp = dest.parent / (dest.name + ".tmp")
        shutil.copy2(src, tmp)
        if os.name != "nt":
            os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
        return str(dest)
    except Exception:
        # Returning the ephemeral path is no worse than returning nothing: the
        # caller stats it before trusting it.
        return src


def export_runtime_binaries(dest_dir) -> tuple:
    """Return (ffmpeg, ffprobe) paths that outlive this process.

    Used by the Go desktop shell, which needs a filesystem path it can exec
    later.  On a machine with a system ffmpeg this is a no-op passthrough; on a
    clean install it materialises the bundled binary into a persistent dir.
    """
    return (
        _stable_copy(get_ffmpeg_exe(), dest_dir),
        _stable_copy(get_ffprobe_exe(), dest_dir),
    )


def get_clean_subprocess_env() -> dict:
    """Return os.environ copy with the PyInstaller _MEIPASS dir stripped from
    LD_LIBRARY_PATH (and LD_PRELOAD) on Linux.

    PyInstaller extracts bundled .so files into /tmp/_MEI*/ and adds that
    directory to LD_LIBRARY_PATH.  When ffmpeg/ffprobe is spawned as a child
    process it inherits this variable, causing system libraries (e.g.
    libcurl.so.4) to load the bundled libssl.so.3 instead of the system one —
    a version mismatch that crashes ffmpeg on Fedora 43 and similar distros.
    """
    env = os.environ.copy()
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and sys.platform.startswith("linux"):
        for var in ("LD_LIBRARY_PATH", "LD_PRELOAD"):
            val = env.get(var, "")
            if not val:
                continue
            cleaned = os.pathsep.join(p for p in val.split(os.pathsep) if p != meipass)
            if cleaned:
                env[var] = cleaned
            else:
                env.pop(var, None)
    return env


def ensure_runtime_environment() -> None:
    exe = get_ffmpeg_exe()
    if not exe:
        return
    ffmpeg_dir = str(Path(exe).parent)
    current_path = os.environ.get("PATH", "")
    if ffmpeg_dir not in current_path.split(os.pathsep):
        os.environ["PATH"] = ffmpeg_dir + os.pathsep + current_path
    os.environ.setdefault("IMAGEIO_FFMPEG_EXE", exe)


def get_app_data_dir() -> Path:
    """Return a persistent, writable per-user data directory for Antra.

    Every cache/DB module in this codebase (endpoint_manifest, config,
    isrc_cache, provider_stats, lyrics_cache) used the same pattern: try
    platformdirs, and on ANY exception fall back to
    ``Path(__file__).resolve().parents[2]``. In a frozen PyInstaller build that
    fallback resolves inside ``sys._MEIPASS`` -- a fresh temp directory that is
    deleted the instant the process exits (the exact "ephemeral path" class of
    bug already fixed for ffmpeg in this file). If platformdirs ever raises
    inside the frozen exe -- our own experience this session is that
    PyInstaller's collector can silently omit a submodule from the archive with
    zero warning, and platformdirs lazily imports an OS-specific submodule
    (platformdirs.windows / .macos / .unix) the first time it is CALLED, not
    when it is imported -- every one of those five modules would silently point
    at a directory that vanishes on every relaunch. That is indistinguishable
    from "the cache/database is always empty", and for endpoint_manifest.py
    specifically it means the cache Go just wrote is never seen by Python.

    So the fallback here is a real, persistent, OS-appropriate directory
    computed WITHOUT platformdirs -- mirroring exactly what the Go desktop
    shell's own getAppDataDir() already does, so both sides converge on the
    same path even in the worst case.
    """
    try:
        from platformdirs import user_data_dir
        return Path(user_data_dir("Antra", "Antra"))
    except Exception:
        pass

    # These MUST match platformdirs' own per-platform convention, because the
    # fallback and the happy path have to resolve to the SAME directory --
    # otherwise a machine where platformdirs works and one where it does not
    # would read different caches. platformdirs uses appauthor only on Windows;
    # macOS and Linux ignore it, which is why only the Windows branch is nested.
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "Antra" / "Antra"
    elif sys.platform == "darwin":
        home = os.environ.get("HOME")
        if home:
            return Path(home) / "Library" / "Application Support" / "Antra"
    else:
        home = os.environ.get("HOME")
        if home:
            xdg = os.environ.get("XDG_DATA_HOME") or str(Path(home) / ".local" / "share")
            return Path(xdg) / "Antra"

    # Only reachable if even HOME/LOCALAPPDATA are unset, which means the OS
    # itself gave us nothing persistent to work with.
    return Path.home() / ".antra"
