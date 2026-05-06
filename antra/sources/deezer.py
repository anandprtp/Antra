"""
Deezer source adapter — lossless FLAC via ARL token + Blowfish decryption.

How it works:
1. Authenticates using an ARL cookie (no username/password needed).
2. Searches Deezer's public API for tracks by ISRC or text.
3. Fetches the stream metadata from the private GW-Light API (used by
   the official Deezer web player).
4. Downloads the encrypted MPEG-DASH or MP3 stream and decrypts it locally
   using the Blowfish ECB algorithm with a per-track key derived from the
   track's MD5 hash and the well-known Blowfish secret.
5. Saves the result as .flac (FLAC 1411kbps) or .mp3 (MP3 320kbps).

Requirements:
    pip install pycryptodomex requests

ARL tokens:
    A Deezer ARL is a long-lived session cookie (~1 year) that grants API access.
    Set DEEZER_ARL_TOKEN in your .env file.
"""

import hashlib
import logging
import os
import struct
from typing import Optional

import requests

from antra.core.models import TrackMetadata, SearchResult, AudioFormat
from antra.sources.base import BaseSourceAdapter
from antra.utils.matching import score_similarity, duration_close

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GW_API_URL = "https://www.deezer.com/ajax/gw-light.php"
_PUBLIC_API  = "https://api.deezer.com"
_DEFAULT_BF_SECRET = "g4el58wc0zvf9na1"
_CHUNK_SIZE = 2048          # Blowfish decrypts in 2048-byte blocks
_DOWNLOAD_CHUNK = 65_536

MIN_SIMILARITY = 0.80


# ---------------------------------------------------------------------------
# Crypto helpers (pure Python — no native deps beyond pycryptodomex)
# ---------------------------------------------------------------------------

def _generate_blowfish_key(track_id: str, bf_secret: str) -> bytes:
    """Derive per-track Blowfish key using Deezer's algorithm."""
    md5 = hashlib.md5(track_id.encode()).hexdigest()
    key = "".join(
        chr(ord(md5[i]) ^ ord(md5[i + 16]) ^ ord(bf_secret[i]))
        for i in range(16)
    )
    return key.encode()


def _decrypt_chunk(data: bytes, key: bytes) -> bytes:
    """Decrypt a single 2048-byte Blowfish-CBC (ECB mode) chunk."""
    from Cryptodome.Cipher import Blowfish
    cipher = Blowfish.new(key, Blowfish.MODE_CBC, b"\x00\x01\x02\x03\x04\x05\x06\x07")
    return cipher.decrypt(data)


def _decrypt_stream(encrypted: bytes, track_id: str, bf_secret: str) -> bytes:
    """
    Deezer encrypts every 3rd 2048-byte block with Blowfish.
    Blocks 0, 3, 6, 9, … are encrypted; blocks 1, 2, 4, 5, … are plain.
    """
    key = _generate_blowfish_key(track_id, bf_secret)
    out = bytearray()
    i = 0
    block_num = 0

    while i < len(encrypted):
        chunk = encrypted[i : i + _CHUNK_SIZE]
        if block_num % 3 == 0 and len(chunk) == _CHUNK_SIZE:
            chunk = _decrypt_chunk(chunk, key)
        out.extend(chunk)
        i += _CHUNK_SIZE
        block_num += 1

    return bytes(out)


# ---------------------------------------------------------------------------
# GW-Light API helpers
# ---------------------------------------------------------------------------

def _build_session(arl: str) -> requests.Session:
    s = requests.Session()
    s.cookies.set("arl", arl, domain=".deezer.com")
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def _gw_api_call(session: requests.Session, method: str, params: dict) -> dict:
    """Make a call to Deezer's internal GW-Light API."""
    resp = session.post(
        _GW_API_URL,
        params={
            "method": method,
            "input": "3",
            "api_version": "1.0",
            "api_token": "null",
        },
        json=params,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data and data["error"]:
        raise RuntimeError(f"GW-API error for {method}: {data['error']}")
    return data.get("results", data)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class DeezerAdapter(BaseSourceAdapter):
    """
    Downloads lossless FLAC (or 320kbps MP3 fallback) from Deezer.

    Priority 25 — sits below HiFi (5) and Tidal (20), above Qobuz proxy,
    Amazon proxy, and JioSaavn.
    """

    name = "deezer"
    priority = 25

    def __init__(self, arl_token: str, bf_secret: str = _DEFAULT_BF_SECRET):
        self.arl_token = arl_token.strip()
        self.bf_secret = bf_secret or _DEFAULT_BF_SECRET
        self._session: Optional[requests.Session] = None
        self._user_token: Optional[str] = None
        self._logged_in = False

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        try:
            from Cryptodome.Cipher import Blowfish  # noqa
        except ImportError:
            logger.warning("[Deezer] pycryptodomex not installed — cannot decrypt streams. Run: pip install pycryptodomex")
            return False
        return bool(self.arl_token)

    # ------------------------------------------------------------------
    # Session / Login
    # ------------------------------------------------------------------

    def _get_session(self) -> requests.Session:
        if self._session and self._logged_in:
            return self._session

        self._session = _build_session(self.arl_token)
        # Fetch the user token (needed for some GW-Light calls)
        try:
            data = _gw_api_call(self._session, "deezer.getUserData", {})
            self._user_token = data.get("checkForm", "")
            user = data.get("USER", {})
            if not user.get("USER_ID") or user.get("USER_ID") == 0:
                raise RuntimeError("ARL token rejected — user not authenticated.")
            self._logged_in = True
            logger.info(f"[Deezer] Authenticated as user {user.get('BLOG_NAME', user.get('USER_ID'))}")
        except Exception as e:
            raise RuntimeError(f"[Deezer] Login failed: {e}")

        return self._session

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        try:
            self._get_session()
        except Exception as e:
            logger.warning(str(e))
            return None

        result = self._search_by_isrc(track)
        if result:
            return result
        return self._search_by_text(track)

    def _search_by_isrc(self, track: TrackMetadata) -> Optional[SearchResult]:
        if not track.isrc:
            return None
        try:
            resp = self._session.get(
                f"{_PUBLIC_API}/track/isrc:{track.isrc}",
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if "id" in data and "error" not in data:
                    result = self._build_result(data, track, isrc_match=True)
                    # Sanity-check duration to catch Pt. 1 / Pt. 2 collisions
                    dur_ms = int(data.get("duration", 0)) * 1000
                    if track.duration_ms and dur_ms:
                        from antra.utils.matching import duration_close
                        if not duration_close(
                            track.duration_ms / 1000,
                            dur_ms / 1000,
                            tolerance=30,
                        ):
                            logger.info(
                                "[Deezer] ISRC match for '%s' rejected — "
                                "duration mismatch (expected %.0fs, got %.0fs)",
                                track.title,
                                track.duration_ms / 1000,
                                dur_ms / 1000,
                            )
                            return None
                    return result
        except Exception as e:
            logger.debug(f"[Deezer] ISRC lookup failed: {e}")
        return None

    def _search_by_text(self, track: TrackMetadata) -> Optional[SearchResult]:
        query = f"{track.title} {track.primary_artist}"
        try:
            resp = self._session.get(
                f"{_PUBLIC_API}/search",
                params={"q": query, "limit": 10, "output": "json"},
                timeout=10,
            )
            resp.raise_for_status()
            items = resp.json().get("data", [])
        except Exception as e:
            logger.warning(f"[Deezer] Text search failed for '{query}': {e}")
            return None

        best = None
        best_score = 0.0

        for item in items:
            artist_name = item.get("artist", {}).get("name", "")
            score = score_similarity(
                query_title=track.title,
                query_artists=track.artists,
                result_title=item.get("title", ""),
                result_artist=artist_name,
            )
            dur = item.get("duration")
            if dur and track.duration_seconds:
                if not duration_close(track.duration_seconds, dur, tolerance=5):
                    score *= 0.8

            if score > best_score:
                best_score = score
                best = self._build_result(item, track, isrc_match=False, override_score=score)

        if best and best_score >= MIN_SIMILARITY:
            logger.debug(f"[Deezer] Match score={best_score:.2f}: {best.title}")
            return best
        return None

    def _build_result(
        self,
        data: dict,
        track: TrackMetadata,
        isrc_match: bool,
        override_score: float = 1.0,
    ) -> SearchResult:
        artist_name = data.get("artist", {}).get("name", "") if isinstance(data.get("artist"), dict) else ""
        album_title = data.get("album", {}).get("title", "") if isinstance(data.get("album"), dict) else ""
        dur_ms = int(data.get("duration", 0)) * 1000

        score = 1.0 if isrc_match else override_score

        return SearchResult(
            source=self.name,
            title=data.get("title", ""),
            artists=[artist_name] if artist_name else [],
            album=album_title or None,
            duration_ms=dur_ms or None,
            audio_format=AudioFormat.FLAC,
            quality_kbps=1411,
            is_lossless=True,
            download_url=None,
            stream_id=str(data["id"]),
            similarity_score=score,
            isrc_match=isrc_match,
        )

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def download(self, result: SearchResult, output_path: str) -> str:
        session = self._get_session()
        track_id = result.stream_id

        # Step 1: Get track page-data from GW-API
        track_data = _gw_api_call(session, "song.getData", {"SNG_ID": track_id})

        # Step 2: Build the download URL
        md5_origin = track_data.get("MD5_ORIGIN", "")
        media_version = track_data.get("MEDIA_VERSION", "1")

        # Try FLAC (format 9) first, fall back to MP3 320 (format 3)
        for fmt_id, ext, kbps in [(9, "flac", 1411), (3, "mp3", 320)]:
            try:
                url = self._build_cdn_url(track_id, md5_origin, fmt_id, media_version)
                encrypted_data = self._download_encrypted(session, url)
                decrypted_data = _decrypt_stream(encrypted_data, track_id, self.bf_secret)

                final_path = output_path + f".{ext}"
                os.makedirs(os.path.dirname(os.path.abspath(final_path)), exist_ok=True)
                with open(final_path, "wb") as f:
                    f.write(decrypted_data)

                logger.info(f"[Deezer] Downloaded {ext.upper()} ({kbps}kbps): {os.path.basename(final_path)}")
                return final_path

            except Exception as e:
                logger.warning(f"[Deezer] Format {fmt_id} ({ext}) failed: {e}")
                continue

        raise RuntimeError(f"[Deezer] All formats failed for track {track_id}")

    def _build_cdn_url(
        self,
        track_id: str,
        md5_origin: str,
        fmt_id: int,
        media_version: str,
    ) -> str:
        """
        Construct Deezer's CDN URL using the same hash algorithm as the web player.
        This is the 'new' URL scheme (cdn-proxy.dzcdn.net).
        """
        step1 = "\xa4".join([md5_origin, str(fmt_id), track_id, media_version])
        step2 = hashlib.md5(step1.encode()).hexdigest() + "\xa4" + step1 + "\xa4"
        # Pad to multiple of 16
        while len(step2) % 16:
            step2 += " "
        step3_bytes = step2.encode()

        from Cryptodome.Cipher import AES
        cipher = AES.new(b"jo6aey6haid2Teih", AES.MODE_ECB)
        encrypted = cipher.encrypt(step3_bytes)
        url_hash = encrypted.hex()

        return f"https://e-cdns-proxy-{md5_origin[0]}.dzcdn.net/mobile/1/{url_hash}"

    def _download_encrypted(self, session: requests.Session, url: str) -> bytes:
        """Stream-download the encrypted audio data."""
        with session.get(url, stream=True, timeout=30) as resp:
            resp.raise_for_status()
            chunks = []
            for chunk in resp.iter_content(_DOWNLOAD_CHUNK):
                if chunk:
                    chunks.append(chunk)
        return b"".join(chunks)
