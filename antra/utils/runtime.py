from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional


def get_ffmpeg_exe() -> Optional[str]:
    """Return the absolute path to the ffmpeg binary, or None if not found.

    Checks system PATH first, then falls back to the imageio_ffmpeg bundle
    (present in the PyInstaller-packaged exe).
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
    return None


def ensure_runtime_environment() -> None:
    exe = get_ffmpeg_exe()
    if not exe:
        return
    ffmpeg_dir = str(Path(exe).parent)
    current_path = os.environ.get("PATH", "")
    if ffmpeg_dir not in current_path.split(os.pathsep):
        os.environ["PATH"] = ffmpeg_dir + os.pathsep + current_path
    os.environ.setdefault("IMAGEIO_FFMPEG_EXE", exe)
