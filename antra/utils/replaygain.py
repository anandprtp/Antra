"""v2.0.0 SF-2 - ReplayGain 2.0 analysis and tags.

One ffmpeg decode per track produces everything needed:

* ``ebur128=metadata=1`` + ``ametadata=print`` emits ``lavfi.r128.M`` every
  100 ms. Each value is the loudness of a 400 ms window with a 100 ms hop -
  which is exactly the ITU-R BS.1770 gating block. Keeping those blocks lets
  ALBUM loudness be computed by gating the union of every track's blocks, with
  no second decode and no concatenation of files. That is the whole reason the
  blocks are kept rather than only the track's integrated value: averaging
  per-track LUFS numbers is not the same measurement and is not what the spec
  describes.
* ``astats`` gives the sample peak to 6 decimals. ebur128's own
  ``lavfi.r128.sample_peak`` is printed with 3, which is too coarse for a tag
  whose whole job is clipping prevention near 1.0.

Reference level is ReplayGain 2.0's -18 LUFS: gain = -18 - integrated.

Nothing in this module may fail a download. ``measure`` returns None on any
problem and ``write_tags`` returns False; callers log and carry on.
"""

from __future__ import annotations

import array
import logging
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

REFERENCE_LUFS = -18.0
ABSOLUTE_GATE_LUFS = -70.0
RELATIVE_GATE_LU = -10.0
# The K-weighting offset in BS.1770's loudness definition. It cancels out of
# the gating arithmetic but must be applied to turn energies back into LUFS.
_K_OFFSET = -0.691

# Containers the tagger writes. Everything else is skipped rather than guessed:
# an .opus file, for instance, would want R128_TRACK_GAIN in Q7.8 relative to
# -23 LUFS, and the tagger does not write .opus at all today.
SUPPORTED_EXTENSIONS = frozenset({".flac", ".mp3", ".m4a", ".mp4"})

TRACK_GAIN = "REPLAYGAIN_TRACK_GAIN"
TRACK_PEAK = "REPLAYGAIN_TRACK_PEAK"
ALBUM_GAIN = "REPLAYGAIN_ALBUM_GAIN"
ALBUM_PEAK = "REPLAYGAIN_ALBUM_PEAK"

# Decoding is fast (hundreds of x realtime for FLAC) but a stuck ffmpeg must
# never hold a download worker forever.
MEASURE_TIMEOUT_S = 300

_SUBPROCESS_FLAGS: dict = {}
if sys.platform == "win32":
    _SUBPROCESS_FLAGS["creationflags"] = subprocess.CREATE_NO_WINDOW

_M_LINE = re.compile(r"^lavfi\.r128\.M=(\S+)\s*$", re.MULTILINE)
_PEAK_LINE = re.compile(r"Peak level dB:\s*(\S+)")


@dataclass(frozen=True)
class Loudness:
    integrated_lufs: float
    peak: float                    # linear sample peak, 0..1+ (inter-sample overs allowed)
    # Momentary loudness per 100 ms gating block, LUFS. A compact float64
    # array (8 bytes/block) rather than a tuple of Python floats (~32).
    blocks: array.array

    @property
    def gain_db(self) -> float:
        return gain_for(self.integrated_lufs)


# ── Pure math ──────────────────────────────────────────────────────────────

def _energy(lufs: float) -> float:
    return 10.0 ** ((lufs - _K_OFFSET) / 10.0)


def _lufs(energy: float) -> float:
    return _K_OFFSET + 10.0 * math.log10(energy)


def integrated_from_blocks(blocks: Iterable[float]) -> Optional[float]:
    """BS.1770-4 integrated loudness from momentary gating blocks.

    Two-stage gate: drop blocks at or below -70 LUFS, then drop blocks more
    than 10 LU below the mean of what survived. Returns None when nothing
    passes the absolute gate - a silent file has no meaningful loudness, and
    writing a +52 dB gain for it would be actively harmful.
    """
    passed = [b for b in blocks if math.isfinite(b) and b > ABSOLUTE_GATE_LUFS]
    if not passed:
        return None
    relative = _lufs(sum(_energy(b) for b in passed) / len(passed)) + RELATIVE_GATE_LU
    kept = [b for b in passed if b > relative]
    if not kept:
        return None
    return _lufs(sum(_energy(b) for b in kept) / len(kept))


def gain_for(integrated_lufs: float) -> float:
    return REFERENCE_LUFS - integrated_lufs


def album_loudness(measurements: Sequence[Loudness]) -> Optional[tuple[float, float]]:
    """(integrated LUFS, peak) for a release, gated over every track's blocks."""
    if not measurements:
        return None
    blocks: list[float] = []
    for m in measurements:
        blocks.extend(m.blocks)
    lufs = integrated_from_blocks(blocks)
    if lufs is None:
        return None
    return lufs, max(m.peak for m in measurements)


def format_gain(db: float) -> str:
    return f"{db:+.2f} dB"


def format_peak(peak: float) -> str:
    return f"{peak:.6f}"


def parse_blocks(stdout: str) -> list[float]:
    out: list[float] = []
    for raw in _M_LINE.findall(stdout or ""):
        try:
            out.append(float(raw))
        except ValueError:
            continue
    return out


def parse_peak(stderr: str) -> Optional[float]:
    """Linear sample peak from astats' summary, or None when absent/-inf."""
    found = _PEAK_LINE.findall(stderr or "")
    if not found:
        return None
    try:
        db = float(found[-1])
    except ValueError:
        return None
    if not math.isfinite(db):
        return None
    return 10.0 ** (db / 20.0)


# ── Measurement ────────────────────────────────────────────────────────────

def _ffmpeg() -> Optional[str]:
    from antra.utils.runtime import get_ffmpeg_exe
    import shutil
    return get_ffmpeg_exe() or shutil.which("ffmpeg")


def measure(path: str, timeout: int = MEASURE_TIMEOUT_S) -> Optional[Loudness]:
    """Decode `path` once and return its loudness, or None on any failure."""
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        logger.debug("[RG] ffmpeg not found - ReplayGain skipped")
        return None
    cmd = [
        ffmpeg, "-hide_banner", "-nostats", "-nostdin",
        "-i", path, "-vn",
        "-af",
        "ebur128=metadata=1,"
        "ametadata=mode=print:key=lavfi.r128.M:file=-,"
        "astats=measure_perchannel=none:measure_overall=Peak_level",
        "-f", "null", "-",
    ]
    try:
        from antra.utils.runtime import get_clean_subprocess_env
        env = get_clean_subprocess_env()
    except Exception:
        env = None
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env, **_SUBPROCESS_FLAGS,
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"[RG] Loudness analysis timed out after {timeout}s: {os.path.basename(path)}")
        return None
    except Exception as e:
        logger.debug(f"[RG] Could not run ffmpeg for {path}: {e}")
        return None
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        logger.debug(f"[RG] ffmpeg exit {proc.returncode} for {path}: {tail[0]}")
        return None

    blocks = parse_blocks(proc.stdout)
    integrated = integrated_from_blocks(blocks)
    peak = parse_peak(proc.stderr)
    if integrated is None or peak is None:
        logger.debug(f"[RG] No usable loudness for {path} (silent or unparseable)")
        return None
    return Loudness(integrated_lufs=integrated, peak=peak, blocks=array.array("d", blocks))


# ── Tag writing ────────────────────────────────────────────────────────────

def write_tags(
    path: str,
    *,
    track: Optional[tuple[float, float]] = None,
    album: Optional[tuple[float, float]] = None,
    clear_album: bool = False,
    id3_v1: int = 0,
) -> bool:
    """Write (gain_db, peak) pairs in each container's convention.

    `clear_album` removes existing album tags. It is used on the track pass:
    a freshly downloaded file can only have inherited album values it did not
    earn, and a stale album gain is worse than none - players fall back to the
    track gain when the album tags are absent.

    FLAC: Vorbis comments. MP3: TXXX frames, saved as ID3v2.3 like the rest of
    the tagger so Windows Explorer keeps reading the cover. MP4: the
    `----:com.apple.iTunes:replaygain_*` freeform atoms foobar2000 and beets
    write. Existing variants in another letter case are replaced, not
    duplicated.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return False
    values: dict[str, str] = {}
    if track is not None:
        values[TRACK_GAIN] = format_gain(track[0])
        values[TRACK_PEAK] = format_peak(track[1])
    if album is not None:
        values[ALBUM_GAIN] = format_gain(album[0])
        values[ALBUM_PEAK] = format_peak(album[1])
    removes = set(values)
    if clear_album and album is None:
        removes |= {ALBUM_GAIN, ALBUM_PEAK}
    if not removes:
        return True
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            audio = FLAC(path)
            for key in removes:
                if key in audio:
                    del audio[key]
            for key, value in values.items():
                audio[key] = value
            audio.save()
        elif ext == ".mp3":
            from mutagen.id3 import ID3, TXXX, ID3NoHeaderError
            try:
                audio = ID3(path)
            except ID3NoHeaderError:
                audio = ID3()
            for frame_key in list(audio.keys()):
                if frame_key.startswith("TXXX:") and frame_key[5:].upper() in removes:
                    del audio[frame_key]
            for key, value in values.items():
                audio.add(TXXX(encoding=3, desc=key, text=[value]))
            audio.save(path, v1=id3_v1, v2_version=3)
        else:
            from mutagen.mp4 import MP4
            audio = MP4(path)
            prefix = "----:com.apple.iTunes:"
            for atom in list(audio.keys()):
                if atom.startswith(prefix) and atom[len(prefix):].upper() in removes:
                    del audio[atom]
            for key, value in values.items():
                audio[prefix + key.lower()] = [value.encode("utf-8")]
            audio.save()
        return True
    except Exception as e:
        logger.debug(f"[RG] Could not write ReplayGain tags to {path}: {e}")
        return False
