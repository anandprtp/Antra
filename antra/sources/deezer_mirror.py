"""
Deezer mirror adapter — 16-bit FLAC via your self-hosted deezer_server.py.

Calls your laptop server at DEEZER_MIRROR_URL.
Priority 3 — used as 16-bit FLAC fallback when 24-bit sources (Tidal, Qobuz,
Amazon) don't have the track. Deezer has excellent catalog coverage.

The server returns a CDN URL + Blowfish decryption key. This adapter downloads
the encrypted stream and decrypts it locally using pycryptodomex.

Config (.env):
  DEEZER_MIRROR_URL=https://your-deezer-host.example   (or http://localhost:7342)
"""
import hashlib
import logging
import os
import re
from typing import Optional

import requests

from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.sources.base import BaseSourceAdapter, RateLimitedError
from antra.utils.matching import score_similarity, duration_close

logger = logging.getLogger(__name__)

MIN_SIMILARITY = 0.75
_CHUNK_SIZE = 2048
_DOWNLOAD_CHUNK = 65536

# Strip featured/with suffixes from titles before text search.
# e.g. "On Time (with John Legend)" → "On Time"
#      "Superhero [with Future & Chris Brown]" → "Superhero"
_FEAT_RE = re.compile(
    r"\s*[\(\[](feat\.?|ft\.?|with|featuring)\s[^\)\]]+[\)\]]",
    re.IGNORECASE,
)


def _decrypt_stream(encrypted: bytes, track_id: str, bf_key_hex: str) -> bytes:
    """Decrypt Deezer's Blowfish-encrypted stream."""
    from Cryptodome.Cipher import Blowfish
    key = bf_key_hex.encode()
    out = bytearray()
    i = 0
    block_num = 0
    while i < len(encrypted):
        chunk = encrypted[i: i + _CHUNK_SIZE]
        if block_num % 3 == 0 and len(chunk) == _CHUNK_SIZE:
            cipher = Blowfish.new(key, Blowfish.MODE_CBC, b"\x00\x01\x02\x03\x04\x05\x06\x07")
            chunk = cipher.decrypt(chunk)
        out.extend(chunk)
        i += _CHUNK_SIZE
        block_num += 1
    return bytes(out)


class DeezerMirrorAdapter(BaseSourceAdapter):
    """
    Downloads 16-bit FLAC from your self-hosted Deezer mirror server.
    Used as a fallback when 24-bit sources don't have the track.
    """

    name = "deezer_mirror"
    priority = 3  # After 24-bit sources (priority 1), before community pools

    def __init__(self, mirror_url: str, api_key: str = ""):
        self._base = mirror_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "Antra/1.0", "Accept": "application/json"})
        if api_key:
            self._session.headers["X-API-Key"] = api_key
        self._available: Optional[bool] = None

    def is_available(self) -> bool:
        if not self._base:
            return False
        # Check pycryptodomex is installed
        try:
            from Cryptodome.Cipher import Blowfish  # noqa
        except ImportError:
            logger.warning("[DeezerMirror] pycryptodomex not installed — run: pip install pycryptodomex")
            return False
        if self._available is not None:
            return self._available
        try:
            r = self._session.get(f"{self._base}/", timeout=5)
            self._available = r.status_code == 200
        except Exception:
            self._available = False
        return self._available

    def _reset_availability(self):
        self._available = None

    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        deezer_track_id = getattr(track, "deezer_track_id", None)
        if deezer_track_id:
            return SearchResult(
                source=self.name,
                title=track.title,
                artists=track.artists,
                album=track.album,
                duration_ms=track.duration_ms,
                audio_format=AudioFormat.FLAC,
                quality_kbps=1411,
                is_lossless=True,
                bit_depth=16,
                sample_rate_hz=44100,
                download_url=None,
                stream_id=str(deezer_track_id),
                similarity_score=1.0,
                isrc_match=bool(track.isrc),
            )

        if track.isrc:
            try:
                r = self._session.get(
                    f"{self._base}/api/search/isrc/{track.isrc}",
                    timeout=10,
                )
                if r.status_code == 429:
                    raise RateLimitedError("Deezer mirror rate limited (429)")
                if r.status_code in (401, 403):
                    logger.warning("[DeezerMirror] API key rejected (%d) — check key on server", r.status_code)
                    return None
                if r.status_code == 503:
                    self._reset_availability()
                    return None
                if r.status_code == 200:
                    data = r.json()
                    result_duration_ms = data.get("duration_ms")
                    # Sanity-check duration to catch cases where two ISRCs
                    # (e.g. Pt. 1 / Pt. 2) resolve to the same catalog track.
                    if track.duration_ms and result_duration_ms:
                        if not duration_close(
                            track.duration_ms / 1000,
                            result_duration_ms / 1000,
                            tolerance=30,
                        ):
                            logger.info(
                                "[DeezerMirror] ISRC match for '%s' rejected — "
                                "duration mismatch (expected %.0fs, got %.0fs)",
                                track.title,
                                track.duration_ms / 1000,
                                result_duration_ms / 1000,
                            )
                            return self._text_search(track)
                    return SearchResult(
                        source=self.name,
                        title=data.get("title", ""),
                        artists=[data.get("artist", "")],
                        album=data.get("album", ""),
                        duration_ms=result_duration_ms,
                        audio_format=AudioFormat.FLAC,
                        quality_kbps=1411,
                        is_lossless=True,
                        bit_depth=16,
                        sample_rate_hz=44100,
                        download_url=None,
                        stream_id=str(data["track_id"]),
                        similarity_score=1.0,
                        isrc_match=True,
                    )
            except RateLimitedError:
                raise
            except Exception as e:
                logger.debug("[DeezerMirror] ISRC search failed: %s", e)

        # Text search fallback — used when ISRC is unavailable
        return self._text_search(track)

    def _clean_title(self, title: str) -> str:
        """Strip featured/with suffixes for cleaner search queries."""
        return _FEAT_RE.sub("", title).strip()

    def _text_search(self, track: TrackMetadata) -> Optional[SearchResult]:
        # Try with full title first, then with featured-artist suffix stripped.
        titles_to_try = [track.title]
        clean = self._clean_title(track.title)
        if clean and clean != track.title:
            titles_to_try.append(clean)

        for search_title in titles_to_try:
            result = self._text_search_with_title(track, search_title)
            if result is not None:
                return result
        return None

    def _text_search_with_title(self, track: TrackMetadata, search_title: str) -> Optional[SearchResult]:
        try:
            r = self._session.get(
                f"{self._base}/api/search",
                params={"title": search_title, "artist": track.primary_artist, "limit": 5},
                timeout=10,
            )
            if r.status_code == 429:
                raise RateLimitedError("Deezer mirror rate limited (429)")
            if r.status_code in (401, 403):
                logger.warning("[DeezerMirror] API key rejected (%d) — check key on server", r.status_code)
                return None
            if r.status_code != 200:
                return None
            items = r.json().get("results") or []
        except RateLimitedError:
            raise
        except Exception as e:
            logger.debug("[DeezerMirror] Text search failed: %s", e)
            return None

        best: Optional[SearchResult] = None
        best_score = 0.0
        for item in items:
            # Score against the original track title (not the cleaned search title)
            # so similarity reflects the actual track, not the stripped query.
            score = score_similarity(
                query_title=track.title,
                query_artists=track.artists,
                result_title=item.get("title", ""),
                result_artist=item.get("artist", ""),
            )
            dur_ms = item.get("duration_ms")
            if dur_ms and track.duration_ms:
                if not duration_close(track.duration_ms / 1000, dur_ms / 1000, tolerance=5):
                    score *= 0.8
            if score > best_score:
                best_score = score
                best = SearchResult(
                    source=self.name,
                    title=item.get("title", ""),
                    artists=[item.get("artist", "")],
                    album=item.get("album", ""),
                    duration_ms=dur_ms,
                    audio_format=AudioFormat.FLAC,
                    quality_kbps=1411,
                    is_lossless=True,
                    bit_depth=16,
                    sample_rate_hz=44100,
                    download_url=None,
                    stream_id=str(item["track_id"]),
                    similarity_score=score,
                    isrc_match=False,
                )

        if best and best_score >= MIN_SIMILARITY:
            return best
        return None

    def download(self, result: SearchResult, output_path: str) -> str:
        track_id = result.stream_id
        try:
            r = self._session.get(f"{self._base}/api/track/{track_id}", timeout=30)
            if r.status_code == 429:
                raise RateLimitedError("Deezer mirror rate limited (429)")
            if r.status_code == 503:
                self._reset_availability()
                raise RuntimeError("[DeezerMirror] Server unavailable (503)")
            r.raise_for_status()
            data = r.json()
        except RateLimitedError:
            raise
        except Exception as e:
            raise RuntimeError(f"[DeezerMirror] Track request failed: {e}") from e

        bf_key = data.get("decryptionKey")
        if not bf_key:
            raise RuntimeError(f"[DeezerMirror] Missing decryptionKey for track {track_id}")

        relay_url = f"{self._base}/api/stream/{track_id}"

        # Download encrypted stream through the VPS relay so the local machine
        # never has to resolve Deezer's CDN hostnames directly.
        with self._session.get(relay_url, stream=True, timeout=120) as r:
            if r.status_code == 429:
                raise RateLimitedError("Deezer mirror rate limited (429)")
            if r.status_code == 503:
                self._reset_availability()
                raise RuntimeError("[DeezerMirror] Stream relay unavailable (503)")
            r.raise_for_status()
            chunks = []
            for chunk in r.iter_content(_DOWNLOAD_CHUNK):
                if chunk:
                    chunks.append(chunk)
        encrypted = b"".join(chunks)

        # Decrypt
        decrypted = _decrypt_stream(encrypted, track_id, bf_key)

        # Detect format from first bytes
        ext = ".flac" if decrypted[:4] == b"fLaC" else ".mp3"
        final_path = output_path + ext
        os.makedirs(os.path.dirname(os.path.abspath(final_path)), exist_ok=True)
        with open(final_path, "wb") as f:
            f.write(decrypted)

        logger.info("[DeezerMirror] Downloaded %s (%d bytes)", os.path.basename(final_path), len(decrypted))
        return final_path
