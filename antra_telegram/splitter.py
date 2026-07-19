import os
import subprocess
from pathlib import Path

from antra.utils.runtime import get_clean_subprocess_env, get_ffmpeg_exe


class AudioSplitError(RuntimeError):
    pass


def estimate_segment_seconds(
    *,
    size_bytes: int,
    duration_seconds: float,
    max_part_bytes: int,
) -> int:
    if size_bytes <= 0 or duration_seconds <= 0 or max_part_bytes <= 0:
        raise AudioSplitError("invalid media size or duration")
    # Target 82% of Telegram's upload ceiling. The extra margin absorbs VBR
    # peaks and container overhead so no generated part crosses the hard limit.
    ratio = min(1.0, (max_part_bytes * 0.82) / size_bytes)
    return max(60, int(duration_seconds * ratio))


def split_audio_copy(
    source: Path,
    output_dir: Path,
    *,
    duration_seconds: float,
    max_part_bytes: int,
) -> list[Path]:
    ffmpeg = get_ffmpeg_exe()
    if not ffmpeg:
        raise AudioSplitError("ffmpeg is required to split large audio")
    source = source.expanduser().resolve(strict=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower()
    if suffix not in {".webm", ".mp3", ".m4a", ".ogg", ".opus", ".aac"}:
        raise AudioSplitError(f"unsupported split container: {suffix or 'unknown'}")

    segment_seconds = estimate_segment_seconds(
        size_bytes=source.stat().st_size,
        duration_seconds=duration_seconds,
        max_part_bytes=max_part_bytes,
    )
    for _attempt in range(4):
        for existing in output_dir.glob("part-*" + suffix):
            existing.unlink(missing_ok=True)
        template = output_dir / f"part-%03d{suffix}"
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-c:a",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(segment_seconds),
            "-reset_timestamps",
            "1",
            str(template),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=180,
                env=get_clean_subprocess_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise AudioSplitError("audio splitting timed out") from exc
        if result.returncode != 0:
            raise AudioSplitError(
                result.stderr.strip() or result.stdout.strip() or "ffmpeg split failed"
            )

        parts = sorted(output_dir.glob("part-*" + suffix))
        if parts and all(0 < part.stat().st_size <= max_part_bytes for part in parts):
            return parts
        segment_seconds = max(60, int(segment_seconds * 0.7))

    oversized = max(
        (part.stat().st_size for part in output_dir.glob("part-*" + suffix)),
        default=0,
    )
    raise AudioSplitError(
        f"could not keep split parts below upload limit (largest={oversized})"
    )
