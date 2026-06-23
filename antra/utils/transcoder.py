"""
Audio transcoding helpers for user-selected output formats.
"""
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

from mutagen import File as MutagenFile

# On Windows, prevent subprocess from flashing a console window
_SUBPROCESS_FLAGS = {}
if sys.platform == "win32":
    _SUBPROCESS_FLAGS["creationflags"] = subprocess.CREATE_NO_WINDOW


OUTPUT_FORMAT_EXTENSION = {
    "source":      None,
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
    _LOSSY_EXTENSIONS = {".mp3", ".aac"}

    def _is_lossy(self, file_path: str) -> bool:
        return os.path.splitext(file_path)[1].lower() in self._LOSSY_EXTENSIONS

    def needs_conversion(self, file_path: str, target_format: str) -> bool:
        # Normalise bit-depth variants to their base format for conversion logic.
        # lossless-16 / lossless-24 → lossless, alac-16 / alac-24 → alac
        base_format = target_format.split("-")[0] if target_format.endswith(("-16", "-24")) else target_format
        # 16-bit output requested: a >16-bit lossless source must be downsampled.
        # Native 16-bit from Tidal is unavailable (LOSSLESS returns AAC on our pool),
        # so the only reliable way to deliver 16-bit is to downsample the 24-bit FLAC.
        wants_16 = target_format.endswith("-16")

        if base_format == "source":
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

    def convert(self, file_path: str, target_format: str) -> str:
        if target_format == "source":
            return file_path
        if not self.needs_conversion(file_path, target_format):
            return file_path
        from antra.utils.runtime import get_ffmpeg_exe, get_clean_subprocess_env
        ffmpeg = get_ffmpeg_exe()
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required for output format conversion")

        plan = self._plan(target_format, file_path=file_path)
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

            if os.path.exists(final_output) and os.path.normcase(final_output) != os.path.normcase(file_path):
                os.remove(final_output)
            if os.path.normcase(final_output) == os.path.normcase(file_path):
                os.remove(file_path)
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

    @staticmethod
    def _plan(target_format: str, file_path: str = "") -> ConversionPlan:
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
                codec_args=["-c:a", "libmp3lame", "-b:a", "320k"],
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
                codec_args=["-c:a", "aac", "-b:a", "320k", "-f", "ipod", "-movflags", "+faststart"],
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
                codec_args=["-c:a", "aac", "-b:a", "256k", "-f", "ipod", "-movflags", "+faststart"],
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
