"""
Audio transcoding helpers for user-selected output formats.
"""
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

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

        if base_format == "source":
            return False
        if base_format == "lossless":
            ext = os.path.splitext(file_path)[1].lower()
            if ext == ".m4a":
                return True
            return False
        if base_format == "flac":
            if self._is_lossy(file_path):
                return False
            ext = os.path.splitext(file_path)[1].lower()
            return ext not in {".flac"}

        if base_format == "alac":
            if self._is_lossy(file_path):
                return False
            ext = os.path.splitext(file_path)[1].lower()
            return ext == ".flac"

        if base_format == "aac":
            ext = os.path.splitext(file_path)[1].lower()
            return ext not in {".m4a", ".aac"}

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

        plan = self._plan(target_format)
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
        return final_output

    @staticmethod
    def _plan(target_format: str) -> ConversionPlan:
        # Normalise bit-depth variants to base format
        base_format = target_format.split("-")[0] if target_format.endswith(("-16", "-24")) else target_format

        if base_format in ("lossless", "flac"):
            return ConversionPlan(
                target_format="flac",
                extension=".flac",
                codec_args=["-c:a", "flac"],
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
                codec_args=["-c:a", "alac"],
            )
        if base_format == "aac":
            return ConversionPlan(
                target_format=base_format,
                extension=".m4a",
                codec_args=["-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart"],
            )
        if base_format == "m4a":
            return ConversionPlan(
                target_format=base_format,
                extension=".m4a",
                codec_args=["-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart"],
            )
        raise ValueError(f"Unsupported output format: {target_format}")
