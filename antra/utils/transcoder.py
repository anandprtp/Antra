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
    "source": None,
    "lossless": None,
    "mp3": ".mp3",
    "aac": ".aac",
    "m4a": ".m4a",
    "flac": ".flac",
}


@dataclass(frozen=True)
class ConversionPlan:
    target_format: str
    extension: str
    codec_args: list[str]


class AudioTranscoder:
    def needs_conversion(self, file_path: str, target_format: str) -> bool:
        if target_format == "source":
            return False
        if target_format == "lossless":
            ext = os.path.splitext(file_path)[1].lower()
            return ext not in {".flac", ".m4a"}

        ext = os.path.splitext(file_path)[1].lower()
        target_ext = OUTPUT_FORMAT_EXTENSION[target_format]
        return ext != target_ext

    def convert(self, file_path: str, target_format: str) -> str:
        if target_format == "source":
            return file_path
        if target_format == "lossless":
            if not self.needs_conversion(file_path, target_format):
                return file_path
            target_format = "flac"
        if not self.needs_conversion(file_path, target_format):
            return file_path
        from antra.utils.runtime import get_ffmpeg_exe
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
        result = subprocess.run(command, capture_output=True, text=True, timeout=240, **_SUBPROCESS_FLAGS)
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
        if target_format == "mp3":
            return ConversionPlan(
                target_format=target_format,
                extension=".mp3",
                codec_args=["-c:a", "libmp3lame", "-b:a", "320k"],
            )
        if target_format == "aac":
            return ConversionPlan(
                target_format=target_format,
                extension=".aac",
                codec_args=["-c:a", "aac", "-b:a", "256k"],
            )
        if target_format == "m4a":
            return ConversionPlan(
                target_format=target_format,
                extension=".m4a",
                codec_args=["-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart"],
            )
        if target_format == "flac":
            return ConversionPlan(
                target_format=target_format,
                extension=".flac",
                codec_args=["-c:a", "flac"],
            )
        raise ValueError(f"Unsupported output format: {target_format}")
