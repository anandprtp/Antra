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
