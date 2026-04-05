"""
HiFi adapter — free FLAC via community-run hifi-api instances.

These are public endpoints running the open-source hifi-api project
(github.com/binimum/hifi-api), which wraps Tidal's streaming API.
No account required. Requests are sent in parallel; first valid
response wins (same strategy as SpotiFLAC).

Audio quality: up to Hi-Res FLAC 24-bit/192kHz depending on instance.
Falls back to 16-bit FLAC if hi-res is unavailable for a track.

NOTE: These are community-run servers. They may go down or rotate.
      Update ENDPOINTS below if instances become unavailable.
      Priority 5 = runs before JioSaavn and YouTube in the chain.
"""
import base64
import concurrent.futures
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from typing import Optional

import requests
from mutagen import File as MutagenFile

from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.sources.base import BaseSourceAdapter
from antra.utils.matching import duration_close, score_similarity

logger = logging.getLogger(__name__)

# On Windows, prevent subprocess from flashing a console window
_SUBPROCESS_FLAGS = {}
if sys.platform == "win32":
    _SUBPROCESS_FLAGS["creationflags"] = subprocess.CREATE_NO_WINDOW

# ── Known public hifi-api endpoints ──────────────────────────────────────────
# Includes original SpotiFLAC endpoints plus Monochrome (monochrome.tf,
# mono.squid.wtf) and QQDL community mirrors. The health-check in
# _get_live_endpoints() filters out any that are down at runtime, so it is
# safe to list more endpoints here than are actually alive at any given time.
ENDPOINTS = [
    # Monochrome (monochrome.tf) backend instances
    "https://api.monochrome.tf",
    "https://arran.monochrome.tf",
    # squid.wtf backends (mono.squid.wtf frontend)
    "https://triton.squid.wtf",
    "https://tidal.squid.wtf",
    # QQDL community mirrors
    "https://tidal.qqdl.site",
    "https://vogel.qqdl.site",
    "https://wolf.qqdl.site",
    "https://maus.qqdl.site",
    # binimum original
    "https://music.binimum.org",
    # Additional community mirrors — verify liveness before relying on
    "https://tidal.notabot.dev",
    "https://hifi.lunar.gg",
    "https://api.hifi.786.moe",
    "https://hifi.geeked.wtf",
    "https://katze.qqdl.site",
    "https://hund.qqdl.site",
    "https://tidal.kinoplus.online",
    "https://hifi.samidy.com",
]

# Quality preference order (try hi-res first, fall back)
QUALITY_LEVELS = ["HI_RES_LOSSLESS", "LOSSLESS"]

ENDPOINT_CACHE_TTL = 300  # 5 minutes
MIN_SIMILARITY = 0.25
REQUEST_TIMEOUT = 8  # seconds per endpoint


class HifiAdapter(BaseSourceAdapter):
    """Free FLAC via community hifi-api instances (Tidal backend)."""

    name = "hifi"
    priority = 2  # After Amazon, before Soulseek

    def __init__(self):
        # Note: IP spoofing headers were removed — they don't bypass network-layer rate limits.
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })
        # Cache working endpoints to avoid re-testing on every track
        self._live_endpoints: Optional[list[str]] = None
        self._live_endpoints_ts: float = 0.0
        self._endpoint_success: dict[str, int] = {}
        self._download_blocklist: set[str] = set()
        # Per-endpoint 429 backoff: maps endpoint URL → unix timestamp when safe to retry
        self._endpoint_backoff: dict[str, float] = {}
        # Track consecutive empty-result searches to detect slow degradation
        self._consecutive_failures: int = 0


    def is_available(self) -> bool:
        """Always available — no credentials needed."""
        return True

    def is_throttled(self) -> bool:
        """
        Return True when HiFi is significantly rate-limited and should be skipped
        in favour of Amazon or other lossless sources.

        Triggers when EITHER:
        - More than half of the known live endpoints are currently backed off (429), OR
        - 3+ consecutive search/download failures have occurred this session
        """
        live = self._live_endpoints
        if live and len(live) > 0:
            backed_off = sum(1 for ep in live if self._is_endpoint_backed_off(ep))
            majority_backed_off = backed_off > len(live) / 2
            if majority_backed_off:
                logger.debug(
                    f"[HiFi] Throttle detected: {backed_off}/{len(live)} endpoints backed off"
                )
                return True
        if self._consecutive_failures >= 3:
            logger.debug(
                f"[HiFi] Throttle detected: {self._consecutive_failures} consecutive failures"
            )
            return True
        return False

    def _is_endpoint_backed_off(self, ep: str) -> bool:
        """Return True if the endpoint is currently in its 429 cooldown window."""
        return time.time() < self._endpoint_backoff.get(ep, 0.0)

    def _mark_endpoint_429(self, ep: str) -> None:
        """Put the endpoint in a 60-second cooldown after receiving a 429 response."""
        self._endpoint_backoff[ep] = time.time() + 60
        logger.debug(f"[HiFi] Endpoint backed off for 60s after 429: {ep}")


    def _get_live_endpoints(self) -> list[str]:
        """Return endpoints that respond to a health check, cached for 5 minutes."""
        if self._live_endpoints is not None:
            if time.time() - self._live_endpoints_ts < ENDPOINT_CACHE_TTL:
                return self._live_endpoints
            else:
                logger.debug("[HiFi] Endpoint cache expired — re-validating")
                self._live_endpoints = None

        def _check(ep: str) -> Optional[str]:
            try:
                r = self._session.get(
                    f"{ep}/search/", params={"s": "test"}, timeout=5
                )
                if r.status_code == 200:
                    logger.debug(f"[HiFi] Live: {ep}")
                    return ep
                logger.debug(f"[HiFi] Dead ({r.status_code}): {ep}")
            except Exception as e:
                logger.debug(f"[HiFi] Unreachable: {ep} — {e}")
            return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as ex:
            results = ex.map(_check, ENDPOINTS)

        live = [ep for ep in results if ep is not None]
        # Sort by historical success count (descending)
        live.sort(key=lambda ep: self._endpoint_success.get(ep, 0), reverse=True)

        self._live_endpoints = live if live else list(ENDPOINTS)
        self._live_endpoints_ts = time.time()
        logger.debug(f"[HiFi] {len(self._live_endpoints)}/{len(ENDPOINTS)} endpoints live")
        return self._live_endpoints

    # ── Search ────────────────────────────────────────────────────────────────

    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        """Search all live endpoints in parallel, return best match."""
        endpoints = self._get_live_endpoints()

        # Try ISRC first (exact match, much faster)
        if track.isrc:
            result = self._search_by_isrc(track, endpoints)
            if result:
                return result

        # Fall back to text search
        return self._search_by_text(track, endpoints)

    # Quality preference rank for ISRC search result selection
    _ISRC_QUALITY_RANK: dict = {"HI_RES_LOSSLESS": 0, "LOSSLESS": 1}

    def _search_by_isrc(
        self, track: TrackMetadata, endpoints: list[str]
    ) -> Optional[SearchResult]:
        """Search for track by ISRC across all endpoints in parallel.

        Queries all endpoints simultaneously. Instead of returning the first
        match received (fastest network != best quality), collects every
        ISRC-matched item and picks the highest audioQuality:
          - HI_RES_LOSSLESS found → return immediately (can't do better).
          - Otherwise wait for all futures, then pick the best candidate.
        """
        query = track.isrc

        def _fetch(ep: str) -> Optional[dict]:
            try:
                r = self._session.get(
                    f"{ep}/search/",
                    params={"s": query},
                    timeout=REQUEST_TIMEOUT,
                )
                if r.status_code == 429:
                    self._mark_endpoint_429(ep)
                    return None
                if r.status_code == 200:
                    data = r.json()
                    items = data.get("data", {}).get("items", [])
                    for item in items:
                        if item.get("isrc", "").upper() == track.isrc.upper():
                            return {"item": item, "endpoint": ep}
                else:
                    logger.debug(f"[HiFi] ISRC search {ep} returned {r.status_code}")
            except Exception as e:
                logger.debug(f"[HiFi] ISRC search failed on {ep}: {e}")
            return None

        available = [ep for ep in endpoints if not self._is_endpoint_backed_off(ep)]
        if not available:
            logger.warning("[HiFi] All endpoints backed off — ignoring backoff for this request")
            available = endpoints

        best_match: Optional[dict] = None  # {"item": ..., "endpoint": ...}

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(available)) as ex:
            futures = {ex.submit(_fetch, ep): ep for ep in available}
            for future in concurrent.futures.as_completed(futures):
                hit = future.result()
                if not hit:
                    continue
                quality = str(hit["item"].get("audioQuality", "")).upper()
                # Best possible quality — return immediately
                if quality == "HI_RES_LOSSLESS":
                    for f in futures:
                        f.cancel()
                    return self._item_to_result(
                        hit["item"], hit["endpoint"], isrc_match=True, score=1.0
                    )
                # Keep as candidate; update if this is higher quality than current best
                if best_match is None:
                    best_match = hit
                else:
                    cur_rank = self._ISRC_QUALITY_RANK.get(quality, 99)
                    best_rank = self._ISRC_QUALITY_RANK.get(
                        str(best_match["item"].get("audioQuality", "")).upper(), 99
                    )
                    if cur_rank < best_rank:
                        best_match = hit

        if best_match:
            return self._item_to_result(
                best_match["item"], best_match["endpoint"], isrc_match=True, score=1.0
            )
        return None


    def _search_by_text(
        self, track: TrackMetadata, endpoints: list[str]
    ) -> Optional[SearchResult]:
        """Text search with similarity scoring."""
        # Strip parenthetical suffixes for better matching
        clean_title = re.sub(r"\s*\(.*?\)\s*", "", track.title).strip()
        query = f"{clean_title} {track.primary_artist}"

        def _fetch(ep: str) -> Optional[list[dict]]:
            try:
                r = self._session.get(
                    f"{ep}/search/",
                    params={"s": query},
                    timeout=REQUEST_TIMEOUT,
                )
                if r.status_code == 429:
                    # Rate-limited: back off this endpoint and skip
                    self._mark_endpoint_429(ep)
                    return None
                if r.status_code == 200:
                    return r.json().get("data", {}).get("items", [])
                else:
                    logger.debug(f"[HiFi] Text search {ep} returned {r.status_code}")
            except Exception as e:
                logger.debug(f"[HiFi] Text search failed on {ep}: {e}")
            return None

        best_result: Optional[SearchResult] = None
        best_score = 0.0
        best_ep = endpoints[0]

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(endpoints)) as ex:
            # Check availability, falling back to full list if everything is backed off
            available = [ep for ep in endpoints if not self._is_endpoint_backed_off(ep)]
            if not available:
                logger.warning("[HiFi] All endpoints backed off — ignoring backoff for this request")
                available = endpoints

            futures = {ex.submit(_fetch, ep): ep for ep in available}
            for future in concurrent.futures.as_completed(futures):
                ep = futures[future]
                items = future.result()
                if not items:
                    continue
                for item in items:
                    artist_name = item.get("artist", {}).get("name", "")
                    score = score_similarity(
                        query_title=track.title,
                        query_artists=track.artists,
                        result_title=item.get("title", ""),
                        result_artist=artist_name,
                    )
                    duration = item.get("duration")
                    if duration and track.duration_seconds:
                        if not duration_close(track.duration_seconds, duration, tolerance=10):
                            score *= 0.8

                    if score > best_score:
                        best_score = score
                        best_ep = ep
                        best_result = self._item_to_result(item, ep, score=score)

        if best_result and best_score >= MIN_SIMILARITY:
            logger.debug(f"[HiFi] Best match score={best_score:.2f}: {best_result.title}")
            # Increment success count for the endpoint that provided the best match
            self._endpoint_success[best_ep] = self._endpoint_success.get(best_ep, 0) + 1
            self._consecutive_failures = 0  # Reset throttle counter on success
            return best_result

        self._consecutive_failures += 1
        logger.debug(
            f"[HiFi] No match (best={best_score:.2f}) for: {track.title} "
            f"(consecutive_failures={self._consecutive_failures})"
        )
        return None


    def _item_to_result(
        self,
        item: dict,
        endpoint: str,
        isrc_match: bool = False,
        score: float = 0.0,
    ) -> Optional[SearchResult]:
        try:
            artist = item.get("artist", {}).get("name", "")
            duration = item.get("duration")
            bit_depth = self._infer_bit_depth(item)
            # Store both the track ID and the endpoint so download() knows where to go
            stream_id = f"{endpoint}|{item['id']}"
            return SearchResult(
                source=self.name,
                title=item.get("title", ""),
                artists=[artist],
                album=item.get("album", {}).get("title"),
                duration_ms=int(duration * 1000) if duration else None,
                audio_format=AudioFormat.FLAC,
                quality_kbps=None,
                is_lossless=True,
                download_url=None,
                stream_id=stream_id,
                similarity_score=score,
                isrc_match=isrc_match,
                bit_depth=bit_depth,
            )
        except Exception as e:
            logger.debug(f"[HiFi] Failed to build SearchResult: {e}")
            return None

    @staticmethod
    def _infer_bit_depth(item: dict) -> Optional[int]:
        quality = str(item.get("audioQuality", "")).upper()
        tags = [
            str(tag).upper()
            for tag in (item.get("mediaMetadata", {}) or {}).get("tags", [])
        ]
        blob = " ".join([quality, *tags])
        if "HI_RES" in blob or "HIRES" in blob:
            return 24
        if "LOSSLESS" in blob:
            return 16
        return None

    # ── Download ──────────────────────────────────────────────────────────────

    def download(self, result: SearchResult, output_path: str) -> str:
        """
        Get a streaming URL from the hifi-api /track/ endpoint,
        then download and reassemble the audio segments into a FLAC file.
        """
        if not result.stream_id or "|" not in result.stream_id:
            raise ValueError(f"[HiFi] Invalid stream_id: {result.stream_id}")

        endpoint, track_id = result.stream_id.split("|", 1)
        endpoints_to_try = []
        if endpoint not in self._download_blocklist:
            endpoints_to_try.append(endpoint)
        for candidate in self._get_live_endpoints():
            if candidate in self._download_blocklist:
                continue
            if candidate not in endpoints_to_try:
                endpoints_to_try.append(candidate)

        for candidate_endpoint in endpoints_to_try:
            for quality in QUALITY_LEVELS:
                try:
                    url_data = self._get_stream_url(candidate_endpoint, track_id, quality)
                    if not url_data:
                        continue

                    mime_type = url_data.get("mime_type", "")
                    urls = url_data.get("urls", [])

                    if not urls:
                        continue

                    # Determine output extension
                    if "flac" in mime_type.lower():
                        ext = ".flac"
                    elif "mp4" in mime_type.lower():
                        ext = ".m4a"
                    else:
                        ext = ".flac"

                    final_path = output_path + ext
                    os.makedirs(os.path.dirname(os.path.abspath(final_path)), exist_ok=True)

                    if len(urls) == 1:
                        # Single file — stream directly
                        self._stream_url_to_file(urls[0], final_path)
                    else:
                        # DASH segments — download and concatenate
                        final_path = self._download_segments(urls, output_path, ext)

                    if self._looks_like_preview_clip(final_path, result.duration_ms):
                        self._discard_invalid_download(final_path)
                        raise RuntimeError(
                            f"[HiFi] Rejected short clip for quality={quality}"
                        )

                    logger.debug(
                        f"[HiFi] Fetched {quality} candidate via {candidate_endpoint}: {final_path}"
                    )
                    # Remux M4A→FLAC if Tidal served hi-res FLAC in an M4A container
                    if final_path.endswith(".m4a"):
                        flac_path = final_path[:-4] + ".flac"
                        logger.debug(f"[HiFi] Remuxing M4A→FLAC (lossless container swap): {final_path}")
                        if self._remux_m4a_to_flac(final_path, flac_path):
                            os.remove(final_path)
                            final_path = flac_path
                            logger.debug(f"[HiFi] Remux successful: {flac_path}")
                        else:
                            logger.warning(
                                f"[HiFi] ffmpeg remux failed — keeping M4A. "
                                f"Is ffmpeg installed and on PATH?"
                            )
                    return final_path

                except Exception as e:
                    message = str(e)
                    if "403 client error" in message.lower() or "404 client error" in message.lower():
                        if candidate_endpoint not in self._download_blocklist:
                            self._download_blocklist.add(candidate_endpoint)
                            logger.debug(
                                "[HiFi] Disabling endpoint for this session after /track failure: %s",
                                candidate_endpoint,
                            )
                    logger.debug(
                        f"[HiFi] Failed endpoint={candidate_endpoint} quality={quality}: {e}"
                    )
                    continue

        raise RuntimeError(
            f"[HiFi] All quality levels failed for track {track_id}"
        )

    def should_retry_download(self, result: SearchResult, error: Exception) -> bool:
        message = str(error).lower()
        if (
            "truncated" in message
            or "preview clip" in message
            or "all quality levels failed" in message
        ):
            return False
        return True

    def _get_stream_url(
        self, endpoint: str, track_id: str, quality: str
    ) -> Optional[dict]:
        """
        Call /track/ endpoint and decode the manifest to extract stream URLs.
        Returns dict with 'mime_type' and 'urls' keys.
        """
        try:
            r = self._session.get(
                f"{endpoint}/track/",
                params={"id": track_id, "quality": quality},
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code == 429:
                # Rate-limited on the download path — back off and let caller try next endpoint
                self._mark_endpoint_429(endpoint)
                raise RuntimeError(f"429 rate-limited on {endpoint}")
            r.raise_for_status()
            data = r.json().get("data", {})
        except RuntimeError:
            raise  # Re-raise 429 sentinels directly
        except Exception as e:
            raise RuntimeError(f"/track request failed: {e}") from e

        manifest_b64 = data.get("manifest", "")
        manifest_type = data.get("manifestMimeType", "")

        if not manifest_b64:
            raise RuntimeError("response did not include a manifest")

        try:
            manifest_bytes = base64.b64decode(manifest_b64)
        except Exception as e:
            raise RuntimeError(f"manifest decode failed: {e}") from e

        if "application/vnd.tidal.bts" in manifest_type:
            # BTS manifest = JSON with direct URLs
            import json
            try:
                manifest = json.loads(manifest_bytes)
            except Exception as e:
                raise RuntimeError(f"BTS manifest parse failed: {e}") from e
            return {
                "mime_type": manifest.get("mimeType", "audio/flac"),
                "urls": manifest.get("urls", []),
            }

        elif "application/dash+xml" in manifest_type:
            # DASH manifest = XML with segment template
            try:
                decoded = manifest_bytes.decode("utf-8")
            except Exception as e:
                raise RuntimeError(f"DASH manifest decode failed: {e}") from e
            parsed = self._parse_dash_manifest(decoded)
            if not parsed:
                raise RuntimeError("DASH manifest parse returned no stream data")
            return parsed

        else:
            raise RuntimeError(f"unknown manifest type: {manifest_type or 'missing'}")

    def _parse_dash_manifest(self, xml_str: str) -> Optional[dict]:
        """
        Parse a DASH MPD manifest and return all segment URLs.
        Handles SegmentTemplate with $Number$ patterns.
        """
        try:
            root = ET.fromstring(xml_str)
            ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}

            # Find the representation
            rep = root.find(".//mpd:Representation", ns)
            if rep is None:
                rep = root.find(".//{urn:mpeg:dash:schema:mpd:2011}Representation")

            # Get mime type from AdaptationSet
            adaptation = root.find(
                ".//{urn:mpeg:dash:schema:mpd:2011}AdaptationSet"
            )
            mime_type = "audio/flac"
            if adaptation is not None:
                mime_type = adaptation.get("mimeType", mime_type)

            # Find SegmentTemplate
            seg_tmpl = root.find(
                ".//{urn:mpeg:dash:schema:mpd:2011}SegmentTemplate"
            )
            if seg_tmpl is None:
                return None

            init_url = seg_tmpl.get("initialization", "")
            media_url = seg_tmpl.get("media", "")
            start_number = int(seg_tmpl.get("startNumber", "1"))

            # Count segments from SegmentTimeline
            timeline = root.find(
                ".//{urn:mpeg:dash:schema:mpd:2011}SegmentTimeline"
            )
            segment_count = 0
            if timeline is not None:
                for s in timeline:
                    r = int(s.get("r", "0"))
                    segment_count += r + 1
            else:
                segment_count = 100  # fallback

            urls = []
            if init_url:
                urls.append(init_url)
            for i in range(start_number, start_number + segment_count):
                urls.append(media_url.replace("$Number$", str(i)))

            return {"mime_type": mime_type, "urls": urls}

        except Exception as e:
            logger.debug(f"[HiFi] DASH parse failed: {e}")
            return None

    def _stream_url_to_file(self, url: str, path: str):
        """Download a single URL to a file."""
        with self._session.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(65536):
                    if chunk:
                        f.write(chunk)

    def _download_segments(
        self, urls: list[str], output_base: str, ext: str
    ) -> str:
        """
        Download DASH segments and concatenate them into a single file.
        If ffmpeg is available, uses it to properly remux. Otherwise
        falls back to raw concatenation (works for FLAC segments).
        """
        tmp_dir = tempfile.mkdtemp(prefix="antra_hifi_")
        segment_files = []

        try:
            for i, url in enumerate(urls):
                seg_path = os.path.join(tmp_dir, f"seg_{i:05d}.part")
                try:
                    self._stream_url_to_file(url, seg_path)
                    segment_files.append(seg_path)
                except Exception as e:
                    logger.debug(f"[HiFi] Segment {i} failed: {e}")
                    continue

            if not segment_files:
                raise RuntimeError("No segments downloaded")

            final_path = output_base + ext
            os.makedirs(os.path.dirname(os.path.abspath(final_path)), exist_ok=True)

            # Try ffmpeg concat first (best quality)
            if self._try_ffmpeg_concat(segment_files, final_path):
                return final_path

            # Fallback: raw concatenation
            with open(final_path, "wb") as out:
                for seg in segment_files:
                    with open(seg, "rb") as f:
                        out.write(f.read())

            return final_path

        finally:
            # Clean up temp files
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)


    @staticmethod
    def _remux_m4a_to_flac(input_path: str, output_path: str) -> bool:
        """
        Remux M4A container to FLAC via ffmpeg. No re-encoding — bit-perfect.
        Tidal serves some hi-res FLAC audio in M4A containers; this unwraps it.
        """
        try:
            import subprocess
            from antra.utils.runtime import get_ffmpeg_exe
            ffmpeg = get_ffmpeg_exe() or "ffmpeg"
            result = subprocess.run(
                [
                    ffmpeg, "-y",
                    "-i", input_path,
                    "-c", "copy",
                    "-f", "flac",
                    output_path,
                ],
                capture_output=True,
                timeout=120,
                **_SUBPROCESS_FLAGS,
            )
            return result.returncode == 0
        except Exception as e:
            from logging import getLogger
            getLogger(__name__).debug(f"[HiFi] ffmpeg remux error: {e}")
            return False

    @staticmethod
    def _try_ffmpeg_concat(segment_files: list[str], output_path: str) -> bool:
        """Use ffmpeg to concat segments into a clean output file."""
        try:
            from antra.utils.runtime import get_ffmpeg_exe
            ffmpeg = get_ffmpeg_exe() or "ffmpeg"
            # Write a concat list file
            tmp_list = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False
            )
            for seg in segment_files:
                tmp_list.write(f"file '{seg}'\n")
            tmp_list.close()

            result = subprocess.run(
                [
                    ffmpeg, "-y",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", tmp_list.name,
                    "-c", "copy",
                    output_path,
                ],
                capture_output=True,
                timeout=120,
                **_SUBPROCESS_FLAGS,
            )
            os.unlink(tmp_list.name)
            return result.returncode == 0

        except Exception:
            return False

    @staticmethod
    def _probe_duration_seconds(path: str) -> Optional[float]:
        try:
            audio = MutagenFile(path)
        except Exception:
            return None
        if not audio or not getattr(audio, "info", None):
            return None
        length = getattr(audio.info, "length", None)
        if length is None:
            return None
        try:
            return float(length)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _looks_like_preview_clip(cls, path: str, expected_duration_ms: Optional[int]) -> bool:
        if not expected_duration_ms or expected_duration_ms < 60000:
            return False
        actual_seconds = cls._probe_duration_seconds(path)
        if actual_seconds is None:
            return False
        expected_seconds = expected_duration_ms / 1000.0
        return (
            actual_seconds < expected_seconds * 0.8
            and (expected_seconds - actual_seconds) >= 20
        )

    @staticmethod
    def _discard_invalid_download(path: str) -> None:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _diagnose():
    """Run with: python -m antra.sources.hifi"""
    logging.basicConfig(level=logging.DEBUG)
    adapter = HifiAdapter()
    print("\n=== Endpoint Health Check ===")
    live = adapter._get_live_endpoints()
    print(f"Live: {len(live)}/{len(ENDPOINTS)}")
    for ep in live:
        print(f"  [OK] {ep}")
    dead = [ep for ep in ENDPOINTS if ep not in live]
    for ep in dead:
        print(f"  [FAIL] {ep}")

    print("\n=== Search Test ===")
    from antra.core.models import TrackMetadata
    track = TrackMetadata(
        title="Emerald Rush",
        artists=["Jon Hopkins"],
        album="Singularity",
        duration_ms=333000,
    )
    result = adapter.search(track)
    if result:
        print(f"  Found: {result.title} — {result.artists} "
              f"(score={result.similarity_score:.2f}, "
              f"bit_depth={result.bit_depth}, "
              f"stream_id={result.stream_id})")

        print("\n=== Download & Tag Verification ===")
        test_path = "test_download"
        try:
            # Test download and remuxing
            final_path = adapter.download(result, test_path)
            print(f"  [OK] Downloaded and remuxed to: {final_path}")
            
            # Test tagging
            from antra.utils.tagger import FileTagger
            tagger = FileTagger()
            # Add dummy lyrics for verification
            track.synced_lyrics = "[00:01.00] Test Synced Lyric\n[00:02.00] Out-of-bounds at 1 hour [60:00.00]"
            tagger.tag(final_path, track)
            print(f"  [OK] Tagged file: {final_path}")
            
            # Internal Tag Inspection
            from mutagen.flac import FLAC
            f = FLAC(final_path)
            print(f"  [METADATA] LYRICS present: {'lyrics' in f or 'LYRICS' in f}")
            print(f"  [METADATA] SYNCEDLYRICS present: {'syncedlyrics' in f or 'SYNCEDLYRICS' in f}")
            
            # Verify validation (the 60:00.00 line should be gone)
            sl = f.get('SYNCEDLYRICS', [''])[0]
            if '[60:00.00]' not in sl:
                print("  [OK] LRC validation stripped out-of-range line")
            else:
                print("  [FAIL] LRC validation failed to strip out-of-range line")

        except Exception as e:
            print(f"  [FAIL] Download/Tag test failed: {e}")
        finally:
            # Keep the file if successful for manual inspection, but notify
            print(f"\n  Note: {test_path}.* files remain for manual inspection")
    else:
        print("  No result found")

    print("\n=== Endpoint Success Counts ===")
    for ep, count in sorted(
        adapter._endpoint_success.items(), key=lambda x: x[1], reverse=True
    ):
        print(f"  {count:3d}x {ep}")


def _rate_limit_test():
    """
    Fire 10 searches in rapid succession and observe which endpoints
    back off and how the stagger + backoff system responds.
    Run with: python -c "from antra.sources.hifi import _rate_limit_test; _rate_limit_test()"
    """
    logging.basicConfig(level=logging.INFO)
    adapter = HifiAdapter()
    queries = [
        "Blinding Lights The Weeknd",
        "Bohemian Rhapsody Queen",
        "Smells Like Teen Spirit Nirvana",
        "Hotel California Eagles",
        "Stairway to Heaven Led Zeppelin",
        "Emerald Rush Jon Hopkins",
        "God Only Knows Beach Boys",
        "A Day In The Life Beatles",
        "Lose Yourself Eminem",
        "Mr Brightside The Killers",
    ]
    from antra.core.models import TrackMetadata
    results = []
    for i, q in enumerate(queries):
        parts = q.rsplit(" ", 1)
        track = TrackMetadata(
            title=parts[0], 
            artists=[parts[1]],
            album="",
        )
        print(f"[{i+1:02d}] Searching: {q}")
        result = adapter.search(track)
        results.append(result)
        status = f"OK {result.title} (score={result.similarity_score:.2f})" if result else "FAIL No result"
        print(f"      {status}")

    print(f"\nResults: {sum(1 for r in results if r)}/{len(results)} found")
    print(f"Backed-off endpoints after burst: "
          f"{[ep for ep, ts in adapter._endpoint_backoff.items() if time.time() < ts]}")


if __name__ == "__main__":
    _diagnose()
