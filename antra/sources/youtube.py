"""
YouTube source adapter — fallback using yt-dlp.
Downloads best available audio as MP3 320kbps.
"""
import logging
import os
import shutil
import subprocess
import sys
from typing import Optional

import yt_dlp

from antra.core.models import TrackMetadata, SearchResult, AudioFormat
from antra.sources.base import BaseSourceAdapter
from antra.utils.matching import score_similarity, duration_close

logger = logging.getLogger(__name__)


class _SilentYtdlLogger:
    """Route yt-dlp output through Python logging instead of stdout."""
    def debug(self, msg):
        # yt-dlp sends download progress here — suppress it
        if msg.startswith('[download]'):
            return
        logger.debug(msg)

    def info(self, msg):
        if msg.startswith('[download]'):
            return
        logger.info(msg)

    def warning(self, msg):
        # Hide the known yt-dlp noise about missing node/deno runtimes
        ignore_keywords = [
            "No supported JavaScript runtime could be found",
            "YouTube extraction without a JS runtime",
            "deno is enabled by default",
            "js-runtimes",
            "deprecated"
        ]
        if any(kw in msg for kw in ignore_keywords):
            return
        logger.warning(msg)

    def error(self, msg):
        logger.error(msg)

MIN_SIMILARITY = 0.20


class YouTubeAdapter(BaseSourceAdapter):
    name = "youtube"
    priority = 30  # Lowest priority — only used when explicitly selected

    def __init__(self, cookiefile: Optional[str] = None, explicit_only: bool = True):
        # Cookies are opt-in. In the current environment, the local cookies.txt
        # causes some videos to fail format resolution during the download path.
        self.cookiefile = cookiefile or os.getenv("ANTRA_YOUTUBE_COOKIEFILE") or None
        # When explicit_only=True (the default), this adapter is INVISIBLE to the
        # automatic waterfall. It only activates when the user explicitly selects
        # --source youtube. This prevents YouTube from ever being a silent fallback.
        self._explicit_only = explicit_only

    def is_available(self) -> bool:
        if self._explicit_only:
            # Opt-out of the automatic waterfall entirely
            return False
        try:
            import yt_dlp  # noqa
            return True
        except ImportError:
            return False

    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        query = self._build_query(track)
        logger.debug(f"[YouTube] Searching: {query}")

        entries = self._search_entries(query)
        if entries is None:
            return None

        best: Optional[SearchResult] = None
        best_score = 0.0

        for entry in entries:
            if not entry:
                continue
            title = entry.get("title", "")
            channel = entry.get("uploader") or entry.get("channel") or entry.get("uploader_id", "")
            duration_s = entry.get("duration")

            score = score_similarity(
                query_title=track.title,
                query_artists=track.artists,
                result_title=title,
                result_artist=channel,
            )

            # Only penalize duration if we actually have it
            if duration_s and track.duration_seconds:
                if not duration_close(track.duration_seconds, float(duration_s), tolerance=15):
                    score *= 0.75

            # Penalize obvious non-matches
            lower = title.lower()
            if any(w in lower for w in ["karaoke", "cover", "tribute", "instrumental"]):
                score *= 0.5

            logger.debug(f"[YouTube] score={score:.2f} | {title}")

            if score > best_score:
                best_score = score
                video_id = entry.get("id") or entry.get("url", "")
                best = SearchResult(
                    source=self.name,
                    title=title,
                    artists=[channel],
                    album=None,
                    duration_ms=int(float(duration_s) * 1000) if duration_s else None,
                    audio_format=AudioFormat.MP3,
                    quality_kbps=320,
                    is_lossless=False,
                    download_url=None,
                    stream_id=video_id,
                    similarity_score=score,
                    artwork_url=self._artwork_url(video_id, entry),
                )

        if best and best_score >= MIN_SIMILARITY:
            logger.debug(f"[YouTube] Best match score={best_score:.2f}: {best.title}")
            return best

        # If no match above threshold, log scores and return None
        logger.warning(f"[YouTube] Best score was {best_score:.2f} (threshold={MIN_SIMILARITY}) for: {track.title}")
        return None

    def download(self, result: SearchResult, output_path: str) -> str:
        """Download audio, return path with extension.

        Tries FFmpeg post-processing to MP3 first.
        Falls back to best native audio format if FFmpeg is unavailable.
        """
        video_id = result.stream_id
        if not video_id:
            raise ValueError("No stream_id in SearchResult")

        url = video_id if video_id.startswith("http") else f"https://www.youtube.com/watch?v={video_id}"

        # --- Attempt 1: FFmpeg post-processing to MP3 ---
        if self._ffmpeg_available():
            try:
                return self._download_with_ffmpeg(url, output_path)
            except Exception as e:
                logger.warning(f"[YouTube] FFmpeg download failed ({e}), falling back to native audio.")

        # --- Attempt 2: Best native audio (no FFmpeg needed) ---
        return self._download_native(url, output_path)

    @staticmethod
    def _ffmpeg_available() -> bool:
        return shutil.which("ffmpeg") is not None

    def _download_with_ffmpeg(self, url: str, output_path: str) -> str:
        ydl_opts = self._ydl_opts(use_cookies=False)
        ydl_opts.update({
            "format": "140/139/251/250/249/bestaudio/best",
            "outtmpl": output_path + ".%(ext)s",
            "noplaylist": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",
                },
            ],
        })
        self._download_with_fallback_cookie_mode(url, ydl_opts)

        final = output_path + ".mp3"
        if not os.path.exists(final):
            raise FileNotFoundError(f"Expected MP3 not found: {final}")
        return final

    def _download_native(self, url: str, output_path: str) -> str:
        """Download best available audio without requiring FFmpeg."""
        ydl_opts = self._ydl_opts(use_cookies=False)
        ydl_opts.update({
            # Prefer M4A first so native downloads stay in a taggable audio container.
            "format": "140/139/251/250/249/bestaudio/best",
            "outtmpl": output_path + ".%(ext)s",
            "noplaylist": True,
        })
        info = self._extract_with_fallback_cookie_mode(url, ydl_opts)
        ext = info.get("ext", "webm") if info else "webm"

        final = output_path + f".{ext}"
        if not os.path.exists(final):
            # yt-dlp sometimes writes a different extension — find it
            base = output_path
            for candidate in [base + ".webm", base + ".m4a", base + ".opus", base + ".mp4", base + ".mkv"]:
                if os.path.exists(candidate):
                    return candidate
            raise FileNotFoundError(f"Downloaded file not found at expected path: {final}")
        return final

    @staticmethod
    def _build_query(track: TrackMetadata) -> str:
        import re
        title = re.sub(r'\s*\(.*?\)\s*', '', track.title).strip()
        artists = " ".join(track.artists[:2])
        return f"{title} {artists} official audio"

    @staticmethod
    def _artwork_url(video_id: str, entry: dict) -> Optional[str]:
        if video_id:
            return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

        thumbnails = entry.get("thumbnails")
        if isinstance(thumbnails, list):
            urls = [
                thumb.get("url")
                for thumb in thumbnails
                if isinstance(thumb, dict) and thumb.get("url")
            ]
            if urls:
                chosen = urls[-1]
                if chosen:
                    return chosen

        thumbnail = entry.get("thumbnail")
        if thumbnail:
            return thumbnail
        return None

    def _search_entries(self, query: str):
        for use_cookies in self._cookie_attempt_order():
            ydl_opts = self._ydl_opts(use_cookies=use_cookies)
            ydl_opts["extract_flat"] = True
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(f"ytsearch5:{query}", download=False)
                    return info.get("entries", []) if info else []
            except Exception as e:
                logger.debug(f"[YouTube] Search failed for '{query}' (cookies={use_cookies}): {e}")
        logger.warning(f"[YouTube] Search failed for '{query}'")
        return None

    def _download_with_fallback_cookie_mode(self, url: str, ydl_opts: dict):
        last_error = None
        for use_cookies in self._cookie_attempt_order():
            opts = dict(ydl_opts)
            opts.update(self._cookie_opts(use_cookies))
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                return
            except Exception as e:
                last_error = e
                logger.debug(f"[YouTube] Download failed (cookies={use_cookies}): {e}")
        raise last_error

    def _extract_with_fallback_cookie_mode(self, url: str, ydl_opts: dict):
        last_error = None
        for use_cookies in self._cookie_attempt_order():
            opts = dict(ydl_opts)
            opts.update(self._cookie_opts(use_cookies))
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(url, download=True)
            except Exception as e:
                last_error = e
                logger.debug(f"[YouTube] Extract failed (cookies={use_cookies}): {e}")
        raise last_error

    def _cookie_attempt_order(self) -> list[bool]:
        if self.cookiefile:
            return [False, True]
        return [False]

    def _ydl_opts(self, use_cookies: bool) -> dict:
        from antra.utils.runtime import get_ffmpeg_exe
        from pathlib import Path
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": _SilentYtdlLogger(),
        }
        ffmpeg = get_ffmpeg_exe()
        if ffmpeg and not shutil.which("ffmpeg"):
            opts["ffmpeg_location"] = str(Path(ffmpeg).parent)
        if sys.platform == "win32":
            # Prevent internal yt-dlp subprocesses (ffmpeg/ffprobe) from flashing console windows
            opts["subprocess_kwargs"] = {
                "creationflags": subprocess.CREATE_NO_WINDOW
            }
        opts.update(self._cookie_opts(use_cookies))
        return opts

    def _cookie_opts(self, use_cookies: bool) -> dict:
        if use_cookies and self.cookiefile:
            return {
                "cookiefile": self.cookiefile,
                "cookiefile_optional": True,
            }
        return {}
