import base64
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from typing import Optional
from urllib.parse import urljoin

import requests
from mutagen import File as MutagenFile

from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.sources.base import BaseSourceAdapter, RateLimitedError
from antra.sources.odesli import OdesliEnricher
from antra.core.apple_fetcher import AppleFetcher

logger = logging.getLogger(__name__)


class _DirectAppleClient:
    """
    Direct Apple Music WebPlayback + Widevine CDM client — no proxy server needed.
    Ports core logic from API-Mirrors/apple_api/apple_server.py.
    Requires: authorization (Bearer JWT), music_user_token, storefront, wvd_path.
    """

    _CATALOG_BASE = "https://amp-api.music.apple.com/v1/catalog"
    _WEBPLAYBACK_URL = "https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/webPlayback"
    _LICENSE_URL = "https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/acquireWebPlaybackLicense"
    _WIDEVINE_UUID = "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
    _WIDEVINE_SYSTEM_ID = bytes.fromhex("edef8ba979d64acea3c827dcd51d21ed")
    # NOTE: Apple's WebPlayback API serves AAC for ALL flavors, including 28:ctrp256.
    # Despite the "alac" label in Apple's internal naming, the actual audio data
    # delivered to web clients is AAC-LC (mp4a.40.2) encrypted with Widevine CENC.
    # Real ALAC is only available via extendedAssetUrls.enhancedHls with FairPlay DRM,
    # which requires Apple's proprietary skd:// key delivery — not supported here.
    # We pick the highest-bitrate AAC stream (28:ctrp256 = ~256kbps AAC).
    _FLAVOR_INFO = {
        "28:ctrp256": {"priority": 10, "codec": "aac", "lossless": False},
        "30:cbcp256": {"priority":  5, "codec": "aac", "lossless": False},
        "32:ctrp64":  {"priority":  2, "codec": "aac", "lossless": False},
        "34:cbcp64":  {"priority":  2, "codec": "aac", "lossless": False},
    }
    _LOSSLESS_FLAVORS: set = set()  # WebPlayback API has no lossless flavors

    def __init__(
        self,
        authorization_token: str,
        music_user_token: str,
        storefront: str = "us",
        wvd_path: str = "",
    ):
        self._auth = authorization_token.strip()
        self._mut = music_user_token.strip()
        self._storefront = (storefront or "us").strip()
        self._wvd_path = wvd_path.strip()

    def is_configured(self) -> bool:
        return bool(self._auth and self._mut and self._wvd_path)

    def _amp_headers(self) -> dict:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Authorization": self._auth,
            "Music-User-Token": self._mut,
            "Origin": "https://music.apple.com",
            "Referer": "https://music.apple.com/",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def _play_headers(self) -> dict:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Authorization": self._auth,
            "Media-User-Token": self._mut,
            "Origin": "https://music.apple.com",
            "Referer": "https://music.apple.com/",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _get_webplayback(self, track_id: str) -> dict:
        r = requests.post(
            self._WEBPLAYBACK_URL,
            json={"salableAdamId": str(track_id), "language": "en-US"},
            headers=self._play_headers(),
            timeout=15,
        )
        if r.status_code == 401:
            raise RuntimeError(
                "[Apple-Direct] Authorization expired (401) — re-extract credentials from music.apple.com"
            )
        if r.status_code == 403:
            raise RuntimeError("[Apple-Direct] Subscription does not cover this track (403)")
        r.raise_for_status()
        song_list = r.json().get("songList", [])
        if not song_list:
            raise RuntimeError(f"[Apple-Direct] webPlayback returned empty songList for {track_id}")
        return song_list[0]

    def get_catalog_song(self, track_id: str) -> dict:
        r = requests.get(
            f"{self._CATALOG_BASE}/{self._storefront}/songs/{track_id}",
            headers=self._amp_headers(),
            params={"extend": "extendedAssetUrls", "include": "albums", "platform": "web", "l": "en-US"},
            timeout=15,
        )
        if r.status_code == 401:
            raise RuntimeError("[Apple-Direct] Authorization expired (401)")
        if r.status_code == 404:
            raise RuntimeError(f"[Apple-Direct] Track not found in catalog: {track_id}")
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            raise RuntimeError(f"[Apple-Direct] Empty catalog response for track {track_id}")
        return data[0]

    def lookup_track_by_isrc(self, isrc: str) -> Optional[dict]:
        r = requests.get(
            f"{self._CATALOG_BASE}/{self._storefront}/songs",
            headers=self._amp_headers(),
            params={
                "filter[isrc]": isrc.upper(),
                "extend": "extendedAssetUrls",
                "platform": "web",
                "l": "en-US",
            },
            timeout=15,
        )
        if r.status_code == 401:
            raise RuntimeError("[Apple-Direct] Authorization expired (401)")
        if r.status_code == 404:
            return None
        r.raise_for_status()

        songs = r.json().get("data", [])
        if not songs:
            return None

        def _quality_rank(song: dict) -> int:
            traits = song.get("attributes", {}).get("audioTraits", [])
            if "hi-res-lossless" in traits:
                return 2
            if "lossless" in traits:
                return 1
            return 0

        return max(songs, key=_quality_rank)

    def search_text_track(self, title: str, artist: str = "") -> Optional[dict]:
        term = f"{title} {artist}".strip()
        r = requests.get(
            "https://itunes.apple.com/search",
            params={"term": term, "entity": "song", "media": "music", "limit": 10},
            headers={
                "User-Agent": "iTunes/12.13.0 (Windows; Microsoft Windows 11 x64) AppleWebKit/7614.5.9.1.9",
                "Accept": "application/json",
            },
            timeout=10,
        )
        if not r.ok:
            return None

        songs = [song for song in r.json().get("results", []) if song.get("kind") == "song"]
        if not songs:
            return None

        title_lower = title.lower()
        artist_lower = artist.lower()

        def _score(song: dict) -> int:
            song_title = song.get("trackName", "").lower()
            song_artist = song.get("artistName", "").lower()
            score = 0
            if title_lower == song_title:
                score += 3
            elif title_lower in song_title or song_title in title_lower:
                score += 2
            if artist_lower and (artist_lower in song_artist or song_artist in artist_lower):
                score += 1
            return score

        return max(songs, key=_score)

    def _select_best_asset(self, song: dict) -> dict:
        assets = song.get("assets", [])
        if not assets:
            raise RuntimeError("[Apple-Direct] No assets in webPlayback response")

        def _priority(a: dict) -> int:
            return self._FLAVOR_INFO.get(a.get("flavor", ""), {}).get("priority", 0)

        best = max(assets, key=_priority)
        # Note: Apple's WebPlayback API only serves AAC (no ALAC available via this path).
        # We pick the highest-priority flavor (28:ctrp256 = ~256kbps AAC-LC).
        return best

    @staticmethod
    def _score_variant_line(info_line: str, uri: str) -> int:
        info_lower = info_line.lower()
        uri_lower = uri.lower()
        score = 0
        if "alac" in info_lower or "alac" in uri_lower:
            score += 100
        if "lossless" in info_lower or "lossless" in uri_lower:
            score += 80
        if "hires" in info_lower or "hi-res" in info_lower or "hires" in uri_lower or "hi-res" in uri_lower:
            score += 60
        if "mp4a.40.2" in info_lower or "aac" in info_lower or "aac" in uri_lower:
            score -= 120
        bw_match = re.search(r"BANDWIDTH=(\d+)", info_line, re.IGNORECASE)
        if bw_match:
            try:
                score += int(bw_match.group(1)) // 100000
            except ValueError:
                pass
        return score

    def _select_best_variant_url(self, master_url: str, master_m3u8: str) -> str:
        """
        Pick the highest-quality ALAC variant from an HLS master playlist.
        Only considers EXT-X-STREAM-INF entries (same as apple_server.py).
        Prefers any stream with CODECS=alac or "alac" in the URI, then
        falls back to the highest-bandwidth stream.
        """
        lines = master_m3u8.splitlines()
        best_uri: Optional[str] = None
        best_bandwidth = -1
        best_is_alac = False

        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith("#EXT-X-STREAM-INF:"):
                continue
            if i + 1 >= len(lines):
                continue
            uri = lines[i + 1].strip()
            if not uri or uri.startswith("#"):
                continue

            bw_match = re.search(r"BANDWIDTH=(\d+)", stripped, re.IGNORECASE)
            codec_match = re.search(r'CODECS="([^"]+)"', stripped, re.IGNORECASE)
            bandwidth = int(bw_match.group(1)) if bw_match else 0
            codecs = (codec_match.group(1) if codec_match else "").lower()
            is_alac = "alac" in codecs or "alac" in uri.lower()

            better = (
                best_uri is None
                or (is_alac and not best_is_alac)
                or (is_alac == best_is_alac and bandwidth > best_bandwidth)
            )
            if better:
                best_uri = urljoin(master_url, uri)
                best_bandwidth = bandwidth
                best_is_alac = is_alac

        if not best_uri:
            raise RuntimeError("[Apple-Direct] No variant stream found in master playlist")
        if not best_is_alac:
            raise RuntimeError("[Apple-Direct] Master playlist only exposed lossy AAC variants")
        return best_uri

    def _resolve_lossless_playlist(self, playlist_url: str, playlist_text: str) -> tuple[str, str]:
        current_url = playlist_url
        current_text = playlist_text

        for _ in range(5):
            best_candidate: Optional[str] = None
            try:
                best_candidate = self._select_best_variant_url(current_url, current_text)
            except RuntimeError:
                best_candidate = None

            if not best_candidate or best_candidate == current_url:
                return current_url, current_text

            next_r = requests.get(best_candidate, timeout=15)
            next_r.raise_for_status()
            current_url = best_candidate
            current_text = next_r.text

            if "#EXT-X-STREAM-INF" not in current_text and "#EXT-X-MEDIA:TYPE=AUDIO" not in current_text:
                return current_url, current_text

        return current_url, current_text

    def _extract_pssh_uris(self, content: str) -> list[str]:
        uris: list[str] = []
        seen: set[str] = set()
        for line in content.splitlines():
            line = line.strip()
            if not (line.startswith("#EXT-X-KEY:") or line.startswith("#EXT-X-SESSION-KEY:")):
                continue
            kf = re.search(r'KEYFORMAT="([^"]+)"', line, re.IGNORECASE)
            if kf and kf.group(1).lower() == self._WIDEVINE_UUID:
                uri = re.search(r'URI="data:[^"]*?base64,([^"]+)"', line, re.IGNORECASE)
                if uri and uri.group(1) not in seen:
                    seen.add(uri.group(1))
                    uris.append(uri.group(1))
                continue
            method = re.search(r'METHOD=([^,\s]+)', line)
            if method and method.group(1).upper() == "ISO-23001-7":
                uri = re.search(r'URI="data:[^"]*?base64,([^"]+)"', line, re.IGNORECASE)
                if uri and uri.group(1) not in seen:
                    seen.add(uri.group(1))
                    uris.append(uri.group(1))
        return uris

    def _build_pssh(self, kid: bytes) -> bytes:
        pssh_data = b"\x12\x10" + kid
        box_size = 4 + 4 + 4 + 16 + 4 + len(pssh_data)
        return (
            box_size.to_bytes(4, "big") + b"pssh"
            + b"\x00\x00\x00\x00" + self._WIDEVINE_SYSTEM_ID
            + len(pssh_data).to_bytes(4, "big") + pssh_data
        )

    def _pssh_to_kid_hex(self, pssh_b64: str) -> Optional[str]:
        try:
            raw = base64.b64decode(pssh_b64)
        except Exception:
            return None
        if len(raw) <= 16:
            return raw.rjust(16, b"\x00").hex()
        try:
            from pywidevine.pssh import PSSH
            pssh = PSSH(pssh_b64)
            key_ids = getattr(pssh, "key_ids", None) or []
            if key_ids:
                first = key_ids[0]
                return getattr(first, "hex", lambda: str(first).replace("-", ""))()
        except Exception:
            pass
        return None

    def _get_content_keys(self, pssh_b64: str, variant_url: str, track_id: str) -> dict[str, str]:
        try:
            from pywidevine.cdm import Cdm
            from pywidevine.device import Device
            from pywidevine.pssh import PSSH
        except ImportError:
            raise RuntimeError(
                "[Apple-Direct] pywidevine not installed — run: pip install pywidevine==1.8.0 construct==2.8.8"
            )

        device = Device.load(self._wvd_path)
        cdm = Cdm.from_device(device)
        session_id = cdm.open()
        try:
            raw = base64.b64decode(pssh_b64)
            if len(raw) <= 16:
                pssh = PSSH(self._build_pssh(raw.rjust(16, b"\x00")))
            else:
                pssh = PSSH(pssh_b64)

            challenge = cdm.get_license_challenge(session_id, pssh)
            challenge_b64 = base64.b64encode(challenge).decode()

            body = {
                "challenge": challenge_b64,
                "key-system": "com.widevine.alpha",
                "uri": f"data:;base64,{pssh_b64}",
                "adamId": str(track_id),
                "isLibrary": False,
                "user-initiated": True,
            }
            r = requests.post(self._LICENSE_URL, json=body, headers=self._play_headers(), timeout=20)
            if r.status_code == 401:
                raise RuntimeError("[Apple-Direct] License rejected (401) — token expired")
            if r.status_code == 403:
                raise RuntimeError("[Apple-Direct] License rejected (403) — subscription issue")
            r.raise_for_status()
            data = r.json()
            if "license" not in data:
                raise RuntimeError(f"[Apple-Direct] No 'license' in license response: {data}")
            cdm.parse_license(session_id, base64.b64decode(data["license"]))
            keys = cdm.get_keys(session_id)
            content_keys = [k for k in keys if k.type == "CONTENT"]
            if not content_keys:
                raise RuntimeError("[Apple-Direct] No CONTENT keys in Widevine license")
            return {k.kid.hex: k.key.hex() for k in content_keys}
        finally:
            cdm.close(session_id)

    def process_track(self, track_id: str) -> dict:
        """
        Returns {streamUrl, decryptionKey, keyMap, codec, quality, bitDepth, sampleRate}.
        Raises RuntimeError with a descriptive message on any failure.
        """
        # 1. Catalog lookup — quality metadata
        r = requests.get(
            f"{self._CATALOG_BASE}/{self._storefront}/songs/{track_id}",
            headers=self._amp_headers(),
            params={"extend": "extendedAssetUrls", "include": "albums", "platform": "web", "l": "en-US"},
            timeout=15,
        )
        if r.status_code == 401:
            raise RuntimeError("[Apple-Direct] Authorization expired (401)")
        if r.status_code == 404:
            raise RuntimeError(f"[Apple-Direct] Track not found in catalog: {track_id}")
        r.raise_for_status()
        try:
            attrs = r.json()["data"][0].get("attributes", {})
        except (KeyError, IndexError):
            attrs = {}

        traits = attrs.get("audioTraits", [])
        # Note: catalog traits reflect what Apple offers in general (ALAC via FairPlay).
        # The WebPlayback API only serves AAC regardless of catalog quality.
        # We report the catalog quality for metadata purposes but the actual stream is AAC.
        if "hi-res-lossless" in traits:
            quality, bit_depth, sample_rate = "HIRES_LOSSLESS", 24, None
        elif "lossless" in traits:
            quality, bit_depth, sample_rate = "LOSSLESS", 16, 44100
        else:
            quality, bit_depth, sample_rate = "LOSSY", None, None

        # 2. webPlayback → asset URL
        song = self._get_webplayback(track_id)
        asset = self._select_best_asset(song)
        asset_url = asset.get("URL") or asset.get("url")
        if not asset_url:
            raise RuntimeError(f"[Apple-Direct] No URL in selected asset for track {track_id}")

        master_r = requests.get(asset_url, timeout=15)
        master_r.raise_for_status()
        playlist_text = master_r.text

        if "#EXT-X-STREAM-INF" in playlist_text or "#EXT-X-MEDIA:TYPE=AUDIO" in playlist_text:
            lines = playlist_text.splitlines()
            variant_url, variant_m3u8 = self._resolve_lossless_playlist(asset_url, playlist_text)

            # Extract bit_depth / sample_rate from AUDIO group metadata in master
            for i, line in enumerate(lines):
                if not line.strip().startswith("#EXT-X-STREAM-INF:"):
                    continue
                if i + 1 >= len(lines):
                    continue
                if urljoin(asset_url, lines[i + 1].strip()) != variant_url:
                    continue
                audio_m = re.search(r'AUDIO="([^"]+)"', line)
                if audio_m:
                    ag = audio_m.group(1)
                    for l2 in lines:
                        if f'GROUP-ID="{ag}"' in l2 and "TYPE=AUDIO" in l2:
                            sr_m = re.search(r"SAMPLE-RATE=(\d+)", l2)
                            bd_m = re.search(r"BIT-DEPTH=(\d+)", l2)
                            if sr_m:
                                sample_rate = int(sr_m.group(1))
                            if bd_m:
                                bit_depth = int(bd_m.group(1))
                break
        else:
            variant_url = asset_url
            variant_m3u8 = playlist_text

        # 3. Extract Widevine PSHPs from variant playlist
        key_uris = self._extract_pssh_uris(variant_m3u8)
        if not key_uris:
            raise RuntimeError("[Apple-Direct] No Widevine key URIs found in variant playlist")

        # 4. CDM challenge → Apple license server → content keys
        keys_by_kid = self._get_content_keys(key_uris[0], variant_url, track_id)

        key_map: dict[str, str] = {}
        for uri_b64 in key_uris:
            kid_hex = self._pssh_to_kid_hex(uri_b64)
            if kid_hex and kid_hex in keys_by_kid:
                key_map[uri_b64] = keys_by_kid[kid_hex]

        if not key_map:
            raise RuntimeError("[Apple-Direct] Keys obtained but none matched playlist key IDs")

        # WebPlayback API always serves AAC regardless of flavor label
        codec = "aac"
        return {
            "streamUrl": variant_url,
            "decryptionKey": next(iter(key_map.values())),
            "keyMap": key_map,
            "codec": codec,
            "quality": quality,
            "bitDepth": bit_depth,
            "sampleRate": sample_rate,
        }

_APPLE_API_REQUEST_SPACING_SECONDS = 2.5
_APPLE_429_RETRY_DELAYS = (3.0, 6.0, 10.0)

# On Windows, prevent subprocess from flashing a console window
_SUBPROCESS_FLAGS = {}
if sys.platform == "win32":
    _SUBPROCESS_FLAGS["creationflags"] = subprocess.CREATE_NO_WINDOW


class AppleAdapter(BaseSourceAdapter):
    """
    Apple Music adapter — downloads AAC (~256kbps) via WebPlayback API + Widevine CENC.

    NOTE: Apple's WebPlayback API only serves AAC to web clients. Real ALAC streams
    require FairPlay DRM (skd:// protocol) which cannot be decrypted with pywidevine.
    This adapter is always_lossy=True and is skipped in lossless/ALAC/FLAC mode.
    It is only used as a fallback when the output format is AAC or MP3.
    """

    name = "apple"
    always_lossy = True  # WebPlayback API serves AAC only — never lossless

    def __init__(
        self,
        mirrors: list[str],
        api_key: Optional[str] = None,
        authorization_token: str = "",
        music_user_token: str = "",
        storefront: str = "us",
        wvd_path: str = "",
    ):
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        })
        self._odesli = OdesliEnricher(api_key=api_key)
        self._apple_fetcher = AppleFetcher()
        self.priority = 2

        self._mirrors = [m.rstrip("/") for m in mirrors if m]
        self._current_mirror: Optional[str] = None
        self._mirror_failures: dict[str, int] = {}
        self._request_lock = threading.Lock()
        self._next_request_at = 0.0

        # Direct auth client (user's own Apple Music account — preferred over mirrors)
        self._direct = _DirectAppleClient(
            authorization_token=authorization_token,
            music_user_token=music_user_token,
            storefront=storefront,
            wvd_path=wvd_path,
        ) if (authorization_token and music_user_token) else None
        if self._direct and self._direct.is_configured():
            logger.info("[Apple-Direct] Direct credentials loaded — will use WebPlayback API directly")

    def _get_working_mirror(self, force_rotate: bool = False) -> str:
        if self._current_mirror and not force_rotate:
            return self._current_mirror

        valid_mirrors = [m for m in self._mirrors if self._mirror_failures.get(m, 0) < 3]
        if not valid_mirrors:
            logger.debug("[Apple] All mirrors failed health checks. Resetting pool.")
            valid_mirrors = self._mirrors
            self._mirror_failures.clear()

        for mirror in valid_mirrors:
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

        if self._mirrors:
            return self._mirrors[0]
        
        raise RuntimeError("[Apple] No mirrors configured.")

    def _wait_for_request_slot(self) -> None:
        with self._request_lock:
            now = time.monotonic()
            sleep_for = self._next_request_at - now
            if sleep_for > 0:
                time.sleep(sleep_for)
                now = time.monotonic()
            self._next_request_at = now + _APPLE_API_REQUEST_SPACING_SECONDS

    def _api_get(self, url: str, **kwargs) -> requests.Response:
        self._wait_for_request_slot()
        return self._session.get(url, **kwargs)

    @staticmethod
    def _coerce_lossless_metadata(data: dict, bit_depth: int, sample_rate: int) -> tuple[int, int]:
        quality = str(data.get("quality") or "").upper()
        reported_bit_depth = data.get("bitDepth")
        reported_sample_rate = data.get("sampleRate")

        try:
            if reported_bit_depth is not None:
                bit_depth = int(reported_bit_depth)
        except (TypeError, ValueError):
            pass

        try:
            if reported_sample_rate is not None:
                sample_rate = int(reported_sample_rate)
        except (TypeError, ValueError):
            pass

        if quality == "HIRES_LOSSLESS" and bit_depth < 24:
            bit_depth = 24

        return bit_depth, sample_rate

    def is_available(self) -> bool:
        try:
            from antra.utils.runtime import get_ffmpeg_exe
            ffmpeg = get_ffmpeg_exe() or "ffmpeg"
            subprocess.run([ffmpeg, "-version"], capture_output=True, check=True, **_SUBPROCESS_FLAGS)
        except Exception:
            return False

        if self._direct and self._direct.is_configured():
            return True
        if self._mirrors:
            try:
                self._get_working_mirror()
                return True
            except Exception:
                return False
        return False

    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        apple_id = None
        bit_depth = 16
        sample_rate = 44100
        isrc_match = False

        # Prefer direct account lookup when available so Apple downloads work
        # without a mirror dependency.
        if self._direct and self._direct.is_configured() and track.isrc:
            try:
                song = self._direct.lookup_track_by_isrc(track.isrc)
                if song:
                    attrs = song.get("attributes", {})
                    apple_id = song.get("id")
                    traits = attrs.get("audioTraits", [])
                    if "hi-res-lossless" in traits:
                        bit_depth = 24
                        sample_rate = 96000
                    elif "lossless" in traits:
                        bit_depth = 16
                        sample_rate = 44100
                    isrc_match = True
                    logger.debug(f"[Apple-Direct] Resolved {track.isrc} to Apple Music ID: {apple_id}")
            except Exception as e:
                logger.debug(f"[Apple-Direct] ISRC search failed: {e}")

        # Try exact match using ISRC via API proxy
        if not apple_id and track.isrc:
            try:
                mirror = self._get_working_mirror()
                r = self._api_get(f"{mirror}/api/search/isrc/{track.isrc}", timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    apple_id = data.get("track_id")
                    bit_depth, sample_rate = self._coerce_lossless_metadata(data, bit_depth, sample_rate)
                    isrc_match = True
                    logger.debug(f"[Apple] Resolved {track.isrc} to Apple Music ID: {apple_id}")
            except Exception as e:
                logger.debug(f"[Apple] ISRC proxy search failed: {e}")

        # Fallback to Odesli
        if not apple_id:
            logger.debug(f"[Apple] Resolving ID via Odesli: {track.title}")
            platform_ids = self._odesli.resolve(track)
            apple_id = platform_ids.get("appleMusic")

        # Direct text search works without mirrors and gives us a track ID
        # for the WebPlayback downloader to use.
        if not apple_id and self._direct and self._direct.is_configured():
            try:
                song = self._direct.search_text_track(track.title, track.primary_artist)
                if song:
                    apple_id = str(song.get("trackId") or "")
                    logger.debug(f"[Apple-Direct] Text search found track_id={apple_id} for '{track.title}'")
            except Exception as e:
                logger.debug(f"[Apple-Direct] Text search failed: {e}")

        # Final fallback: text search via proxy (for tracks without ISRCs when Odesli fails)
        if not apple_id:
            try:
                mirror = self._get_working_mirror()
                r = self._api_get(
                    f"{mirror}/api/search",
                    params={"title": track.title, "artist": track.primary_artist},
                    timeout=15,
                )
                if r.status_code == 200:
                    data = r.json()
                    apple_id = data.get("track_id")
                    bit_depth, sample_rate = self._coerce_lossless_metadata(data, bit_depth, sample_rate)
                    logger.debug(f"[Apple] Text search found track_id={apple_id} for '{track.title}'")
            except Exception as e:
                logger.debug(f"[Apple] Text search fallback failed: {e}")

        if not apple_id:
            logger.debug(f"[Apple] No Apple Music ID found for '{track.title}'")
            return None

        # Check the catalog for hi-res traits if we don't already know the quality
        if apple_id and bit_depth < 24:
            try:
                if self._direct and self._direct.is_configured():
                    song = self._direct.get_catalog_song(str(apple_id))
                    traits = song.get("attributes", {}).get("audioTraits", [])
                    if "hi-res-lossless" in traits:
                        logger.debug(f"[Apple-Direct] Catalog confirmed hi-res lossless for Apple Music ID {apple_id}")
                        bit_depth = 24
                        if sample_rate < 48000:
                            sample_rate = 96000
                    elif "lossless" in traits and sample_rate < 44100:
                        sample_rate = 44100
                else:
                    # _fetch_song queries the Catalog API via developer token (no Widevine overhead)
                    meta = self._apple_fetcher._fetch_song(str(apple_id))
                    if meta and "hi-res-lossless" in (meta.audio_traits or []):
                        logger.debug(f"[Apple] Catalog confirmed hi-res lossless for Apple Music ID {apple_id}")
                        bit_depth = 24
                        if sample_rate < 48000:
                            sample_rate = 96000 # Default assumption for hi-res if unknown
            except Exception as e:
                logger.debug(f"[Apple] Failed to check catalog for hi-res traits: {e}")

        return SearchResult(
            source="apple",
            title=track.title,
            artists=track.artists,
            album=track.album,
            duration_ms=track.duration_ms,
            audio_format=AudioFormat.AAC,
            quality_kbps=256,  # WebPlayback API serves ~256kbps AAC (flavor 28:ctrp256)
            is_lossless=False,
            bit_depth=None,
            sample_rate_hz=None,
            download_url=None,
            stream_id=str(apple_id),
            similarity_score=1.0,
            isrc_match=isrc_match,
        )

    def _apply_quality_from_data(self, result: SearchResult, data: dict) -> None:
        quality = str(data.get("quality") or "").upper()
        if quality == "HIRES_LOSSLESS":
            result.bit_depth = 24
        elif data.get("bitDepth"):
            try:
                result.bit_depth = int(data["bitDepth"])
            except (ValueError, TypeError):
                pass
        if data.get("sampleRate"):
            try:
                result.sample_rate_hz = int(data["sampleRate"])
            except (ValueError, TypeError):
                pass

    def download(self, result: SearchResult, output_path: str) -> str:
        track_id = result.stream_id
        if not track_id:
            raise ValueError("[Apple] Missing Apple track_id in search result")

        # Try direct WebPlayback + Widevine path first — avoids proxy latency
        if self._direct and self._direct.is_configured():
            try:
                logger.info(f"[Apple-Direct] Fetching stream via WebPlayback API for track {track_id}...")
                data = self._direct.process_track(track_id)
                self._apply_quality_from_data(result, data)
                stream_url = data["streamUrl"]
                decryption_key = data["decryptionKey"]
                key_map = data.get("keyMap")
                expected_codec = str(data.get("codec") or "aac").lower()
                # Apple's WebPlayback API serves AAC (not ALAC) — accept it
                return self._process_download(stream_url, decryption_key, output_path,
                                              expected_codec=expected_codec, key_map=key_map)
            except Exception as e:
                err = str(e)
                logger.warning(f"[Apple-Direct] Direct auth failed: {err}")
                if "401" in err or "expired" in err.lower():
                    raise RuntimeError(
                        f"[Apple-Direct] Credentials expired — re-extract from music.apple.com: {err}"
                    )
                if not self._mirrors:
                    raise
                logger.info("[Apple-Direct] Falling back to mirror pool...")

        if not self._mirrors:
            raise RuntimeError("[Apple] No direct credentials and no mirrors configured")

        max_attempts = len(self._mirrors)
        last_error = None
        saw_rate_limit = False

        for attempt in range(max_attempts):
            mirror = self._get_working_mirror(force_rotate=(attempt > 0))
            api_url = f"{mirror}/api/track/{track_id}"

            try:
                logger.debug(f"[Apple] Fetching stream info (attempt {attempt+1}/{max_attempts}) from {mirror}...")
                resp = None
                for rate_attempt, retry_delay in enumerate((0.0, *_APPLE_429_RETRY_DELAYS), start=1):
                    if retry_delay > 0:
                        logger.debug(
                            f"[Apple] Backing off {retry_delay:.1f}s after 429 "
                            f"(track_id={track_id}, retry {rate_attempt}/{len(_APPLE_429_RETRY_DELAYS) + 1})"
                        )
                        time.sleep(retry_delay)
                    resp = self._api_get(api_url, timeout=20)
                    if resp.status_code != 429:
                        break
                if resp is None:
                    raise RuntimeError("[Apple] No response from proxy")

                if resp.status_code == 200:
                    data = resp.json()
                    stream_url = data.get("streamUrl")
                    decryption_key = data.get("decryptionKey")
                    key_map = data.get("keyMap")
                    expected_codec = str(data.get("codec") or "aac").lower()

                    self._apply_quality_from_data(result, data)

                    if not stream_url or not decryption_key:
                        raise RuntimeError("Missing streamUrl or decryptionKey in proxy response")

                    return self._process_download(
                        stream_url,
                        decryption_key,
                        output_path,
                        expected_codec=expected_codec,
                        key_map=key_map,
                    )

                if resp.status_code == 429:
                    saw_rate_limit = True
                    raise RateLimitedError(f"[Apple] Rate limited (429) on {mirror}")

                if resp.status_code in (403, 503):
                    self._mirror_failures[mirror] = 99
                    self._current_mirror = None
                    last_error = f"API error {resp.status_code}"
                    continue

                last_error = f"API error {resp.status_code}"
            except RateLimitedError as e:
                last_error = str(e)
                self._mirror_failures[mirror] = self._mirror_failures.get(mirror, 0) + 1
                continue
            except Exception as e:
                last_error = str(e)

            self._mirror_failures[mirror] = self._mirror_failures.get(mirror, 0) + 1

        if saw_rate_limit:
            raise RateLimitedError(last_error or "[Apple] Rate limited")
        raise RuntimeError(f"[Apple] All mirrors failed. Last error: {last_error}")

    def should_retry_download(self, result: SearchResult, error: Exception) -> bool:
        if isinstance(error, RateLimitedError):
            return False
        if "404" in str(error):
            return False
        return True

    def _process_download(
        self,
        stream_url: str,
        decryption_key_hex: str,
        output_path: str,
        expected_codec: str = "aac",
        key_map: Optional[dict[str, str]] = None,
    ) -> str:
        # Follow any master playlist to the correct variant URL first.
        # process_track() and the proxy server already resolve this, but we
        # re-resolve here as a safety net in case the caller passes a master URL.
        variant_url, _ = self._resolve_playlist(stream_url)

        final_path = output_path + ".m4a"
        os.makedirs(os.path.dirname(os.path.abspath(final_path)), exist_ok=True)

        from antra.utils.runtime import get_ffmpeg_exe
        ffmpeg = get_ffmpeg_exe() or "ffmpeg"

        # Apple Music uses CENC (ISO-23001-7) encryption.
        # Try ffmpeg -decryption_key first (requires a full ffmpeg build).
        # If that option is not available (essentials build), fall back to Python
        # segment-by-segment download + AES-CTR decryption.
        logger.debug("[Apple] Attempting download via ffmpeg -decryption_key...")
        result = subprocess.run(
            [
                ffmpeg, "-y",
                "-decryption_key", decryption_key_hex.strip(),
                "-allowed_extensions", "ALL",
                "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                "-i", variant_url,
                "-c", "copy",
                final_path,
            ],
            capture_output=True,
            timeout=300,
            **_SUBPROCESS_FLAGS,
        )

        if result.returncode == 0:
            self._validate_downloaded_file(final_path, expected_codec=expected_codec)
            return final_path

        stderr = result.stderr.decode("utf-8", errors="ignore")
        if "decryption_key" in stderr and ("not found" in stderr.lower() or "Option" in stderr):
            logger.debug("[Apple] ffmpeg -decryption_key unavailable (essentials build), using Python CENC fallback...")
            return self._download_and_decrypt_cenc_hls(
                variant_url, decryption_key_hex, final_path, expected_codec
            )

        raise RuntimeError(f"[Apple] ffmpeg CENC decryption failed (exit {result.returncode}): {stderr.strip()[-500:]}")

    def _download_and_decrypt_cenc_hls(
        self,
        m3u8_url: str,
        key_hex: str,
        final_path: str,
        expected_codec: str = "aac",
    ) -> str:
        """
        Download Apple Music HLS CENC stream and decrypt with Python AES-CTR.
        Handles both BYTERANGE playlists (single-file, all segments are byte
        ranges) and standard HLS (separate segment files).
        Used when ffmpeg -decryption_key is unavailable.
        """
        resp = self._api_get(m3u8_url, timeout=15)
        resp.raise_for_status()
        content = resp.text
        base = m3u8_url.rsplit("/", 1)[0] + "/"

        # Parse M3U8 — collect init info and segment URLs with optional BYTERANGE
        init_url: Optional[str] = None
        init_byterange: Optional[tuple[int, int]] = None  # (length, offset)
        seg_entries: list[tuple[str, Optional[tuple[int, int]]]] = []
        pending_byterange: Optional[tuple[int, int]] = None
        last_offset = 0

        for line in content.splitlines():
            s = line.strip()
            if s.startswith("#EXT-X-MAP:"):
                uri_m = re.search(r'URI="([^"]+)"', s)
                br_m = re.search(r'BYTERANGE="(\d+)@(\d+)"', s)
                if uri_m:
                    init_url = urljoin(base, uri_m.group(1))
                    if br_m:
                        init_byterange = (int(br_m.group(1)), int(br_m.group(2)))
            elif s.startswith("#EXT-X-BYTERANGE:"):
                br_m = re.search(r'(\d+)(?:@(\d+))?', s)
                if br_m:
                    length = int(br_m.group(1))
                    offset = int(br_m.group(2)) if br_m.group(2) is not None else last_offset
                    pending_byterange = (length, offset)
                    last_offset = offset + length
            elif s and not s.startswith("#"):
                seg_url = urljoin(base, s)
                seg_entries.append((seg_url, pending_byterange))
                if pending_byterange is None:
                    last_offset = 0
                pending_byterange = None

        if not seg_entries:
            raise RuntimeError("[Apple] No segments found in HLS variant playlist")

        enc_path = final_path + ".enc"
        try:
            # Detect BYTERANGE playlist: all segments are ranges of the same base file.
            seg_urls_only = [u for u, _ in seg_entries]
            all_byterange = all(br is not None for _, br in seg_entries)
            single_file = len(set(seg_urls_only)) == 1

            if all_byterange and single_file:
                # All segments (and init) are byte ranges of one MP4 file — download it whole.
                mp4_url = seg_urls_only[0]
                logger.debug(f"[Apple] BYTERANGE playlist — downloading full MP4 from {mp4_url}")
                r = requests.get(mp4_url, timeout=300)
                r.raise_for_status()
                with open(enc_path, "wb") as f:
                    f.write(r.content)
            else:
                # Standard HLS: download init + individual segments and concatenate.
                logger.debug(f"[Apple] Downloading {len(seg_entries)} segments + init via Python...")
                with open(enc_path, "wb") as f:
                    if init_url:
                        hdr = {}
                        if init_byterange:
                            ln, off = init_byterange
                            hdr["Range"] = f"bytes={off}-{off+ln-1}"
                        r = requests.get(init_url, headers=hdr, timeout=30)
                        r.raise_for_status()
                        f.write(r.content)
                    for seg_url, byterange in seg_entries:
                        hdr = {}
                        if byterange:
                            ln, off = byterange
                            hdr["Range"] = f"bytes={off}-{off+ln-1}"
                        r = requests.get(seg_url, headers=hdr, timeout=60)
                        r.raise_for_status()
                        f.write(r.content)

            err = self._decrypt_cenc_python(enc_path, final_path, key_hex)
            if err:
                raise RuntimeError(f"[Apple] Python CENC decryption failed: {err}")
        finally:
            if os.path.exists(enc_path):
                os.remove(enc_path)

        self._validate_downloaded_file(final_path, expected_codec=expected_codec)
        return final_path

    @staticmethod
    def _decrypt_cenc_python(input_path: str, output_path: str, key_hex: str) -> Optional[str]:
        """
        Pure-Python AES-CTR CENC decryption for CMAF/fMP4 streams.
        Handles fragmented MP4 as produced by Apple Music HLS.
        Returns None on success, or an error string on failure.
        """
        try:
            from Cryptodome.Cipher import AES
        except ImportError:
            return "Cryptodome not available — run: pip install pycryptodomex"

        try:
            key = bytes.fromhex(key_hex.strip())
        except Exception as e:
            return f"Invalid key hex: {e}"
        if len(key) not in (16, 24, 32):
            return f"Key must be 16/24/32 bytes, got {len(key)}"

        def read_box(d, pos):
            if pos + 8 > len(d):
                return None
            sz = struct.unpack_from(">I", d, pos)[0]
            bt = d[pos + 4:pos + 8].decode("latin-1", errors="replace")
            if sz == 1:
                if pos + 16 > len(d):
                    return None
                sz = struct.unpack_from(">Q", d, pos + 8)[0]
                return sz, bt, 16
            if sz < 8:
                return None
            return sz, bt, 8

        def find_first(d, name):
            pos = 0
            while pos < len(d):
                r = read_box(d, pos)
                if r is None:
                    break
                sz, bt, hs = r
                if bt == name:
                    return d[pos + hs:pos + sz]
                pos += sz
            return None

        def parse_senc(d):
            if len(d) < 8:
                return 8, []
            flags = struct.unpack_from(">I", d, 0)[0] & 0xFFFFFF
            iv_size = 8
            off = 4
            if flags & 1:
                if off + 20 > len(d):
                    return 8, []
                iv_size = d[off + 3]
                off += 20
            cnt = struct.unpack_from(">I", d, off)[0]
            off += 4
            result = []
            for _ in range(cnt):
                if off + iv_size > len(d):
                    break
                iv = bytes(d[off:off + iv_size])
                off += iv_size
                subs = None
                if flags & 2:
                    if off + 2 > len(d):
                        break
                    sc = struct.unpack_from(">H", d, off)[0]
                    off += 2
                    subs = []
                    for _ in range(sc):
                        if off + 6 > len(d):
                            break
                        subs.append((struct.unpack_from(">H", d, off)[0],
                                     struct.unpack_from(">I", d, off + 2)[0]))
                        off += 6
                result.append((iv, subs))
            return iv_size, result

        def parse_trun(d):
            if len(d) < 8:
                return None, []
            flags = struct.unpack_from(">I", d, 0)[0] & 0xFFFFFF
            cnt = struct.unpack_from(">I", d, 4)[0]
            off = 8
            doff = None
            if flags & 0x001:
                doff = struct.unpack_from(">i", d, off)[0]
                off += 4
            if flags & 0x004:
                off += 4
            sizes = []
            for _ in range(cnt):
                sz = 0
                if flags & 0x100:
                    off += 4
                if flags & 0x200:
                    sz = struct.unpack_from(">I", d, off)[0]
                    off += 4
                if flags & 0x400:
                    off += 4
                if flags & 0x800:
                    off += 4
                sizes.append(sz)
            return doff, sizes

        try:
            with open(input_path, "rb") as f:
                raw = bytearray(f.read())
        except Exception as e:
            return f"Cannot read encrypted file: {e}"

        pos, n, changed = 0, len(raw), 0
        while pos < n:
            r = read_box(raw, pos)
            if r is None:
                break
            moof_sz, bt, moof_hs = r
            if bt != "moof":
                pos += moof_sz
                continue
            moof_start = pos
            moof_end = pos + moof_sz
            traf = find_first(raw[pos + moof_hs:moof_end], "traf")
            if traf is None:
                pos = moof_end
                continue
            senc_raw = find_first(traf, "senc")
            trun_raw = find_first(traf, "trun")
            if senc_raw is None or trun_raw is None:
                pos = moof_end
                continue
            _, samples = parse_senc(senc_raw)
            doff, sizes = parse_trun(trun_raw)
            mr = read_box(raw, moof_end)
            if mr is None or mr[1] != "mdat":
                pos = moof_end
                continue
            mdat_sz, _, mdat_hs = mr
            sample_pos = (moof_start + doff) if doff is not None else (moof_end + mdat_hs)
            for idx, (iv, subs) in enumerate(samples):
                s_sz = sizes[idx] if idx < len(sizes) else 0
                if s_sz == 0:
                    continue
                iv16 = iv.ljust(16, b"\x00")
                cipher = AES.new(key, AES.MODE_CTR, initial_value=iv16, nonce=b"")
                if subs:
                    cur = sample_pos
                    for clear, enc in subs:
                        cur += clear
                        if enc > 0:
                            raw[cur:cur + enc] = cipher.decrypt(bytes(raw[cur:cur + enc]))
                        cur += enc
                else:
                    raw[sample_pos:sample_pos + s_sz] = cipher.decrypt(
                        bytes(raw[sample_pos:sample_pos + s_sz])
                    )
                sample_pos += s_sz
                changed += 1
            pos = moof_end + mdat_sz

        if not changed:
            return "No CENC samples found — file may not be fragmented MP4 or is not CENC-encrypted"

        # Patch enca → mp4a in the stsd box so the container reports the correct codec.
        # After CENC decryption the sample data is plain AAC, but the stsd still says
        # 'enca' (encrypted audio). Replace the first occurrence of b'enca' with b'mp4a'
        # so players and mutagen can identify the codec correctly.
        enca_idx = raw.find(b"enca")
        if enca_idx >= 0:
            raw[enca_idx:enca_idx + 4] = b"mp4a"

        try:
            with open(output_path, "wb") as f:
                f.write(raw)
        except Exception as e:
            return f"Cannot write decrypted file: {e}"

        return None

    @staticmethod
    def _rewrite_hls_playlist(
        m3u8_content: str,
        stream_url: str,
        temp_dir: str,
        default_key_path: str,
        key_map: Optional[dict[str, str]] = None,
    ) -> str:
        base_url = stream_url.rsplit("/", 1)[0] + "/"
        lines: list[str] = []
        key_file_by_uri: dict[str, str] = {}
        key_index = 0

        for line in m3u8_content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#EXT-X-KEY:"):
                uri_match = re.search(r'URI="([^"]+)"', stripped)
                keyformat_match = re.search(r'KEYFORMAT="([^"]+)"', stripped, re.IGNORECASE)
                uri = uri_match.group(1) if uri_match else ""
                keyformat = keyformat_match.group(1).lower() if keyformat_match else ""

                local_key_path: Optional[str] = None
                if uri in key_file_by_uri:
                    local_key_path = key_file_by_uri[uri]
                elif key_map and uri.startswith("data:text/plain;base64,"):
                    uri_b64 = uri.split("base64,", 1)[1]
                    key_hex = key_map.get(uri_b64)
                    if key_hex:
                        key_index += 1
                        local_key_path = os.path.join(temp_dir, f"apple_{key_index}.key")
                        with open(local_key_path, "wb") as f:
                            f.write(bytes.fromhex(key_hex.strip()))
                        key_file_by_uri[uri] = local_key_path
                elif keyformat == "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed":
                    local_key_path = default_key_path
                    key_file_by_uri[uri] = local_key_path

                if local_key_path:
                    lines.append(f'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="{local_key_path}"')
                continue

            if 'URI="' in stripped and stripped.startswith("#"):
                lines.append(re.sub(
                    r'URI="([^"]+)"',
                    lambda m: f'URI="{urljoin(base_url, m.group(1))}"',
                    line,
                ))
                continue

            if stripped and not stripped.startswith("#"):
                lines.append(urljoin(base_url, stripped))
            else:
                lines.append(line)
        return "\n".join(lines)

    def _resolve_playlist(self, stream_url: str) -> tuple[str, str]:
        current_url = stream_url

        for _ in range(5):
            response = self._api_get(current_url, timeout=15)
            response.raise_for_status()
            content = response.text

            if "#EXT-X-STREAM-INF" not in content and "#EXT-X-MEDIA:TYPE=AUDIO" not in content:
                return current_url, content

            variant_url = self._select_lossless_variant_url(current_url, content)
            if not variant_url or variant_url == current_url:
                return current_url, content

            current_url = variant_url

        final_response = self._api_get(current_url, timeout=15)
        final_response.raise_for_status()
        return current_url, final_response.text

    @staticmethod
    def _select_lossless_variant_url(stream_url: str, content: str) -> Optional[str]:
        lines = content.splitlines()
        variants: list[tuple[int, str]] = []
        audio_group_scores: dict[str, int] = {}

        for index, line in enumerate(lines):
            stripped = line.strip()

            if stripped.startswith("#EXT-X-MEDIA:"):
                if "TYPE=AUDIO" not in stripped:
                    continue
                uri_match = re.search(r'URI="([^"]+)"', stripped)
                if not uri_match:
                    continue
                uri = uri_match.group(1)
                
                lower_info = stripped.lower()
                lower_uri = uri.lower()
                score = 0
                if "alac" in lower_info or "alac" in lower_uri:
                    score += 100
                if "lossless" in lower_info or "lossless" in lower_uri:
                    score += 80
                if "hires" in lower_info or "hi-res" in lower_info or "hires" in lower_uri or "hi-res" in lower_uri:
                    score += 60
                if "mp4a.40.2" in lower_info or "aac" in lower_info or "aac" in lower_uri:
                    score -= 120
                group_match = re.search(r'GROUP-ID="([^"]+)"', stripped)
                if group_match:
                    audio_group_scores[group_match.group(1)] = score
                variants.append((score, urljoin(stream_url, uri)))
                continue

            if not stripped.startswith("#EXT-X-STREAM-INF:"):
                continue

            uri = None
            for next_index in range(index + 1, len(lines)):
                candidate = lines[next_index].strip()
                if not candidate:
                    continue
                if candidate.startswith("#"):
                    break
                uri = candidate
                break
            if not uri:
                continue

            lower_info = stripped.lower()
            lower_uri = uri.lower()
            score = 0

            if "alac" in lower_info or "alac" in lower_uri:
                score += 100
            if "lossless" in lower_info or "lossless" in lower_uri:
                score += 80
            if "hires" in lower_info or "hires" in lower_uri or "hi-res" in lower_info or "hi-res" in lower_uri:
                score += 60
            if "mp4a.40.2" in lower_info or "aac" in lower_info or "aac" in lower_uri:
                score -= 120
            audio_group_match = re.search(r'AUDIO="([^"]+)"', stripped)
            if audio_group_match:
                score += audio_group_scores.get(audio_group_match.group(1), 0)

            bandwidth_match = re.search(r"BANDWIDTH=(\d+)", stripped, re.IGNORECASE)
            if bandwidth_match:
                try:
                    score += int(bandwidth_match.group(1)) // 100000
                except ValueError:
                    pass

            variants.append((score, urljoin(stream_url, uri)))

        if not variants:
            return None
        variants.sort(key=lambda item: item[0], reverse=True)
        if variants[0][0] < 0:
            return None
        return variants[0][1]

    @staticmethod
    def _validate_downloaded_file(final_path: str, expected_codec: str = "aac") -> None:
        audio = MutagenFile(final_path)
        info = getattr(audio, "info", None)
        codec = str(getattr(info, "codec", "") or "").lower()
        bitrate = getattr(info, "bitrate", None)

        if expected_codec == "alac":
            if "alac" not in codec:
                raise RuntimeError(
                    f"[Apple] Downloaded file is not ALAC (codec={codec or 'unknown'}, bitrate={bitrate})"
                )
        elif expected_codec == "aac":
            # Accept mp4a, aac, or enca (enca = still-encrypted container, treated as AAC)
            if codec and "alac" in codec:
                # Somehow got ALAC when expecting AAC — that's fine, keep it
                pass
            elif codec and codec not in ("mp4a.40.2", "mp4a.40.5", "mp4a", "aac", "enca", ""):
                logger.warning(f"[Apple] Unexpected codec={codec!r} (expected aac/mp4a), proceeding anyway")
