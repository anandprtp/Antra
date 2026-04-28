import base64
import json
import logging
import os
import random
import re
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from typing import Optional

import requests

from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.sources.base import BaseSourceAdapter, RateLimitedError
from antra.sources.odesli import OdesliEnricher

logger = logging.getLogger(__name__)


class _DirectAmazonClient:
    """
    Direct Amazon Music DMLS/Widevine client — no proxy server needed.
    Ports core logic from API-Mirrors/amazon_api/amazon_server.py.
    Credentials JSON schema: {cookie, authorization, csrf_token, csrf_rnd,
    csrf_ts, customer_id, device_id, session_id, wvd_path}
    """

    _DMLS_URL = "https://music.amazon.com/NA/api/dmls/"
    _INVALID_ESCAPE_RE = re.compile(r'\\([^"\\/bfnrtu]|u(?![0-9a-fA-F]{4}))')

    def __init__(self, creds_json: str):
        self._creds: Optional[dict] = None
        if creds_json.strip():
            try:
                self._creds = json.loads(creds_json)
            except Exception:
                logger.warning("[Amazon-Direct] Invalid credentials JSON — ignoring")

    def is_configured(self) -> bool:
        if not self._creds:
            return False
        # customer_id and device_id are optional — not all Amazon sessions expose them
        # and they are not required for DMLS API calls.
        required = ("cookie", "authorization", "csrf_token", "wvd_path")
        return all(self._creds.get(k) for k in required)

    def _safe_json_loads(self, text: str) -> dict:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            fixed = self._INVALID_ESCAPE_RE.sub(lambda m: "\\\\" + m.group(0), text)
            return json.loads(fixed)

    def _build_headers(self) -> dict:
        c = self._creds
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "Origin": "https://music.amazon.com",
            "Referer": "https://music.amazon.com/",
            "Cookie": c["cookie"],
            "content-type": "application/json",
            "Content-Encoding": "amz-1.0",
            "csrf-token": c["csrf_token"],
            "csrf-rnd": c["csrf_rnd"],
            "csrf-ts": c["csrf_ts"],
            "Authorization": c["authorization"],
        }

    def _get_manifest(self, asin: str) -> dict:
        headers = self._build_headers()
        headers["X-Amz-Target"] = (
            "com.amazon.digitalmusiclocator.DigitalMusicLocatorServiceExternal.getDashManifestsV2"
        )
        c = self._creds
        # customer_id and device_id may be empty if not captured during login.
        # Use safe fallback values — Amazon accepts these for manifest requests.
        customer_id = c.get("customer_id") or ""
        device_id = c.get("device_id") or "antra-web-player"
        payload = {
            "deviceToken": {"deviceTypeId": "A16ZV8BU3SN1N3", "deviceId": device_id},
            "appMetadata": {"https": "true"},
            "clientMetadata": {
                "clientId": "WebCP",
                "clientRequestId": f"{random.randint(10**8, 10**9-1):09x}-3d00-11f1-8fb8-0b4e12d5b57b",
            },
            "contentIdList": [{"identifier": asin, "identifierType": "ASIN"}],
            "musicDashVersionList": ["SIREN_KATANA_NO_CLEAR_LEAD"],
            "contentProtectionList": ["TRACK_PSSH"],
            "tryAsinSubstitution": True,
            "customerInfo": {"marketplaceId": "ATVPDKIKX0DER", "territoryId": "US"},
            "appInfo": {"musicAgent": "Maestro/1.0 WebCP/1.0.10034.0 (7dbf-196c-WebC-2b70-7689c)"},
        }
        # Only include customerId if we have it — omitting it avoids 400 errors
        # when the field is an empty string.
        if customer_id:
            payload["customerId"] = customer_id
        r = requests.post(self._DMLS_URL, headers=headers, json=payload, timeout=20)
        if r.status_code == 401:
            raise RuntimeError(
                "[Amazon-Direct] Authorization expired (401) — re-extract credentials from music.amazon.com"
            )
        if r.status_code != 200:
            raise RuntimeError(f"[Amazon-Direct] Manifest request failed: {r.status_code}")
        return self._safe_json_loads(r.text)

    def _parse_manifest(self, data: dict) -> tuple[str, str]:
        try:
            manifest_xml = data["contentResponseList"][0]["manifest"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"[Amazon-Direct] Unexpected manifest structure: {e}")

        root = ET.fromstring(manifest_xml)
        ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011", "cenc": "urn:mpeg:cenc:2013"}
        best_pssh = best_url = None
        best_score = -1

        for adaptation_set in root.findall(".//mpd:AdaptationSet", ns):
            priority = int(adaptation_set.get("selectionPriority", 0))
            for rep in adaptation_set.findall("mpd:Representation", ns):
                if "flac" not in rep.get("codecs", ""):
                    continue
                bd_prop = rep.find('.//mpd:SupplementalProperty[@schemeIdUri="amz-music:bitDepth"]', ns)
                bit_depth = int(bd_prop.get("value", 16)) if bd_prop is not None else 16
                score = priority * 100 + bit_depth
                if score > best_score:
                    best_score = score
                    pssh_el = adaptation_set.find(".//cenc:pssh", ns)
                    best_pssh = pssh_el.text.strip() if pssh_el is not None else None
                    base_url_el = rep.find("mpd:BaseURL", ns)
                    best_url = base_url_el.text.strip() if base_url_el is not None else None

        if not best_pssh or not best_url:
            raise RuntimeError("[Amazon-Direct] No FLAC stream found in manifest")
        return best_url, best_pssh

    def _get_license_key(self, pssh_b64: str) -> str:
        try:
            from pywidevine.cdm import Cdm
            from pywidevine.device import Device
            from pywidevine.pssh import PSSH
        except ImportError:
            raise RuntimeError(
                "[Amazon-Direct] pywidevine not installed — run: pip install pywidevine==1.8.0 construct==2.8.8"
            )

        c = self._creds
        wvd_path = c.get("wvd_path", "")
        if not wvd_path:
            raise RuntimeError("[Amazon-Direct] No wvd_path in credentials JSON")

        device = Device.load(wvd_path)
        cdm = Cdm.from_device(device)
        session_id = cdm.open()
        try:
            pssh = PSSH(pssh_b64)
            challenge = cdm.get_license_challenge(session_id, pssh)
            b64_challenge = base64.b64encode(challenge).decode()

            bearer = c["authorization"].removeprefix("Bearer ")
            customer_id = c.get("customer_id") or ""
            device_id = c.get("device_id") or "antra-web-player"
            session_id_str = c.get("session_id") or ""
            payload = {
                "DrmType": "WIDEVINE",
                "licenseChallenge": b64_challenge,
                "deviceToken": {"deviceTypeId": "A16ZV8BU3SN1N3", "deviceId": device_id},
                "appInfo": {"musicAgent": "Maestro/1.0 WebCP/1.0.10034.0 (7dbf-196c-WebC-2b70-7689c)"},
                "Authorization": c["authorization"],
            }
            if customer_id:
                payload["customerId"] = customer_id
            headers = self._build_headers()
            headers["X-Amz-Target"] = (
                "com.amazon.digitalmusiclocator.DigitalMusicLocatorServiceExternal.getLicenseForPlaybackV2"
            )
            headers["x-amzn-authentication"] = json.dumps({
                "interface": "ClientAuthenticationInterface.v1_0.ClientTokenElement",
                "accessToken": f"Bearer {bearer}",
            })
            headers["x-amzn-device-model"] = "WEBPLAYER"
            headers["x-amzn-device-family"] = "WebPlayer"
            headers["x-amzn-device-id"] = device_id
            headers["x-amzn-session-id"] = session_id_str
            headers["x-amzn-device-language"] = "en_US"
            headers["x-amzn-application-version"] = "1.0.10034.0"
            headers["x-amzn-os-version"] = "1.0"

            r = requests.post(self._DMLS_URL, json=payload, headers=headers, timeout=20)
            if r.status_code != 200:
                raise RuntimeError(
                    f"[Amazon-Direct] License request failed: {r.status_code} {r.text[:200]}"
                )
            rj = r.json()
            if "license" not in rj:
                raise RuntimeError(f"[Amazon-Direct] No license in response: {list(rj.keys())}")

            license_bytes = base64.b64decode(rj["license"])
            cdm.parse_license(session_id, license_bytes)
            keys = cdm.get_keys(session_id)
        finally:
            cdm.close(session_id)

        content_keys = [k for k in keys if k.type == "CONTENT"]
        if not content_keys:
            raise RuntimeError("[Amazon-Direct] No CONTENT keys in license response")
        return content_keys[0].key.hex()

    def process_track(self, asin: str) -> dict:
        """Returns {"streamUrl": str, "decryptionKey": str}."""
        data = self._get_manifest(asin)
        stream_url, pssh_b64 = self._parse_manifest(data)
        key_hex = self._get_license_key(pssh_b64)
        return {"streamUrl": stream_url, "decryptionKey": key_hex}

# On Windows, prevent subprocess from flashing a console window
_SUBPROCESS_FLAGS = {}
if sys.platform == "win32":
    _SUBPROCESS_FLAGS["creationflags"] = subprocess.CREATE_NO_WINDOW


class AmazonAdapter(BaseSourceAdapter):
    """
    Amazon Music adapter using a community stream proxy pool.
    Requires ffmpeg for decryption.
    """

    name = "amazon"

    def __init__(
        self,
        mirrors: list[str],
        api_key: Optional[str] = None,
        direct_creds_json: str = "",
    ):
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        })
        self._odesli = OdesliEnricher(api_key=api_key)
        self.priority = 2  # Shared free-lossless tier with Apple/HiFi/DAB

        # Mirror management
        self._mirrors = [m.rstrip("/") for m in mirrors if m]
        self._current_mirror: Optional[str] = None
        self._mirror_failures: dict[str, int] = {}

        # Direct auth client (user's own Amazon Music account — preferred over mirrors)
        self._direct = _DirectAmazonClient(direct_creds_json) if direct_creds_json.strip() else None
        if self._direct and self._direct.is_configured():
            logger.info("[Amazon-Direct] Direct credentials loaded — will use DMLS API directly")


    def _get_working_mirror(self, force_rotate: bool = False) -> str:
        """
        Return a working mirror from the pool. 
        If force_rotate is True, it skips the current mirror.
        """
        if self._current_mirror and not force_rotate:
            return self._current_mirror

        # Filter out mirrors that have failed too many times recently
        valid_mirrors = [m for m in self._mirrors if self._mirror_failures.get(m, 0) < 3]
        if not valid_mirrors:
            logger.debug("[Amazon] All mirrors failed health checks. Resetting pool.")
            valid_mirrors = self._mirrors
            self._mirror_failures.clear()

        # Try to find a responsive mirror
        for mirror in valid_mirrors:
            if mirror == self._current_mirror and force_rotate:
                continue
                
            try:
                # Quick health check
                resp = self._session.get(mirror + "/", timeout=5)
                if resp.status_code in (200, 404):
                    self._current_mirror = mirror
                    logger.debug(f"[Amazon] Using mirror: {mirror}")
                    return mirror
            except Exception as e:
                logger.debug(f"[Amazon] Mirror {mirror} unreachable: {e}")
                self._mirror_failures[mirror] = self._mirror_failures.get(mirror, 0) + 1

        if self._mirrors:
            # Fallback to the first mirror if all checks fail
            return self._mirrors[0]
        
        raise RuntimeError("[Amazon] No mirrors configured.")

    def is_available(self) -> bool:
        """Check if ffmpeg is installed and at least one download path is ready."""
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
        """
        Resolve track to Amazon Music ASIN.
        Uses the ASIN already on the track (when sourced from an Amazon Music URL)
        or falls back to Odesli cross-platform lookup.
        """
        amazon_id = getattr(track, "amazon_asin", None)
        if amazon_id:
            logger.debug(f"[Amazon] Using embedded ASIN for '{track.title}': {amazon_id}")
        else:
            logger.debug(f"[Amazon] Resolving ID via Odesli: {track.title}")
            platform_ids = self._odesli.resolve(track)
            amazon_id = platform_ids.get("amazonMusic") or platform_ids.get("amazon")

        if not amazon_id:
            logger.debug(f"[Amazon] No Amazon ID found for '{track.title}'")
            return None

        # Amazon's community proxy serves Ultra HD (24-bit lossless) streams.
        # Explicitly marking bit_depth=24 so the resolver quality-tier comparison
        # correctly ranks this as tier-4 (Hi-Res), equal to Apple/HiFi.
        return SearchResult(
            source="amazon",
            title=track.title,
            artists=track.artists,
            album=track.album,
            duration_ms=track.duration_ms,
            audio_format=AudioFormat.FLAC,
            quality_kbps=None,
            is_lossless=True,
            bit_depth=24,  # Ultra HD — 24-bit lossless via community proxy
            download_url=None,
            stream_id=amazon_id,
            similarity_score=1.0,  # Odesli match is authoritative
            isrc_match=True if track.isrc else False,
        )

    def download(self, result: SearchResult, output_path: str) -> str:
        """
        Download and decrypt a track using its Amazon ASIN.
        Tries direct auth first (if configured), then rotates through mirrors.
        """
        asin = result.stream_id
        if not asin:
            raise ValueError("[Amazon] Missing ASIN in search result")

        # Try direct DMLS API first — avoids proxy latency and rate-limit exposure
        if self._direct and self._direct.is_configured():
            try:
                logger.info(f"[Amazon-Direct] Fetching stream via DMLS API for ASIN {asin}...")
                data = self._direct.process_track(asin)
                return self._process_download(data["streamUrl"], data.get("decryptionKey"), output_path)
            except Exception as e:
                err = str(e)
                logger.warning(f"[Amazon-Direct] Direct auth failed: {err}")
                if "401" in err or "expired" in err.lower():
                    raise RuntimeError(
                        f"[Amazon-Direct] Credentials expired — re-extract from music.amazon.com: {err}"
                    )
                if not self._mirrors:
                    raise
                logger.info("[Amazon-Direct] Falling back to mirror pool...")

        if not self._mirrors:
            raise RuntimeError("[Amazon] No direct credentials and no mirrors configured")

        max_attempts = len(self._mirrors)
        last_error = None
        saw_rate_limit = False

        for attempt in range(max_attempts):
            mirror = self._get_working_mirror(force_rotate=(attempt > 0))
            api_url = f"{mirror}/api/track/{asin}"

            try:
                logger.debug(f"[Amazon] Fetching stream info (attempt {attempt+1}/{max_attempts}) from {mirror}...")
                resp = self._session.get(api_url, timeout=20)

                if resp.status_code == 200:
                    data = resp.json()
                    download_url = data.get("streamUrl")
                    decryption_key = data.get("decryptionKey")

                    if not download_url:
                        raise RuntimeError("No stream URL returned")

                    return self._process_download(download_url, decryption_key, output_path)

                if resp.status_code == 429:
                    saw_rate_limit = True
                    raise RateLimitedError(f"[Amazon] Rate limited (429) on {mirror}")

                # 403/503 means mirror is blocking/refreshing — permanently remove it
                if resp.status_code in (403, 503):
                    logger.debug(f"[Amazon] Mirror returned {resp.status_code} — removing from pool for session")
                    self._mirror_failures[mirror] = 99
                    self._current_mirror = None
                    last_error = f"API error {resp.status_code}"
                    continue

                logger.debug(f"[Amazon] Mirror {mirror} returned {resp.status_code}")
                last_error = f"API error {resp.status_code}"

            except RateLimitedError as e:
                logger.debug(f"[Amazon] Mirror {mirror} rate limited: {e}")
                last_error = str(e)
                self._mirror_failures[mirror] = self._mirror_failures.get(mirror, 0) + 1
                continue
            except Exception as e:
                logger.debug(f"[Amazon] Mirror {mirror} failed: {e}")
                last_error = str(e)

            # Non-429 failure — mark mirror and rotate.
            self._mirror_failures[mirror] = self._mirror_failures.get(mirror, 0) + 1

        if saw_rate_limit:
            raise RateLimitedError(last_error or "[Amazon] Rate limited")
        raise RuntimeError(f"[Amazon] All mirrors failed. Last error: {last_error}")

    def should_retry_download(self, result: SearchResult, error: Exception) -> bool:
        # Never retry after a 429 — fall through to the next source immediately.
        if isinstance(error, RateLimitedError):
            return False
        err = str(error)
        # 404 means the ASIN doesn't exist on any mirror — retrying won't help.
        if "404" in err:
            return False
        return True

    def _process_download(self, download_url: str, decryption_key: Optional[str], output_path: str) -> str:
        # Download encrypted file
        temp_enc_path = output_path + ".enc.m4a"
        logger.debug(f"[Amazon] Downloading encrypted stream: {temp_enc_path}")
        
        with self._session.get(download_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(temp_enc_path, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)

        # Probing the encrypted file to determine the codec (FLAC vs AAC)
        try:
            from antra.utils.runtime import get_ffprobe_exe
            ffprobe = get_ffprobe_exe() or "ffprobe"
            cmd = [
                ffprobe, "-v", "quiet",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                temp_enc_path
            ]
            codec = subprocess.check_output(cmd, **_SUBPROCESS_FLAGS).decode().strip()
            logger.debug(f"[Amazon] Probed codec: {codec}")
            dec_ext = ".flac" if codec == "flac" else ".m4a"
        except Exception as e:
            logger.debug(f"[Amazon] Probing failed: {e}")
            codec = "unknown"
            dec_ext = ".m4a"

        # Decrypt
        final_path = output_path + dec_ext
        if not decryption_key:
            logger.warning(f"[Amazon] No decryption key provided — assuming track is unencrypted.")
            os.rename(temp_enc_path, final_path)
        else:
            logger.debug(f"[Amazon] Decrypting {codec.upper()} stream using session key...")
            ffmpeg_err = self._decrypt_file(temp_enc_path, final_path, decryption_key)
            if ffmpeg_err is not None:
                logger.warning(f"[Amazon] ffmpeg decryption failed: {ffmpeg_err} — trying Python fallback")
                py_err = self._decrypt_cenc_python(temp_enc_path, final_path, decryption_key)
                if py_err is not None:
                    if os.path.exists(temp_enc_path):
                        os.remove(temp_enc_path)
                    raise RuntimeError(
                        f"[Amazon] Decryption failed. ffmpeg: {ffmpeg_err[:200]} | python: {py_err}"
                    )
                logger.debug("[Amazon] Python CENC fallback succeeded.")
            os.remove(temp_enc_path)

        # Post-process: Standardize extension and remux if needed
        return self._finalize_audio(final_path)

    def _decrypt_file(self, input_path: str, output_path: str, key: str) -> Optional[str]:
        """
        Decrypt via ffmpeg.
        Returns None on success, or an error string describing the failure.
        """
        try:
            from antra.utils.runtime import get_ffmpeg_exe
            ffmpeg = get_ffmpeg_exe() or "ffmpeg"
            result = subprocess.run(
                [ffmpeg, "-y", "-decryption_key", key.strip(),
                 "-i", input_path, "-c", "copy", output_path],
                capture_output=True,
                timeout=180,
                **_SUBPROCESS_FLAGS,
            )
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="ignore").strip()[-400:]
                return f"ffmpeg exit {result.returncode}: {stderr}"
            return None
        except Exception as e:
            return f"ffmpeg error: {e}"

    def _decrypt_cenc_python(self, input_path: str, output_path: str, key_hex: str) -> Optional[str]:
        """
        Pure-Python AES-CTR CENC decryption using Cryptodome.
        Fallback for when ffmpeg's -decryption_key fails on the user's system.
        Handles fragmented MP4 (CMAF) format used by Amazon Music.
        Returns None on success, or an error string on failure.
        """
        try:
            from Cryptodome.Cipher import AES
        except ImportError:
            return "Cryptodome not available for Python CENC fallback"

        try:
            key = bytes.fromhex(key_hex.strip())
        except Exception as e:
            return f"Invalid key hex: {e}"
        if len(key) not in (16, 24, 32):
            return f"Key must be 16/24/32 bytes, got {len(key)}"

        # ── ISOBMFF helpers ───────────────────────────────────────────────────
        def read_box(d, pos):
            if pos + 8 > len(d):
                return None
            sz = struct.unpack_from(">I", d, pos)[0]
            bt = d[pos+4:pos+8].decode("latin-1", errors="replace")
            if sz == 1:
                if pos + 16 > len(d):
                    return None
                sz = struct.unpack_from(">Q", d, pos+8)[0]
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
                    return d[pos+hs:pos+sz]
                pos += sz
            return None

        def parse_senc(d):
            """Return (iv_size, [(iv_bytes, subsamples_or_None), ...])."""
            if len(d) < 8:
                return 8, []
            flags = struct.unpack_from(">I", d, 0)[0] & 0xFFFFFF
            iv_size = 8  # AES-CTR default; override if flag bit 0 set
            off = 4
            if flags & 1:
                # AlgorithmID (3 bytes) + IV_Size (1 byte) + KID (16 bytes)
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
                iv = bytes(d[off:off+iv_size])
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
                                     struct.unpack_from(">I", d, off+2)[0]))
                        off += 6
                result.append((iv, subs))
            return iv_size, result

        def parse_trun(d):
            """Return (data_offset_or_None, [sample_sizes])."""
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
                if flags & 0x100: off += 4
                if flags & 0x200:
                    sz = struct.unpack_from(">I", d, off)[0]
                    off += 4
                if flags & 0x400: off += 4
                if flags & 0x800: off += 4
                sizes.append(sz)
            return doff, sizes

        # ── Load file ─────────────────────────────────────────────────────────
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
            traf = find_first(raw[pos+moof_hs:moof_end], "traf")
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

            # mdat immediately follows moof
            mr = read_box(raw, moof_end)
            if mr is None or mr[1] != "mdat":
                pos = moof_end
                continue
            mdat_sz, _, mdat_hs = mr

            # data_offset in trun is relative to the start of moof
            sample_pos = (moof_start + doff) if doff is not None else (moof_end + mdat_hs)

            for idx, (iv, subs) in enumerate(samples):
                s_sz = sizes[idx] if idx < len(sizes) else 0
                if s_sz == 0:
                    sample_pos += s_sz
                    continue
                # Pad IV to 16 bytes (AES-CTR counter initial value)
                iv16 = iv.ljust(16, b"\x00")
                cipher = AES.new(key, AES.MODE_CTR, initial_value=iv16, nonce=b"")
                if subs:
                    cur = sample_pos
                    for clear, enc in subs:
                        cur += clear
                        if enc > 0:
                            raw[cur:cur+enc] = cipher.decrypt(bytes(raw[cur:cur+enc]))
                        cur += enc
                else:
                    raw[sample_pos:sample_pos+s_sz] = cipher.decrypt(
                        bytes(raw[sample_pos:sample_pos+s_sz])
                    )
                sample_pos += s_sz
                changed += 1

            pos = moof_end + mdat_sz

        if not changed:
            return "No CENC samples found — file may not be fragmented MP4 or is not CENC-encrypted"

        try:
            with open(output_path, "wb") as f:
                f.write(raw)
        except Exception as e:
            return f"Cannot write decrypted file: {e}"

        return None

    def _finalize_audio(self, path: str) -> str:
        """
        Remux to .flac if lossless FLAC is wrapped in M4A container.
        Tries ffprobe for codec detection; when ffprobe is unavailable, performs
        a blind remux attempt — Amazon Ultra HD is always FLAC-in-M4A so this
        is safe and eliminates the extra engine transcoder pass.
        """
        if not path.lower().endswith(".m4a"):
            return path

        # Try ffprobe to confirm codec
        ffprobe_ran = False
        codec_is_flac = False
        try:
            from antra.utils.runtime import get_ffprobe_exe
            ffprobe = get_ffprobe_exe()
            if ffprobe:
                cmd = [
                    ffprobe, "-v", "quiet",
                    "-select_streams", "a:0",
                    "-show_entries", "stream=codec_name",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    path
                ]
                codec_is_flac = subprocess.check_output(
                    cmd, **_SUBPROCESS_FLAGS
                ).decode().strip() == "flac"
                ffprobe_ran = True
        except Exception:
            pass

        # Remux when ffprobe says FLAC, or when ffprobe wasn't available
        # (Amazon HD streams are always FLAC-in-M4A — blind attempt is safe).
        if codec_is_flac or not ffprobe_ran:
            flac_path = path.rsplit(".", 1)[0] + ".flac"
            logger.debug(f"[Amazon] Remuxing M4A → FLAC container...")
            if self._remux_to_flac(path, flac_path):
                os.remove(path)
                return flac_path

        return path

    def _remux_to_flac(self, input_path: str, output_path: str) -> bool:
        """Bit-perfect container swap."""
        try:
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
        except Exception:
            return False


def _diagnose():
    """Run with: python -m antra.sources.amazon"""
    logging.basicConfig(level=logging.DEBUG)
    mirrors = ["https://amazon.spotbye.qzz.io"]
    adapter = AmazonAdapter(mirrors=mirrors)
    if not adapter.is_available():
        print("Amazon adapter not available (check ffmpeg and internet).")
        return

    from antra.core.models import TrackMetadata
    track = TrackMetadata(
        title="Bad Guy",
        artists=["Billie Eilish"],
        spotify_id="2JpMcmBYYvX3C6p8p8p8"
    )
    res = adapter.search(track)
    if res:
        print(f"Found: {res.title} (ASIN: {res.stream_id})")
    else:
        print("Track not found on Amazon.")

if __name__ == "__main__":
    _diagnose()
