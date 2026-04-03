"""
Apple Music source adapter — free ALAC/AAC via community proxy pool.

Uses the iTunes Search API (no auth, no rate limits) to resolve track IDs,
then downloads lossless ALAC streams via community Apple Music proxy endpoints
that handle FairPlay decryption server-side.

Audio quality: ALAC up to 24-bit/192kHz (Hi-Res Lossless).
Output format: .m4a (ALAC lossless) or .m4a (AAC 256kbps fallback).

No Apple Music account needed. No credentials needed.
"""
import logging
import os
import re
import subprocess
import sys
from typing import Optional

import requests

from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.sources.base import BaseSourceAdapter
from antra.utils.matching import duration_close, score_similarity

logger = logging.getLogger(__name__)

# On Windows, prevent subprocess from flashing a console window
_SUBPROCESS_FLAGS = {}
if sys.platform == "win32":
    _SUBPROCESS_FLAGS["creationflags"] = subprocess.CREATE_NO_WINDOW

# ── Community Apple Music proxy pool ─────────────────────────────────────────
# These are community-run instances that wrap Apple Music's streaming API and
# handle FairPlay DRM server-side. Same network as the HiFi Tidal proxies.
#
# API contract (same pattern as Amazon adapter):
#   GET /api/track/{itunes_track_id}
#   → { "streamUrl": "...", "decryptionKey": "..." (optional), "quality": "..." }
ENDPOINTS = [
    "https://apple.squid.wtf",
    "https://appl.afkarxyz.qzz.io",
    "https://apple.rnb.su",
    "https://apple.vov.li",
]

# ── iTunes Search API ─────────────────────────────────────────────────────────
_ITUNES_SEARCH = "https://itunes.apple.com/search"
_ITUNES_LOOKUP = "https://itunes.apple.com/lookup"

MIN_SIMILARITY = 0.25
REQUEST_TIMEOUT = 12


class AppleMusicAdapter(BaseSourceAdapter):
    """
    Apple Music lossless ALAC via community proxy endpoints.
    Track resolution uses the free iTunes Search API (no auth).
    """

    name = "apple"
    priority = 2  # Behind Amazon (p=1) — both are Hi-Res community proxies; Amazon takes precedence

    def __init__(self, mirrors: Optional[list[str]] = None):
        self._mirrors = [m.rstrip("/") for m in (mirrors or ENDPOINTS) if m]
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        })
        self._current_mirror: Optional[str] = None
        self._mirror_failures: dict[str, int] = {}

    def is_available(self) -> bool:
        """Available if at least one proxy mirror is reachable."""
        return self._get_working_mirror() is not None

    # ── Mirror management ─────────────────────────────────────────────────────

    def _get_working_mirror(self, force_rotate: bool = False) -> Optional[str]:
        if self._current_mirror and not force_rotate:
            return self._current_mirror

        valid = [m for m in self._mirrors if self._mirror_failures.get(m, 0) < 3]
        if not valid:
            logger.warning("[Apple] All mirrors failed. Resetting failure counts.")
            self._mirror_failures.clear()
            valid = self._mirrors

        for mirror in valid:
            if mirror == self._current_mirror and force_rotate:
                continue
            try:
                resp = self._session.get(mirror + "/", timeout=5)
                if resp.status_code in (200, 404):
                    self._current_mirror = mirror
                    logger.debug(f"[Apple] Using mirror: {mirror}")
                    return mirror
            except Exception as e:
                logger.debug(f"[Apple] Mirror {mirror} unreachable: {e}")
                self._mirror_failures[mirror] = self._mirror_failures.get(mirror, 0) + 1

        # Last resort: return first mirror without a health check
        if self._mirrors:
            self._current_mirror = self._mirrors[0]
            return self._current_mirror
        return None

    # ── Search ────────────────────────────────────────────────────────────────

    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        """Resolve iTunes track ID using the iTunes Search API."""
        # ISRC lookup first — exact match, fastest path
        if track.isrc:
            result = self._lookup_by_isrc(track)
            if result:
                return result

        return self._search_by_text(track)

    def _lookup_by_isrc(self, track: TrackMetadata) -> Optional[SearchResult]:
        """
        Use iTunes lookup API to find a track by ISRC.
        https://itunes.apple.com/lookup?isrc={isrc}&entity=song
        """
        try:
            resp = self._session.get(
                _ITUNES_LOOKUP,
                params={"isrc": track.isrc, "entity": "song"},
                timeout=REQUEST_TIMEOUT,
            )
            if not resp.ok:
                return None
            items = resp.json().get("results", [])
            # Filter to actual songs (not albums/artists)
            songs = [i for i in items if i.get("wrapperType") == "track" and i.get("kind") == "song"]
            if songs:
                return self._item_to_result(songs[0], track, isrc_match=True)
        except Exception as e:
            logger.debug(f"[Apple] ISRC lookup failed: {e}")
        return None

    def _search_by_text(self, track: TrackMetadata) -> Optional[SearchResult]:
        """
        Search iTunes catalog by text query with similarity scoring.
        https://itunes.apple.com/search?term={query}&entity=song&limit=10
        """
        # Strip parenthetical suffixes for cleaner matching
        clean_title = re.sub(r"\s*\(.*?\)\s*", "", track.title).strip()
        query = f"{clean_title} {track.primary_artist}"

        try:
            resp = self._session.get(
                _ITUNES_SEARCH,
                params={"term": query, "entity": "song", "limit": 10, "media": "music"},
                timeout=REQUEST_TIMEOUT,
            )
            if not resp.ok:
                logger.debug(f"[Apple] iTunes search returned {resp.status_code}")
                return None
            items = resp.json().get("results", [])
        except Exception as e:
            logger.debug(f"[Apple] iTunes search failed: {e}")
            return None

        best: Optional[SearchResult] = None
        best_score = 0.0

        for item in items:
            if item.get("kind") != "song":
                continue

            score = score_similarity(
                query_title=track.title,
                query_artists=track.artists,
                result_title=item.get("trackName", ""),
                result_artist=item.get("artistName", ""),
            )

            duration_ms = item.get("trackTimeMillis")
            if duration_ms and track.duration_ms:
                if not duration_close(track.duration_ms / 1000, duration_ms / 1000, tolerance=10):
                    score *= 0.8

            if score > best_score:
                best_score = score
                best = self._item_to_result(item, track, score=score)

        if best and best_score >= MIN_SIMILARITY:
            logger.debug(f"[Apple] Best match score={best_score:.2f}: {best.title}")
            return best

        logger.debug(f"[Apple] No match (best={best_score:.2f}) for: {track.title}")
        return None

    def _item_to_result(
        self,
        item: dict,
        track: TrackMetadata,
        isrc_match: bool = False,
        score: float = 0.0,
    ) -> Optional[SearchResult]:
        try:
            track_id = str(item.get("trackId", ""))
            if not track_id:
                return None
            duration_ms = item.get("trackTimeMillis")
            return SearchResult(
                source=self.name,
                title=item.get("trackName", track.title),
                artists=[item.get("artistName", "")],
                album=item.get("collectionName"),
                duration_ms=duration_ms,
                audio_format=AudioFormat.ALAC,
                quality_kbps=None,
                is_lossless=True,
                bit_depth=24,  # Hi-Res Lossless tier via community proxy
                download_url=None,
                stream_id=track_id,
                similarity_score=1.0 if isrc_match else score,
                isrc_match=isrc_match,
            )
        except Exception as e:
            logger.debug(f"[Apple] _item_to_result failed: {e}")
            return None

    # ── Download ──────────────────────────────────────────────────────────────

    def download(self, result: SearchResult, output_path: str) -> str:
        """
        Fetch the ALAC stream for a track from the proxy pool.
        Retries with the next mirror on failure.
        """
        track_id = result.stream_id
        if not track_id:
            raise ValueError("[Apple] Missing track_id in SearchResult")

        max_attempts = len(self._mirrors)
        last_error = None

        for attempt in range(max_attempts):
            mirror = self._get_working_mirror(force_rotate=(attempt > 0))
            if not mirror:
                raise RuntimeError("[Apple] No mirrors available")

            api_url = f"{mirror}/api/track/{track_id}"
            try:
                logger.debug(f"[Apple] Fetching stream info (attempt {attempt + 1}/{max_attempts}) from {mirror}")
                resp = self._session.get(api_url, timeout=20)

                if resp.status_code == 200:
                    data = resp.json()
                    stream_url = data.get("streamUrl")
                    decryption_key = data.get("decryptionKey")

                    if not stream_url:
                        raise RuntimeError("No streamUrl in proxy response")

                    return self._process_download(stream_url, decryption_key, output_path)

                logger.debug(f"[Apple] Mirror {mirror} returned {resp.status_code}")
                last_error = f"HTTP {resp.status_code}"

            except Exception as e:
                logger.debug(f"[Apple] Mirror {mirror} failed: {e}")
                last_error = str(e)

            self._mirror_failures[mirror] = self._mirror_failures.get(mirror, 0) + 1

        raise RuntimeError(f"[Apple] All mirrors failed. Last error: {last_error}")

    def _process_download(
        self,
        stream_url: str,
        decryption_key: Optional[str],
        output_path: str,
    ) -> str:
        """Download (and optionally decrypt) the audio stream."""
        enc_path = output_path + ".enc.m4a"
        logger.debug(f"[Apple] Downloading stream to: {enc_path}")

        with self._session.get(stream_url, stream=True, timeout=90) as r:
            r.raise_for_status()
            os.makedirs(os.path.dirname(os.path.abspath(enc_path)), exist_ok=True)
            with open(enc_path, "wb") as f:
                for chunk in r.iter_content(65536):
                    if chunk:
                        f.write(chunk)

        final_path = output_path + ".m4a"

        if not decryption_key:
            # Proxy served a pre-decrypted stream
            os.rename(enc_path, final_path)
        else:
            logger.debug("[Apple] Decrypting via ffmpeg…")
            if not self._decrypt(enc_path, final_path, decryption_key):
                if os.path.exists(enc_path):
                    os.remove(enc_path)
                raise RuntimeError("[Apple] ffmpeg decryption failed")
            os.remove(enc_path)

        return final_path

    @staticmethod
    def _decrypt(input_path: str, output_path: str, key: str) -> bool:
        """Decrypt FairPlay-protected stream via ffmpeg (lossless, no re-encode)."""
        try:
            from antra.utils.runtime import get_ffmpeg_exe
            ffmpeg = get_ffmpeg_exe() or "ffmpeg"
            result = subprocess.run(
                [
                    ffmpeg, "-y",
                    "-decryption_key", key.strip(),
                    "-i", input_path,
                    "-c", "copy",
                    output_path,
                ],
                capture_output=True,
                timeout=180,
                **_SUBPROCESS_FLAGS,
            )
            if result.returncode != 0:
                logger.debug(
                    f"[Apple] ffmpeg stderr: {result.stderr.decode('utf-8', errors='ignore')}"
                )
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"[Apple] ffmpeg decryption error: {e}")
            return False


def _diagnose():
    """Run with: python -m antra.sources.apple"""
    logging.basicConfig(level=logging.DEBUG)

    adapter = AppleMusicAdapter()
    print("\n=== Mirror Health Check ===")
    for mirror in adapter._mirrors:
        try:
            r = adapter._session.get(mirror + "/", timeout=5)
            print(f"  [{'OK' if r.status_code in (200, 404) else 'FAIL'}] {mirror} → {r.status_code}")
        except Exception as e:
            print(f"  [FAIL] {mirror} → {e}")

    print("\n=== ISRC Lookup ===")
    from antra.core.models import TrackMetadata
    track = TrackMetadata(
        title="Blinding Lights",
        artists=["The Weeknd"],
        album="After Hours",
        duration_ms=200000,
        isrc="USUG11904206",
    )
    result = adapter.search(track)
    if result:
        print(f"  Found: {result.title} — {result.artists}")
        print(f"  iTunes ID: {result.stream_id}, score={result.similarity_score:.2f}")
    else:
        print("  Not found via ISRC — trying text search…")
        result = adapter._search_by_text(track)
        if result:
            print(f"  Found via text: {result.title} — {result.artists} (ID: {result.stream_id})")
        else:
            print("  Not found")


if __name__ == "__main__":
    _diagnose()
