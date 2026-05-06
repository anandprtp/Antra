"""
YouTube source adapter backed by yt-dlp.

Intended as a strict lossy fallback when the preferred streaming source cannot
produce a usable file. Search matching is intentionally conservative so we fail
cleanly rather than downloading a wrong recording for common song titles.
"""
import glob
import logging
import os
import re
from typing import Optional

from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.sources.base import BaseSourceAdapter
from antra.utils.matching import duration_close, score_similarity

logger = logging.getLogger(__name__)

MIN_SIMILARITY = 0.58
STRICT_DURATION_TOLERANCE_SECONDS = 8

_BAD_VARIANT_RE = re.compile(
    r"\b("
    r"cover|karaoke|instrumental|tribute|remix|nightcore|sped\s*up|slowed|reverb|8d|"
    r"bass\s*boosted|live|concert|reaction|fan\s*made|demo|teaser|shorts?"
    r")\b",
    re.IGNORECASE,
)


class YouTubeAdapter(BaseSourceAdapter):
    name = "youtube"
    priority = 5
    always_lossy = True

    def is_available(self) -> bool:
        try:
            import yt_dlp  # noqa: F401
            return True
        except ImportError:
            return False

    def should_retry_download(self, result, error: Exception) -> bool:
        message = str(error).lower()
        return "copyright" not in message and "private video" not in message

    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        try:
            import yt_dlp
        except ImportError:
            return None

        best: Optional[SearchResult] = None
        best_score = 0.0

        for query in self._build_queries(track):
            options = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": True,
                "skip_download": True,
                "noplaylist": True,
            }
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(f"ytsearch6:{query}", download=False)
            except Exception as exc:
                logger.debug("[YouTube] Search failed for %r: %s", query, exc)
                continue

            entries = info.get("entries") or []
            for entry in entries:
                result = self._entry_to_result(track, entry)
                if result is None:
                    continue
                if result.similarity_score > best_score:
                    best = result
                    best_score = result.similarity_score
                if result.isrc_match or result.similarity_score >= 0.92:
                    return result

        if best and best_score >= MIN_SIMILARITY:
            return best
        return None

    def download(self, result: SearchResult, output_path: str) -> str:
        import yt_dlp

        video_url = result.stream_id or result.download_url
        if not video_url:
            raise ValueError("[YouTube] Missing video URL for download")

        tmp_dir = os.path.dirname(output_path)
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_stem = os.path.join(tmp_dir, f"_yt_tmp_{self._safe_stem(result.stream_id or result.title)}")

        options = {
            "format": "bestaudio[acodec!=none]/bestaudio/best",
            "outtmpl": tmp_stem + ".%(ext)s",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": 3,
            "nopart": True,
        }

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(video_url, download=True)
                ext = info.get("ext") or "m4a"
        except Exception as exc:
            for candidate in glob.glob(tmp_stem + ".*"):
                try:
                    os.remove(candidate)
                except OSError:
                    pass
            raise ValueError(f"[YouTube] yt-dlp download failed: {exc}") from exc

        tmp_path = f"{tmp_stem}.{ext}"
        if not os.path.exists(tmp_path):
            candidates = glob.glob(tmp_stem + ".*")
            if not candidates:
                raise ValueError(f"[YouTube] Expected output file not found: {tmp_path}")
            tmp_path = candidates[0]
            ext = os.path.splitext(tmp_path)[1].lstrip(".")

        final_path = f"{output_path}.{ext}"
        os.replace(tmp_path, final_path)
        return final_path

    def _entry_to_result(self, track: TrackMetadata, entry: dict) -> Optional[SearchResult]:
        video_id = (entry.get("id") or "").strip()
        title = (entry.get("title") or "").strip()
        if not video_id or not title:
            return None

        uploader = (entry.get("uploader") or entry.get("channel") or entry.get("channel_name") or "").strip()
        duration_s = entry.get("duration")
        similarity = self._score(track, title, uploader, duration_s)
        if similarity <= 0:
            return None

        webpage_url = entry.get("url") or entry.get("webpage_url")
        if webpage_url and not str(webpage_url).startswith("http"):
            webpage_url = f"https://www.youtube.com/watch?v={video_id}"
        elif not webpage_url:
            webpage_url = f"https://www.youtube.com/watch?v={video_id}"

        thumbnail = None
        thumbnails = entry.get("thumbnails") or []
        if thumbnails:
            thumbnail = thumbnails[-1].get("url")
        if not thumbnail:
            thumbnail = entry.get("thumbnail")

        abr = entry.get("abr")
        audio_format = AudioFormat.OPUS
        ext = (entry.get("audio_ext") or entry.get("ext") or "").lower()
        if ext in {"m4a", "aac"}:
            audio_format = AudioFormat.AAC
        elif ext == "mp3":
            audio_format = AudioFormat.MP3

        return SearchResult(
            source=self.name,
            title=title,
            artists=[uploader] if uploader else [],
            album=None,
            duration_ms=int(float(duration_s) * 1000) if duration_s else None,
            audio_format=audio_format,
            quality_kbps=int(round(float(abr))) if isinstance(abr, (int, float)) else None,
            is_lossless=False,
            download_url=webpage_url,
            stream_id=webpage_url,
            similarity_score=similarity,
            isrc_match=False,
            artwork_url=thumbnail,
        )

    def _score(
        self,
        track: TrackMetadata,
        result_title: str,
        uploader: str,
        duration_s: Optional[float],
    ) -> float:
        artist_blob = uploader
        score = max(
            (
                score_similarity(
                    query_title=variant,
                    query_artists=track.artists,
                    result_title=result_title,
                    result_artist=artist_blob,
                )
                for variant in self._title_variants(track.title)
            ),
            default=0.0,
        )

        normalized_title = result_title.lower()
        if _BAD_VARIANT_RE.search(normalized_title) and not _BAD_VARIANT_RE.search(track.title.lower()):
            score -= 0.28

        if duration_s and track.duration_seconds:
            if duration_close(track.duration_seconds, float(duration_s), tolerance=STRICT_DURATION_TOLERANCE_SECONDS):
                score += 0.05
            else:
                delta = abs(track.duration_seconds - float(duration_s))
                if delta >= 20:
                    return 0.0
                score *= 0.65

        return max(score, 0.0)

    def _build_queries(self, track: TrackMetadata) -> list[str]:
        queries: list[str] = []
        seen: set[str] = set()
        primary_artist = track.primary_artist

        def add(query: str) -> None:
            query = re.sub(r"\s+", " ", query).strip()
            if query and query not in seen:
                seen.add(query)
                queries.append(query)

        for variant in self._title_variants(track.title):
            add(f"{primary_artist} {variant} official audio")
            add(f"{primary_artist} {variant}")
            if track.album:
                add(f"{primary_artist} {variant} {track.album}")
        if track.isrc:
            add(f"{track.isrc} {primary_artist} {track.title}")
        return queries or [f"{primary_artist} {track.title}"]

    @staticmethod
    def _title_variants(title: str) -> list[str]:
        variants: list[str] = []

        def add(value: str) -> None:
            value = re.sub(r"\s+", " ", value).strip()
            if value and value not in variants:
                variants.append(value)

        add(title)
        add(re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", title))
        add(re.sub(r"\s*(feat\.?|ft\.?|with)\s+.*$", "", title, flags=re.IGNORECASE))
        return variants

    @staticmethod
    def _safe_stem(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", value or "youtube")
