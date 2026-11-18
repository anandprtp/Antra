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
import shutil
import subprocess
from typing import Optional

from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.sources.base import BaseSourceAdapter
from antra.utils.matching import duration_close, score_similarity
from antra.utils.runtime import get_ffmpeg_exe

logger = logging.getLogger(__name__)

_CLIENT_LADDER = (None, ["android"])  # None = yt-dlp defaults

# Direct InnerTube rung, tried before yt-dlp. Measured 2026-11-03: yt-dlp 2026.03 without a
# PO-token provider only streams itag 18 (~48 kbps), while the VISIONOS client returns plain
# audio URLs (itag 140 AAC ~130 kbps, 251 Opus ~136 kbps) that download in full. A `player`
# request WITHOUT a visitor id is treated as sessionless and answered "Sign in to confirm you're
# not a bot"; with one (from the web player's own sw.js_data bootstrap) 8/8 test videos played.
# shortcut: unofficial client identity, YouTube can retire it; any failure falls back to yt-dlp.
_IT_KEY = "AIzaSyC9XL3ZjWddXya6X74dJoCTL-WEYFDNX30"
_IT_UA = "com.google.ios.youtube/20.10.4 (Apple Vision Pro; U; CPU OS 1_0 like Mac OS X)"
_WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
_IT_CLIENT = {
    "clientName": "VISIONOS", "clientVersion": "1.02", "osName": "VISION_OS", "osVersion": "1.0",
    "deviceMake": "Apple", "deviceModel": "RealityDevice14,1", "platform": "MOBILE",
    "hl": "en", "gl": "US",
}
_IT_ITAGS = (140, 251)  # AAC first: an .m4a needs no extra container handling downstream
_IT_CHUNK = 4 << 20
_VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})")
_VISITOR_RE = re.compile(r"Cg[A-Za-z0-9_%-]{40,}")
_visitor_cache: dict = {"id": None, "at": 0.0}


def _visitor_id(refresh: bool = False) -> Optional[str]:
    import time
    import requests

    if not refresh and _visitor_cache["id"] and time.time() - _visitor_cache["at"] < 3600:
        return _visitor_cache["id"]
    try:
        text = requests.get(
            "https://www.youtube.com/sw.js_data", headers={"User-Agent": _WEB_UA}, timeout=15
        ).text
        found = _VISITOR_RE.search(text)
    except Exception as exc:
        logger.debug("[YouTube] visitor id fetch failed: %s", exc)
        return _visitor_cache["id"]
    if found:
        _visitor_cache["id"], _visitor_cache["at"] = found.group(0), time.time()
    return _visitor_cache["id"]


def _innertube_audio(video_url: str, tmp_stem: str) -> Optional[str]:
    """Download the best direct audio stream via InnerTube; None on ANY failure."""
    import requests

    match = _VIDEO_ID_RE.search(video_url or "")
    if not match:
        return None
    video_id = match.group(1)
    part = None
    try:
        data = None
        for attempt in (0, 1):
            visitor = _visitor_id(refresh=bool(attempt))
            if not visitor:
                return None
            body = {
                "context": {"client": dict(_IT_CLIENT, visitorData=visitor)},
                "videoId": video_id, "contentCheckOk": True, "racyCheckOk": True,
            }
            resp = requests.post(
                f"https://music.youtube.com/youtubei/v1/player?key={_IT_KEY}", json=body, timeout=20,
                headers={"User-Agent": _IT_UA, "X-Goog-Visitor-Id": visitor},
            )
            data = resp.json()
            status = (data.get("playabilityStatus") or {}).get("status")
            if status == "OK":
                break
            logger.debug("[YouTube] InnerTube %s: %s", status, (data.get("playabilityStatus") or {}).get("reason"))
            if status != "LOGIN_REQUIRED":
                return None
            data = None  # a burned visitor id is worth one fresh try
        if not data:
            return None
        formats = {f.get("itag"): f for f in (data.get("streamingData") or {}).get("adaptiveFormats", [])}
        fmt = next((formats[i] for i in _IT_ITAGS if i in formats and formats[i].get("url")), None)
        if not fmt:
            return None
        size = int(fmt["contentLength"])
        ext = "m4a" if fmt["itag"] == 140 else "webm"
        part = f"{tmp_stem}.innertube.part"
        got = 0
        with open(part, "wb") as out:
            while got < size:
                end = min(got + _IT_CHUNK, size) - 1
                chunk = requests.get(fmt["url"], headers={"Range": f"bytes={got}-{end}"}, timeout=30)
                if chunk.status_code not in (200, 206) or not chunk.content:
                    raise IOError(f"HTTP {chunk.status_code} at byte {got}/{size}")
                out.write(chunk.content)
                got += len(chunk.content)
        if got != size:
            raise IOError(f"incomplete: {got}/{size} bytes")
        final = f"{tmp_stem}.{ext}"
        os.replace(part, final)
        return final
    except Exception as exc:
        logger.debug("[YouTube] InnerTube rung failed, falling back to yt-dlp: %s", exc)
        if part and os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
        return None

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
    is_last_resort = True  # placed after all lossless adapters even in MP3 mode

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

        direct = _innertube_audio(video_url, tmp_stem)
        if direct:
            final_path = f"{output_path}.{os.path.splitext(direct)[1].lstrip('.')}"
            os.replace(direct, final_path)
            return final_path

        base_options = {
            "format": "bestaudio[acodec!=none]/bestaudio/best",
            "outtmpl": tmp_stem + ".%(ext)s",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": 3,
            "nopart": True,
        }
        # yt-dlp's solver needs a JS runtime; use node when it is on PATH (deno is the default).
        if shutil.which("node") and not shutil.which("deno"):
            base_options["js_runtimes"] = {"node": {}}

        try:
            info = None
            last_exc: Optional[Exception] = None
            # Measured 2026-11-03 (yt-dlp 2026.03.17, no PO-token provider): the default
            # client lists formats but every full download answers 403, and tv/web/ios give
            # "format not available". Only the `android` client still streams, as a muxed
            # mp4 (itag 18, AAC ~96k), so it is the second rung. shortcut: ~96k AAC ceiling,
            # lift it by adding a PO-token provider.
            for client in _CLIENT_LADDER:
                options = dict(base_options)
                if client:
                    options["extractor_args"] = {"youtube": {"player_client": client}}
                try:
                    with yt_dlp.YoutubeDL(options) as ydl:
                        info = ydl.extract_info(video_url, download=True)
                    break
                except Exception as exc:
                    last_exc = exc
                    logger.debug("[YouTube] client %s failed: %s", client or "default", exc)
                    for candidate in glob.glob(glob.escape(tmp_stem) + ".*"):
                        try:
                            os.remove(candidate)
                        except OSError:
                            pass
            if info is None:
                raise last_exc or RuntimeError("no YouTube client could download this video")
            ext = info.get("ext") or "m4a"
        except Exception as exc:
            for candidate in glob.glob(glob.escape(tmp_stem) + ".*"):   # escape: folder may contain [ ] * ?
                try:
                    os.remove(candidate)
                except OSError:
                    pass
            raise ValueError(f"[YouTube] yt-dlp download failed: {exc}") from exc

        tmp_path = f"{tmp_stem}.{ext}"
        if not os.path.exists(tmp_path):
            candidates = glob.glob(glob.escape(tmp_stem) + ".*")
            if not candidates:
                raise ValueError(f"[YouTube] Expected output file not found: {tmp_path}")
            tmp_path = candidates[0]
            ext = os.path.splitext(tmp_path)[1].lstrip(".")

        if ext in ("mp4", "3gp"):
            # Muxed video+audio (itag 18): keep only the AAC track so the engine, tagger and
            # transcoder see an ordinary audio file.
            audio_path = f"{tmp_stem}_audio.m4a"
            ffmpeg = get_ffmpeg_exe()
            if not ffmpeg:
                raise ValueError("[YouTube] ffmpeg is required to extract audio from the video stream")
            proc = subprocess.run(
                [ffmpeg, "-y", "-v", "error", "-i", tmp_path, "-vn", "-c:a", "copy", audio_path],
                capture_output=True, text=True, timeout=120,
            )
            os.remove(tmp_path)
            if proc.returncode != 0 or not os.path.exists(audio_path):
                raise ValueError(f"[YouTube] audio extraction failed: {proc.stderr.strip()[:200]}")
            tmp_path, ext = audio_path, "m4a"

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
