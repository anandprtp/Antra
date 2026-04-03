import logging
import os
import re
import subprocess
import sys
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
            # Check ffmpeg
            subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True, **_SUBPROCESS_FLAGS)
            
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
                
                logger.debug(f"[Amazon] Mirror {mirror} returned {resp.status_code}")
                last_error = f"API error {resp.status_code}"
                
            except Exception as e:
                logger.debug(f"[Amazon] Mirror {mirror} failed: {e}")
                last_error = str(e)
            
            # If we reach here, this mirror failed. Mark it and try next.
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
            cmd = [
                "ffprobe", "-v", "quiet",
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

        # Decrypt using ffmpeg
        final_path = output_path + dec_ext
        if not decryption_key:
            logger.warning(f"[Amazon] No decryption key provided — assuming track is unencrypted.")
            os.rename(temp_enc_path, final_path)
        else:
            logger.debug(f"[Amazon] Decrypting {codec.upper()} bit-stream using session key...")
            if not self._decrypt_file(temp_enc_path, final_path, decryption_key):
                if os.path.exists(temp_enc_path):
                    os.remove(temp_enc_path)
                raise RuntimeError("[Amazon] Decryption failed via ffmpeg.")
            os.remove(temp_enc_path)

        # Post-process: Standardize extension and remux if needed
        return self._finalize_audio(final_path)

    def _decrypt_file(self, input_path: str, output_path: str, key: str) -> bool:
        """Decrypt via ffmpeg."""
        try:
            cmd = [
                "ffmpeg", "-y",
                "-decryption_key", key.strip(),
                "-i", input_path,
                "-c", "copy",
                output_path,
            ]
            logger.debug(f"[Amazon] Executing: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=180,
                **_SUBPROCESS_FLAGS,
            )
            if result.returncode != 0:
                logger.debug(f"[Amazon] ffmpeg stderr: {result.stderr.decode('utf-8', errors='ignore')}")
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"[Amazon] ffmpeg decryption error: {e}")
            return False

    def _finalize_audio(self, path: str) -> str:
        """
        Analyze codec and remux to .flac if lossless FLAC is wrapped in M4A.
        """
        try:
             # Check codec via ffprobe
            cmd = [
                "ffprobe", "-v", "quiet",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path
            ]
            codec_output = subprocess.check_output(cmd, **_SUBPROCESS_FLAGS).decode().strip()
            
            if codec_output == "flac":
                flac_path = path.rsplit(".", 1)[0] + ".flac"
                logger.debug(f"[Amazon] Detected FLAC codec in M4A. Remuxing to standard container...")
                if self._remux_to_flac(path, flac_path):
                    os.remove(path)
                    return flac_path
            
            return path
        except Exception as e:
            logger.debug(f"[Amazon] Finalization failed (keeping original): {e}")
            return path

    def _remux_to_flac(self, input_path: str, output_path: str) -> bool:
        """Bit-perfect container swap."""
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y",
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
