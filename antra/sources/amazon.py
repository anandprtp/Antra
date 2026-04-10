import logging
import os
import re
import struct
import subprocess
import sys
import time
from typing import Optional

import requests

from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.sources.base import BaseSourceAdapter
from antra.sources.odesli import OdesliEnricher

logger = logging.getLogger(__name__)

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

    def __init__(self, mirrors: list[str], api_key: Optional[str] = None):
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        })
        self._odesli = OdesliEnricher(api_key=api_key)
        self.priority = 1 # Highest priority
        
        # Mirror management
        self._mirrors = [m.rstrip("/") for m in mirrors if m]
        self._current_mirror: Optional[str] = None
        self._mirror_failures: dict[str, int] = {}

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
        """Check if any community API is reachable and ffmpeg is installed."""
        try:
            from antra.utils.runtime import get_ffmpeg_exe
            ffmpeg = get_ffmpeg_exe() or "ffmpeg"
            subprocess.run([ffmpeg, "-version"], capture_output=True, check=True, **_SUBPROCESS_FLAGS)
            
            # Check if at least one mirror is reachable
            self._get_working_mirror()
            return True
        except Exception:
            return False

    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        """
        Resolve Spotify track to Amazon Music ASIN via Odesli.
        """
        logger.debug(f"[Amazon] Resolving ID via Odesli: {track.title}")
        platform_ids = self._odesli.resolve(track)
        amazon_id = platform_ids.get("amazonMusic") or platform_ids.get("amazon")
        
        if not amazon_id:
            logger.debug(f"[Amazon] No Amazon ID found for '{track.title}'")
            return None

        # Amazon's community proxy serves Ultra HD (24-bit lossless) streams.
        # Explicitly marking bit_depth=24 so the resolver quality-tier comparison
        # correctly ranks this as tier-4 (Hi-Res), equal to HiFi's best quality.
        # Amazon has priority=1 vs HiFi's priority=2, so it wins the tiebreaker
        # and is preferred when both sources offer the same tier.
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
        Rotates through mirrors on failure.
        """
        asin = result.stream_id
        if not asin:
            raise ValueError("[Amazon] Missing ASIN in search result")

        max_attempts = len(self._mirrors)
        last_error = None

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
                    # Rate-limited — mirror is healthy, just needs a moment.
                    # Don't count this as a mirror failure.
                    logger.debug(f"[Amazon] Mirror {mirror} rate-limited (429), backing off...")
                    last_error = "rate limited (429)"
                    time.sleep(1.0)
                    continue

                logger.debug(f"[Amazon] Mirror {mirror} returned {resp.status_code}")
                last_error = f"API error {resp.status_code}"

            except Exception as e:
                logger.debug(f"[Amazon] Mirror {mirror} failed: {e}")
                last_error = str(e)

            # Non-429 failure — mark mirror and rotate.
            self._mirror_failures[mirror] = self._mirror_failures.get(mirror, 0) + 1

        raise RuntimeError(f"[Amazon] All mirrors failed. Last error: {last_error}")

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
    mirrors = ["https://amzn.afkarxyz.qzz.io"]
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
