"""
NetEase Cloud Music source adapter (网易云音乐).

Provides audio downloads for Chinese and other tracks with no account
required. Uses pyncm for catalog search and yt-dlp (already a project
dependency) for audio extraction — yt-dlp's NetEase extractor handles
the encrypted URL retrieval cleanly without needing a VIP account for
the portion of the catalog that is freely streamable.

Priority 4 — same tier as JioSaavn; inserted before it in build_adapters()
so the stable sort places NetEase first in this tier.

always_lossy = True because NetEase's freely streamable tier only provides
MP3 (128/320kbps). The VIP-only lossless tier is not accessible without an
authenticated session, which this adapter does not support. Skipped entirely
when the resolver is in lossless-only mode.
"""
import glob
import logging
import os
import re
import tempfile
from typing import Optional

import requests

from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.sources.base import BaseSourceAdapter
from antra.utils.matching import score_similarity, duration_close

logger = logging.getLogger(__name__)

MIN_SIMILARITY = 0.42
MIN_SIMILARITY_CJK = 0.35   # loosened for predominantly CJK titles

_CJK_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
    r"\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]"
)


def _is_cjk_heavy(text: str) -> bool:
    stripped = text.replace(" ", "")
    if not stripped:
        return False
    return len(_CJK_RE.findall(stripped)) / len(stripped) > 0.35


def _cjk_sim(a: str, b: str) -> float:
    """Raw SequenceMatcher on lowercased strings — handles trad/simplified variants."""
    if not a or not b:
        return 0.0
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


class NetEaseAdapter(BaseSourceAdapter):
    name = "netease"
    priority = 4      # same tier as JioSaavn; stable-sort places us first
    always_lossy = True   # MP3 only — VIP lossless tier requires auth we don't have

    def is_available(self) -> bool:
        try:
            import pyncm    # noqa: F401
            import yt_dlp   # noqa: F401
            return True
        except ImportError:
            return False

    def should_retry_download(self, result, error: Exception) -> bool:
        """Don't retry geo-restricted tracks — retries would always fail."""
        err = str(error).lower()
        return (
            "geo restriction" not in err
            and "media links" not in err
            and "available in china" not in err
        )

    # ── Search ────────────────────────────────────────────────────────────────

    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        try:
            from pyncm.apis.cloudsearch import GetSearchResult
        except ImportError:
            return None

        best: Optional[SearchResult] = None

        for query in self._build_queries(track):
            try:
                raw = GetSearchResult(query, stype=1, limit=15)
                songs = (raw.get("result") or {}).get("songs") or []
            except Exception as e:
                logger.debug(f"[NetEase] Search error for '{query}': {e}")
                continue

            result = self._best_match(songs, track)
            if result is None:
                continue

            if best is None or result.similarity_score > best.similarity_score:
                best = result

            if result.similarity_score >= 0.90:
                return result

        return best

    def _best_match(self, songs: list, track: TrackMetadata) -> Optional[SearchResult]:
        best = None
        best_score = 0.0
        threshold = MIN_SIMILARITY_CJK if _is_cjk_heavy(track.title) else MIN_SIMILARITY

        for song in songs:
            title = song.get("name", "")
            artists = [a.get("name", "") for a in (song.get("ar") or [])]
            album = (song.get("al") or {}).get("name", "")
            duration_ms = song.get("dt") or None
            song_id = song.get("id")

            if not song_id or not title:
                continue

            score = self._score(track, title, artists)

            if duration_ms and track.duration_ms:
                if not duration_close(track.duration_ms / 1000, duration_ms / 1000, tolerance=6):
                    score *= 0.8

            if score > best_score:
                best_score = score
                best = SearchResult(
                    source=self.name,
                    title=title,
                    artists=artists,
                    album=album or None,
                    duration_ms=int(duration_ms) if duration_ms else None,
                    audio_format=AudioFormat.MP3,
                    quality_kbps=None,   # unknown until download; filled in download()
                    is_lossless=False,
                    download_url=None,
                    stream_id=str(song_id),
                    similarity_score=score,
                    isrc_match=False,
                    artwork_url=self._artwork(song),
                )

        if best and best_score >= threshold:
            logger.debug(
                f"[NetEase] Match score={best_score:.2f}: '{best.title}' "
                f"by {', '.join(best.artists)}"
            )
            return best

        return None

    def _score(self, track: TrackMetadata, result_title: str, result_artists: list[str]) -> float:
        artist_blob = ", ".join(a for a in result_artists if a)

        score = max(
            (
                score_similarity(
                    query_title=v,
                    query_artists=track.artists,
                    result_title=result_title,
                    result_artist=artist_blob,
                )
                for v in self._variants(track.title)
            ),
            default=0.0,
        )

        # For CJK titles: also try raw similarity (handles trad vs simplified)
        if _is_cjk_heavy(track.title) or _is_cjk_heavy(result_title):
            cjk_title = max((_cjk_sim(v, result_title) for v in self._variants(track.title)), default=0.0)
            cjk_artist = max((_cjk_sim(a, artist_blob) for a in track.artists), default=0.0)
            score = max(score, 0.65 * cjk_title + 0.35 * cjk_artist)

        return score

    # ── Download ──────────────────────────────────────────────────────────────

    def download(self, result: SearchResult, output_path: str) -> str:
        """
        Download via yt-dlp using the NetEase song URL.
        yt-dlp handles the encrypted URL extraction internally.
        """
        import yt_dlp

        ne_url = f"https://music.163.com/#/song?id={result.stream_id}"

        # We tell yt-dlp to write to a temp path and then move it ourselves
        # so we control the final extension.
        tmp_dir = os.path.dirname(output_path)
        os.makedirs(tmp_dir, exist_ok=True)

        # Use a unique temp stem to avoid collisions
        tmp_stem = os.path.join(tmp_dir, f"_ne_tmp_{result.stream_id}")

        ydl_opts = {
            "format": "bestaudio",
            "outtmpl": tmp_stem + ".%(ext)s",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": 3,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(ne_url, download=True)
                ext = info.get("ext") or "mp3"
        except Exception as e:
            # Clean up any partial files left by yt-dlp
            for f in glob.glob(tmp_stem + ".*"):
                try:
                    os.remove(f)
                except OSError:
                    pass
            raise ValueError(f"[NetEase] yt-dlp download failed for ID {result.stream_id}: {e}") from e

        # Locate the actual downloaded file — prefer the expected path but fall
        # back to glob in case yt-dlp used a slightly different extension.
        tmp_path = f"{tmp_stem}.{ext}"
        if not os.path.exists(tmp_path):
            candidates = glob.glob(tmp_stem + ".*")
            if not candidates:
                raise ValueError(f"[NetEase] Expected output file not found: {tmp_path}")
            tmp_path = candidates[0]
            ext = os.path.splitext(tmp_path)[1].lstrip(".")

        final_path = f"{output_path}.{ext}"
        try:
            os.replace(tmp_path, final_path)
        except OSError:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
        return final_path

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_queries(self, track: TrackMetadata) -> list[str]:
        seen: set[str] = set()
        queries: list[str] = []

        def _add(q: str):
            q = q.strip()
            if q and q not in seen:
                seen.add(q)
                queries.append(q)

        primary = track.primary_artist
        for v in self._variants(track.title):
            _add(f"{v} {primary}")
            _add(v)

        if len(track.artists) > 1:
            _add(f"{track.title} {' '.join(track.artists[:2])}")

        return queries or [f"{track.title} {primary}"]

    @staticmethod
    def _variants(title: str) -> list[str]:
        vs: list[str] = []

        def _add(v: str):
            v = re.sub(r"\s+", " ", v).strip()
            if v and v not in vs:
                vs.append(v)

        _add(title)
        _add(re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", title))
        _add(re.sub(r"\s*(feat\.?|ft\.?|with)\s+.*$", "", title, flags=re.IGNORECASE))
        cleaned = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", title)
        _add(re.sub(r"\s*(feat\.?|ft\.?|with)\s+.*$", "", cleaned, flags=re.IGNORECASE))
        return vs

    @staticmethod
    def _artwork(song: dict) -> Optional[str]:
        pic = (song.get("al") or {}).get("picUrl")
        return pic if isinstance(pic, str) and pic.strip() else None
