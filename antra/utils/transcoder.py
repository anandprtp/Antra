"""
Audio transcoding helpers for user-selected output formats.
"""
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

from mutagen import File as MutagenFile

# On Windows, prevent subprocess from flashing a console window
_SUBPROCESS_FLAGS = {}
if sys.platform == "win32":
    _SUBPROCESS_FLAGS["creationflags"] = subprocess.CREATE_NO_WINDOW


OUTPUT_FORMAT_EXTENSION = {
    "source":      None,
    # Atmos keeps its own container; .mp4 never .m4a (ipod muxer rejects EC-3).
    "atmos":        None,
    "atmos-tidal":  None,
    "atmos-apple":  None,
    "atmos-amazon": None,
    "lossless":    None,
    "lossless-16": None,
    "lossless-24": None,
    "alac-16":     None,
    "alac-24":     None,
    "mp3":         ".mp3",
    "aac":         ".m4a",
    "alac":        ".m4a",
    "m4a":         ".m4a",
    "flac":        ".flac",
}


@dataclass(frozen=True)
class ConversionPlan:
    target_format: str
    extension: str
    codec_args: list[str]


class AudioTranscoder:
    _LOSSY_EXTENSIONS = {".mp3", ".aac", ".ogg", ".opus"}
    # Lossy formats that live in an MP4 container cannot be identified by
    # extension alone — .m4a is used by both ALAC (lossless) and AAC (lossy).
    _MP4_CONTAINER_EXTENSIONS = {".m4a", ".mp4"}

    def __init__(self, prevent_lossy_transcode: bool = True):
        # v1.1.8 FEAT-2 — when True, one lossy format is never re-encoded into a
        # different lossy format. Same-format output is unaffected: that path is
        # already a stream copy / remux rather than a re-encode.
        self.prevent_lossy_transcode = prevent_lossy_transcode

    def _is_lossy(self, file_path: str) -> bool:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in self._LOSSY_EXTENSIONS:
            return True
        if ext in self._MP4_CONTAINER_EXTENSIONS:
            # Judging .m4a by extension alone treated an AAC file as lossless, so
            # in lossless mode it was transcoded straight to FLAC — producing a
            # fake-lossless file. Probe the real codec instead (v1.1.8 FEAT-2/3).
            return self._mp4_is_lossy(file_path)
        return False

    @staticmethod
    def _mp4_is_lossy(file_path: str) -> bool:
        """True if an MP4-family file holds a lossy codec, False if lossless.

        Returns False ("treat as lossless") when the codec cannot be determined,
        preserving previous behaviour rather than newly rejecting files on an
        inconclusive probe.
        """
        try:
            from mutagen.mp4 import MP4
            info = getattr(MP4(file_path), "info", None)
            codec = str(getattr(info, "codec", "") or "").lower()
            if not codec:
                # ALAC reports a real bit depth; AAC reports 0/None.
                return not bool(getattr(info, "bits_per_sample", None))
            if codec.startswith("alac"):
                return False
            if codec.startswith(("mp4a", "aac")):
                return True
        except Exception:
            return False
        return False

    def _lossy_family(self, file_path: str) -> str:
        """Coarse lossy-format identity, used to tell a re-encode (different
        family) from a remux (same family)."""
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".mp3":
            return "mp3"
        if ext in {".aac", ".m4a", ".mp4"}:
            return "aac"
        if ext in {".ogg", ".opus"}:
            return "opus"
        return ext.lstrip(".")

    @staticmethod
    def _target_lossy_family(target_format: str) -> "str | None":
        base = (target_format or "").lower()
        if base == "mp3":
            return "mp3"
        if base in {"aac", "m4a"}:
            return "aac"
        return None

    def blocks_lossy_reencode(self, file_path: str, target_format: str) -> bool:
        """True when this conversion would be a lossy → different-lossy re-encode
        and FEAT-2 is enabled. Requesting MP3 and receiving AAC is the reported
        case: re-encoding it is pure generation loss."""
        if not self.prevent_lossy_transcode:
            return False
        target_family = self._target_lossy_family(target_format)
        if target_family is None:
            return False  # lossless target — encoding once is correct
        if not self._is_lossy(file_path):
            return False  # lossless source — encoding once is correct
        return self._lossy_family(file_path) != target_family

    def needs_conversion(self, file_path: str, target_format: str, allow_lossy_reencode: bool = False) -> bool:
        # Normalise bit-depth variants to their base format for conversion logic.
        # lossless-16 / lossless-24 → lossless, alac-16 / alac-24 → alac
        base_format = target_format.split("-")[0] if target_format.endswith(("-16", "-24")) else target_format
        # 16-bit output requested: a >16-bit lossless source must be downsampled.
        # Native 16-bit from Tidal is unavailable (LOSSLESS returns AAC on our pool),
        # so the only reliable way to deliver 16-bit is to downsample the 24-bit FLAC.
        wants_16 = target_format.endswith("-16")

        if base_format == "source":
            return False

        # Dolby Atmos is delivered as E-AC-3/AC-4 in an .mp4 and must be passed
        # through untouched (v1.1.8 FEAT-1). ffmpeg's `ipod` muxer refuses these
        # codecs outright, and re-encoding would destroy the spatial mix.
        if base_format.startswith("atmos"):
            return False

        # FEAT-2: refuse a lossy → different-lossy re-encode. Checked before the
        # per-format branches below, which otherwise return True unconditionally
        # for aac/m4a targets and would happily re-encode an AAC source to MP3.
        # allow_lossy_reencode: the source is a last resort (YouTube), so nothing better exists
        # and delivering the requested container beats handing back an .m4a for an MP3 job.
        if not allow_lossy_reencode and self.blocks_lossy_reencode(file_path, base_format):
            return False
        if base_format == "lossless":
            ext = os.path.splitext(file_path)[1].lower()
            if ext == ".m4a":
                return True
            if wants_16 and not self._is_lossy(file_path) and (self._source_bit_depth(file_path) or 24) > 16:
                return True
            return False
        if base_format == "flac":
            if self._is_lossy(file_path):
                return False
            ext = os.path.splitext(file_path)[1].lower()
            if ext != ".flac":
                return True
            return wants_16 and (self._source_bit_depth(file_path) or 24) > 16

        if base_format == "alac":
            if self._is_lossy(file_path):
                return False
            ext = os.path.splitext(file_path)[1].lower()
            if ext == ".flac":
                return True
            return wants_16 and (self._source_bit_depth(file_path) or 24) > 16

        if base_format == "aac":
            return True

        if base_format == "m4a":
            return True
        ext = os.path.splitext(file_path)[1].lower()
        target_ext = OUTPUT_FORMAT_EXTENSION.get(target_format)
        return target_ext is not None and ext != target_ext

    # Standard encoder steps. A lossy source of N kbps is re-encoded at the first step that is not
    # below N-4 (the 4 kbps absorbs VBR/measurement noise: 128.4 -> 128, 131 -> 128, 136 -> 160),
    # never at the configured ceiling: a 128 kbps stream re-encoded at 320 is ~2.5x the size for
    # no extra audio.
    _BITRATE_STEPS = (48, 64, 96, 128, 160, 192, 224, 256, 320)

    @classmethod
    def cap_kbps(cls, configured_kbps: int, source_kbps: Optional[int]) -> int:
        """Encode bitrate for a lossy -> lossy conversion: the configured one, but never more
        than the source warrants. Unknown source bitrate leaves the configured value alone."""
        if not source_kbps or source_kbps <= 0:
            return configured_kbps
        step = next((s for s in cls._BITRATE_STEPS if s >= source_kbps - 4), cls._BITRATE_STEPS[-1])
        return min(configured_kbps, step)

    def convert(self, file_path: str, target_format: str, allow_lossy_reencode: bool = False,
                max_source_kbps: Optional[int] = None) -> str:
        """`max_source_kbps` is the bitrate of a LOSSY source (None for lossless or unknown); it
        caps the bitrate of any lossy re-encode, see [cap_kbps]."""
        if target_format == "source":
            return file_path
        if not self.needs_conversion(file_path, target_format, allow_lossy_reencode):
            return file_path
        from antra.utils.runtime import get_ffmpeg_exe, get_clean_subprocess_env
        ffmpeg = get_ffmpeg_exe()
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required for output format conversion")

        plan = self._plan(target_format, file_path=file_path, max_source_kbps=max_source_kbps)
        base, _ = os.path.splitext(file_path)
        temp_output = base + f".antra-convert{plan.extension}"
        final_output = base + plan.extension

        if os.path.exists(temp_output):
            os.remove(temp_output)

        command = [
            ffmpeg,
            "-y",
            "-i",
            file_path,
            "-vn",
            *plan.codec_args,
            temp_output,
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=240,
                                    env=get_clean_subprocess_env(), **_SUBPROCESS_FLAGS)
            if result.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg conversion to {target_format} failed: {result.stderr.strip() or result.stdout.strip()}"
                )

            # ffmpeg can exit 0 and still leave nothing usable; never swap that in over the source.
            if not os.path.exists(temp_output) or os.path.getsize(temp_output) == 0:
                raise RuntimeError(f"ffmpeg conversion to {target_format} produced an empty file")
            # os.replace overwrites atomically. This used to delete the source (same-extension
            # conversion, e.g. 24-bit -> 16-bit FLAC) or the existing target FIRST, so any
            # failure of the rename afterwards (a Windows lock, antivirus) lost the only copy.
            os.replace(temp_output, final_output)
            if os.path.exists(file_path) and os.path.normcase(file_path) != os.path.normcase(final_output):
                os.remove(file_path)
        except Exception:
            if os.path.exists(temp_output):
                try:
                    os.remove(temp_output)
                except OSError:
                    pass
            raise
        return final_output

    @classmethod
    def _plan(cls, target_format: str, file_path: str = "", max_source_kbps: Optional[int] = None) -> ConversionPlan:
        # Normalise bit-depth variants to base format
        base_format = target_format.split("-")[0] if target_format.endswith(("-16", "-24")) else target_format
        # For a "-16" request, force 16-bit output (ffmpeg dithers on bit-depth
        # reduction by default). "-24"/no-suffix keep the source depth.
        depth_args = ["-sample_fmt", "s16"] if target_format.endswith("-16") else []

        if base_format in ("lossless", "flac"):
            return ConversionPlan(
                target_format="flac",
                extension=".flac",
                codec_args=["-c:a", "flac", *depth_args],
            )
        if base_format == "mp3":
            return ConversionPlan(
                target_format=base_format,
                extension=".mp3",
                codec_args=["-c:a", "libmp3lame", "-b:a", f"{cls.cap_kbps(320, max_source_kbps)}k"],
            )
        if base_format == "alac":
            return ConversionPlan(
                target_format=base_format,
                extension=".m4a",
                codec_args=["-c:a", "alac", *depth_args],
            )
        if base_format == "aac":
            if AudioTranscoder._can_normalize_aac_container(file_path):
                return ConversionPlan(
                    target_format=base_format,
                    extension=".m4a",
                    codec_args=["-c:a", "copy", "-f", "ipod", "-movflags", "+faststart"],
                )
            return ConversionPlan(
                target_format=base_format,
                extension=".m4a",
                codec_args=["-c:a", "aac", "-b:a", f"{cls.cap_kbps(320, max_source_kbps)}k", "-f", "ipod", "-movflags", "+faststart"],
            )
        if base_format == "m4a":
            if AudioTranscoder._can_normalize_aac_container(file_path):
                return ConversionPlan(
                    target_format=base_format,
                    extension=".m4a",
                    codec_args=["-c:a", "copy", "-f", "ipod", "-movflags", "+faststart"],
                )
            return ConversionPlan(
                target_format=base_format,
                extension=".m4a",
                codec_args=["-c:a", "aac", "-b:a", f"{cls.cap_kbps(256, max_source_kbps)}k", "-f", "ipod", "-movflags", "+faststart"],
            )
        raise ValueError(f"Unsupported output format: {target_format}")

    @staticmethod
    def _can_normalize_aac_container(file_path: str) -> bool:
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".aac":
            return True
        if ext not in {".m4a", ".mp4"}:
            return False
        codec = AudioTranscoder._probe_codec_name(file_path)
        if not codec:
            return True
        return "alac" not in codec

    @staticmethod
    def _probe_codec_name(file_path: str) -> str:
        try:
            audio = MutagenFile(file_path)
        except Exception:
            return ""
        info = getattr(audio, "info", None)
        codec = getattr(info, "codec", "") if info else ""
        return str(codec or "").lower()

    @staticmethod
    def _source_bit_depth(file_path: str) -> int | None:
        """Bits-per-sample of a lossless source (FLAC/ALAC), or None if unknown.
        Used to decide whether a 16-bit request needs a downsample pass."""
        try:
            audio = MutagenFile(file_path)
        except Exception:
            return None
        info = getattr(audio, "info", None)
        depth = getattr(info, "bits_per_sample", None) if info else None
        try:
            return int(depth) if depth else None
        except (TypeError, ValueError):
            return None
