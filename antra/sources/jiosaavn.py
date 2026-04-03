"""
JioSaavn source adapter — provides high-quality MP4/M4A audio downloads.
Uses JioSaavn's public API endpoints (no authentication required).

Supported quality levels:
  12  — 12kbps  (mobile)
  48  — 48kbps
  96  — 96kbps
  160 — 160kbps
  320 — 320kbps (default, best)
"""
import base64
import html
import logging
import os
import re
from urllib.parse import urlparse
from typing import Optional

import requests

try:
    from Cryptodome.Cipher import DES
    from Cryptodome.Util.Padding import unpad
except ImportError:
    try:
        from Crypto.Cipher import DES
        from Crypto.Util.Padding import unpad
    except ImportError:
        DES = None
        unpad = None

from antra.core.models import TrackMetadata, SearchResult, AudioFormat
from antra.sources.base import BaseSourceAdapter
from antra.utils.matching import score_similarity, duration_close

logger = logging.getLogger(__name__)

# JioSaavn metadata is often noisier than Spotify metadata. We keep a lower
# adapter-local threshold so the resolver can still compare it against lower
# priority sources before deciding to fall through.
MIN_SIMILARITY = 0.45

_DES_KEY = b"38346591"


def _decrypt_url(enc_url: str) -> Optional[str]:
    """Decrypt the JioSaavn download URL."""
    if DES is None or unpad is None:
        logger.debug("[JioSaavn] DES crypto module unavailable; cannot decrypt encrypted media URL.")
        return None

    try:
        decoded = base64.b64decode(enc_url.strip())
        decrypted = DES.new(_DES_KEY, DES.MODE_ECB).decrypt(decoded)
        try:
            decrypted = unpad(decrypted, DES.block_size)
        except ValueError:
            decrypted = decrypted.rstrip(b"\x00")
        return decrypted.decode("utf-8", errors="ignore").strip().replace("http://", "https://")
    except Exception as e:
        logger.debug(f"[JioSaavn] URL decrypt failed: {e}")
        return None


class JioSaavnAdapter(BaseSourceAdapter):
    name = "jiosaavn"
    priority = 4  # After Amazon (1), HiFi (2), Soulseek (3)

    _API_BASE = "https://www.jiosaavn.com/api.php"
    # Fallback endpoint used by some open-source clients
    _API_V2 = "https://saavn.dev/api"

    def __init__(self, quality: str = "320"):
        """
        quality: one of "12", "48", "96", "160", "320"
        """
        self.quality = quality
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })

    def is_available(self) -> bool:
        # JioSaavn needs no credentials. Crypto is optional because the
        # official API often returns a usable media_url directly.
        try:
            import requests  # already imported, but check is fast
            return True
        except ImportError:
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # Search
    # ──────────────────────────────────────────────────────────────────────────

    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        """Search JioSaavn for the best matching track."""
        best: Optional[SearchResult] = None

        for query in self._build_queries(track):
            for search_fn in (self._search_via_official_api, self._search_via_community_api):
                result = search_fn(query, track)
                if not result:
                    continue

                if best is None or result.similarity_score > best.similarity_score:
                    best = result

                # Strong hit on the preferred source; no reason to keep searching.
                if result.similarity_score >= 0.90:
                    return result

        return best

    def _search_via_community_api(self, query: str, track: TrackMetadata) -> Optional[SearchResult]:
        """Use saavn.dev community API."""
        try:
            resp = self._session.get(
                f"{self._API_V2}/search/songs",
                params={"query": query, "page": 1, "limit": 10},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("data", {}).get("results", [])
            return self._best_match(items, track, source="community")
        except Exception as e:
            logger.debug(f"[JioSaavn] Community API search failed: {e}")
            return None

    def _search_via_official_api(self, query: str, track: TrackMetadata) -> Optional[SearchResult]:
        """Use JioSaavn's internal API."""
        try:
            resp = self._session.get(
                self._API_BASE,
                params={
                    "__call": "autocomplete.get",
                    "_format": "json",
                    "_marker": "0",
                    "cc": "in",
                    "includeMetaTags": "1",
                    "query": query,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            songs = data.get("songs", {}).get("data", [])
            return self._best_match(songs, track, source="official")
        except Exception as e:
            logger.debug(f"[JioSaavn] Official API search failed: {e}")
            return None

    def _best_match(self, items: list, track: TrackMetadata, source: str) -> Optional[SearchResult]:
        best = None
        best_score = 0.0

        for item in items:
            title, artists, duration_s, song_id = self._parse_item(item, source)
            if not song_id:
                continue

            score = self._score_match(track, title, artists)

            if duration_s and track.duration_seconds:
                if not duration_close(track.duration_seconds, duration_s, tolerance=6):
                    score *= 0.8

            if score > best_score:
                best_score = score
                best = SearchResult(
                    source=self.name,
                    title=title,
                    artists=artists,
                    album=item.get("album", {}).get("name") if isinstance(item.get("album"), dict) else item.get("album"),
                    duration_ms=int(duration_s * 1000) if duration_s else None,
                    audio_format=AudioFormat.AAC,
                    quality_kbps=int(self.quality),
                    is_lossless=False,
                    download_url=None,
                    stream_id=song_id,
                    similarity_score=score,
                    isrc_match=False,
                    artwork_url=self._extract_artwork_url(item),
                )

        if best and best_score >= MIN_SIMILARITY:
            logger.debug(f"[JioSaavn] Match score={best_score:.2f}: {best.title}")
            return best

        return None

    def _parse_item(self, item: dict, source: str) -> tuple[str, list[str], Optional[float], Optional[str]]:
        """Extract (title, artists, duration_seconds, song_id) from an API item."""
        try:
            if source == "community":
                title = self._clean_text(item.get("name", ""))
                artists = [a["name"] for a in item.get("artists", {}).get("primary", [])] or [
                    item.get("primaryArtists", "Unknown")
                ]
                artists = [self._clean_text(artist) for artist in artists if artist]
                duration_s = float(item.get("duration", 0)) or None
                song_id = item.get("id")
            else:
                # Official API shape
                title = self._clean_text(
                    item.get("title")
                    or item.get("song")
                    or item.get("name")
                    or ""
                )
                primary = item.get("more_info", {}).get("artistMap", {}).get("primary_artists", [])
                if primary:
                    artists = [self._clean_text(a["name"]) for a in primary]
                else:
                    subtitle = self._clean_text(item.get("subtitle", "Unknown"))
                    artists = [
                        artist.strip()
                        for artist in re.split(r",|&| feat\. ", subtitle, flags=re.IGNORECASE)
                        if artist.strip()
                    ]
                duration_s = float(item.get("more_info", {}).get("duration", 0)) or None
                song_id = item.get("id")

            return title, artists, duration_s, song_id
        except Exception:
            return "", [], None, None

    def _score_match(self, track: TrackMetadata, result_title: str, result_artists: list[str]) -> float:
        artist_blob = ", ".join(a for a in result_artists if a)
        return max(
            (
                score_similarity(
                    query_title=title_variant,
                    query_artists=track.artists,
                    result_title=result_title,
                    result_artist=artist_blob,
                )
                for title_variant in self._title_variants(track.title)
            ),
            default=0.0,
        )

    def _build_queries(self, track: TrackMetadata) -> list[str]:
        queries: list[str] = []
        seen = set()
        artist_variants = [track.primary_artist, " ".join(track.artists[:2]).strip(), ""]

        for title in self._title_variants(track.title):
            for artist in artist_variants:
                query = f"{title} {artist}".strip()
                if query and query not in seen:
                    seen.add(query)
                    queries.append(query)

        return queries or [f"{track.title} {track.primary_artist}".strip()]

    @staticmethod
    def _title_variants(title: str) -> list[str]:
        variants: list[str] = []

        def add(value: str):
            value = re.sub(r"\s+", " ", value).strip()
            if value and value not in variants:
                variants.append(value)

        add(title)
        add(re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", title))
        add(re.sub(r"\s*(feat\.?|ft\.?|with)\s+.*$", "", title, flags=re.IGNORECASE))

        cleaned = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", title)
        add(re.sub(r"\s*(feat\.?|ft\.?|with)\s+.*$", "", cleaned, flags=re.IGNORECASE))

        return variants

    @staticmethod
    def _clean_text(value: str) -> str:
        value = html.unescape(value or "")
        value = re.sub(r"<[^>]+>", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _extract_artwork_url(item: dict) -> Optional[str]:
        image = item.get("image") or item.get("song_image") or item.get("image_url") or item.get("album_image")
        if isinstance(image, str) and image.strip():
            return image.strip().replace("http://", "https://")

        if isinstance(image, list):
            links = [
                entry.get("link", "").strip()
                for entry in image
                if isinstance(entry, dict) and entry.get("link")
            ]
            if links:
                return links[-1].replace("http://", "https://")

        more_info = item.get("more_info", {})
        if isinstance(more_info, dict):
            image_url = more_info.get("image") or more_info.get("song_image") or more_info.get("image_url")
            if isinstance(image_url, str) and image_url.strip():
                return image_url.strip().replace("http://", "https://")

        return None

    def hydrate_track_metadata(self, track: TrackMetadata, result: SearchResult) -> None:
        if not result.stream_id:
            return

        details = self._fetch_song_metadata(result.stream_id)
        if not details:
            logger.debug(f"[JioSaavn] No song metadata available for hydration: {result.stream_id}")
            return

        album = self._extract_album_name(details)
        artwork_url = self._extract_artwork_url(details)
        lyrics = self._extract_plain_lyrics(details)

        if (not track.album or track.album == "Unknown Album") and album:
            track.album = album
        if not track.artwork_url and artwork_url:
            track.artwork_url = artwork_url
        if not track.lyrics and lyrics:
            track.lyrics = lyrics

        logger.debug(
            "[JioSaavn] Hydrated %s | album=%r artwork=%s lyrics=%s",
            result.stream_id,
            track.album,
            bool(track.artwork_url),
            bool(track.lyrics),
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Download
    # ──────────────────────────────────────────────────────────────────────────

    def download(self, result: SearchResult, output_path: str) -> str:
        """Download the AAC file to output_path with the correct extension."""
        stream_url = self._get_stream_url(result.stream_id)
        if not stream_url:
            raise ValueError(f"[JioSaavn] Could not get stream URL for ID {result.stream_id}")

        final_path = output_path + self._infer_extension(stream_url)
        self._stream_to_file(stream_url, final_path)
        return final_path

    def _get_stream_url(self, song_id: str) -> Optional[str]:
        """Fetch and decrypt the actual download URL for a song."""
        # Try the official API first. It is the most reliable endpoint in the
        # current environment and avoids depending on third-party mirrors.
        url = self._get_url_via_official_api(song_id)
        if url:
            return url

        return self._get_url_via_community_api(song_id)

    def _fetch_song_metadata(self, song_id: str) -> Optional[dict]:
        for fetcher in (self._get_song_via_community_api, self._get_song_via_official_api):
            try:
                song = fetcher(song_id)
                if song:
                    return song
            except Exception as e:
                logger.debug(f"[JioSaavn] Song metadata fetch failed for {song_id}: {e}")
        return None

    def _get_song_via_community_api(self, song_id: str) -> Optional[dict]:
        resp = self._session.get(
            f"{self._API_V2}/songs/{song_id}",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data")
        if isinstance(data, list):
            return data[0] if data else None
        if isinstance(data, dict):
            return data
        return None

    def _get_song_via_official_api(self, song_id: str) -> Optional[dict]:
        resp = self._session.get(
            self._API_BASE,
            params={
                "__call": "song.getDetails",
                "cc": "in",
                "_format": "json",
                "_marker": "0",
                "pids": song_id,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return self._extract_official_song(data, song_id)

    def _get_url_via_community_api(self, song_id: str) -> Optional[str]:
        try:
            resp = self._session.get(
                f"{self._API_V2}/songs/{song_id}",
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            songs = data.get("data", [])
            if not songs:
                return None

            song = songs[0] if isinstance(songs, list) else songs
            urls = song.get("downloadUrl", [])

            # downloadUrl is a list of {quality, url} dicts ordered low → high
            target = self.quality + "kbps"
            for entry in reversed(urls):  # try highest first
                if entry.get("quality", "") == target:
                    return entry["url"]
            # Fallback: last (highest available)
            if urls:
                return urls[-1]["url"]
        except Exception as e:
            logger.debug(f"[JioSaavn] Community URL fetch failed for {song_id}: {e}")
        return None

    def _get_url_via_official_api(self, song_id: str) -> Optional[str]:
        try:
            song = self._get_song_via_official_api(song_id)
            if not song:
                return None

            media_url = song.get("media_url")
            if media_url:
                return self._normalize_media_url(media_url)

            enc_url = song.get("encrypted_media_url") or song.get("encrypted_drm_media_url")
            if not enc_url:
                return None

            decrypted = _decrypt_url(enc_url)
            if not decrypted:
                return None
            return self._normalize_media_url(decrypted)
        except Exception as e:
            logger.debug(f"[JioSaavn] Official URL fetch failed for {song_id}: {e}")
        return None

    def _normalize_media_url(self, url: str) -> Optional[str]:
        if not url:
            return None

        normalized = url.replace("http://", "https://")
        for quality in ("12", "48", "96", "160", "320"):
            normalized = normalized.replace(f"_{quality}.mp4", f"_{self.quality}.mp4")
            normalized = normalized.replace(f"_{quality}.mp3", f"_{self.quality}.mp3")
            normalized = normalized.replace(f"_{quality}.m4a", f"_{self.quality}.m4a")
        return normalized

    @staticmethod
    def _extract_official_song(data: dict, song_id: str) -> Optional[dict]:
        if not isinstance(data, dict):
            return None

        direct = data.get(song_id)
        if isinstance(direct, dict):
            return direct

        songs = data.get("songs")
        if isinstance(songs, list) and songs:
            first = songs[0]
            if isinstance(first, dict):
                return first

        for value in data.values():
            if isinstance(value, dict) and value.get("id") == song_id:
                return value

        return None

    @staticmethod
    def _extract_album_name(item: dict) -> Optional[str]:
        album = item.get("album")
        if isinstance(album, dict):
            name = album.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        if isinstance(album, str) and album.strip():
            return album.strip()

        for key in ("album_name", "more_info"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = value.get("album") or value.get("album_name")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()

        return None

    @staticmethod
    def _extract_plain_lyrics(item: dict) -> Optional[str]:
        for key in (
            "lyrics",
            "plainLyrics",
            "plain_lyrics",
            "lyrics_snippet",
            "lyricsSnippet",
        ):
            value = item.get(key)
            if isinstance(value, str):
                cleaned = html.unescape(value).strip()
                if cleaned:
                    return cleaned

        more_info = item.get("more_info", {})
        if isinstance(more_info, dict):
            for key in ("lyrics", "lyrics_snippet", "lyricsSnippet"):
                value = more_info.get(key)
                if isinstance(value, str):
                    cleaned = html.unescape(value).strip()
                    if cleaned:
                        return cleaned

        return None

    def _stream_to_file(self, url: str, path: str):
        with self._session.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)

    @staticmethod
    def _infer_extension(url: str) -> str:
        path = urlparse(url).path.lower()
        if path.endswith(".m4a"):
            return ".m4a"
        if path.endswith(".mp4"):
            return ".m4a"
        if path.endswith(".aac"):
            return ".aac"
        return ".m4a"
