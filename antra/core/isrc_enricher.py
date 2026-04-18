import hmac
import hashlib
import base64
import time
import struct
import requests
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from antra.core.models import TrackMetadata

logger = logging.getLogger(__name__)

TOTP_SECRET = "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4DQOJUGQYTAMRRGA2TCMJSHE3TCMBY"

class ISRCEnricher:
    def __init__(self, market="US"):
        self.market = market
        self.token = None

    def _generate_totp(self) -> str:
        key = base64.b32decode(TOTP_SECRET, casefold=True)
        timestamp = int(time.time()) // 30
        msg = struct.pack(">Q", timestamp)
        h = hmac.new(key, msg, hashlib.sha1).digest()
        offset = h[-1] & 0x0F
        code = struct.unpack(">I", h[offset:offset+4])[0] & 0x7FFFFFFF
        return str(code % 1000000).zfill(6)

    def _get_anonymous_token(self) -> str:
        totp = self._generate_totp()
        r = requests.get(
            "https://open.spotify.com/api/token",
            params={
                "reason": "init",
                "productType": "web-player",
                "totp": totp,
                "totpVer": "5",
            },
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "app-platform": "WebPlayer",
                "spotify-app-version": "1.2.31.596.g3c58432e",
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("accessToken")

    def enrich_tracks(self, tracks: list[TrackMetadata], max_workers=2):
        if not self.token:
            self.token = self._get_anonymous_token()

        # Build map
        id_to_track = {t.spotify_id: t for t in tracks if t.spotify_id}
        ids = list(id_to_track.keys())
        if not ids:
            return

        BATCH_SIZE = 50
        batches = [ids[i:i + BATCH_SIZE] for i in range(0, len(ids), BATCH_SIZE)]

        logger.info(f"[ISRCEnricher] Enriching {len(ids)} tracks using {len(batches)} batches...")

        def fetch_batch(batch_ids, attempt=1):
            headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
            try:
                r = requests.get(
                    "https://api.spotify.com/v1/tracks",
                    headers=headers,
                    params={"ids": ",".join(batch_ids), "market": self.market},
                    timeout=10,
                )
                if r.status_code == 401:
                    if attempt < 3:
                        logger.warning("[ISRCEnricher] Token expired (401). Regenerating TOTP token and retrying...")
                        self.token = self._get_anonymous_token()
                        return fetch_batch(batch_ids, attempt + 1)
                    return []
                if r.status_code == 429:
                    if attempt <= 3:
                        wait = 2 ** attempt  # 2s, 4s, 8s
                        logger.warning(f"[ISRCEnricher] Rate limited (429), attempt {attempt}/3. Retrying in {wait}s...")
                        time.sleep(wait)
                        return fetch_batch(batch_ids, attempt + 1)
                    logger.warning("[ISRCEnricher] Rate limited (429) after 3 retries. Skipping batch.")
                    return []
                r.raise_for_status()
                return r.json().get("tracks", [])
            except Exception as e:
                logger.warning(f"[ISRCEnricher] Batch error: {e}")
                return []

        def _apply_results(resp_tracks):
            nonlocal enriched_count
            for t in resp_tracks:
                if not t or not t.get("id"):
                    continue
                track_obj = id_to_track.get(t["id"])
                if not track_obj:
                    continue
                isrc = t.get("external_ids", {}).get("isrc")
                if isrc:
                    track_obj.isrc = isrc
                    enriched_count += 1
                if t.get("album", {}).get("release_date"):
                    rel = t["album"]["release_date"]
                    track_obj.release_date = rel
                    track_obj.release_year = int(rel[:4]) if len(rel) >= 4 else None
                if t.get("track_number"):
                    track_obj.track_number = t["track_number"]
                # Spotify v1 /tracks includes disc_number — fill it in
                # when missing so _stamp_disc_totals() can compute total_discs
                # correctly for multi-disc albums (e.g. anonymous Spotify path
                # or Amazon Music tracks whose spotify_id was resolved).
                if track_obj.disc_number is None and t.get("disc_number"):
                    track_obj.disc_number = t["disc_number"]

        enriched_count = 0

        # For a single batch use direct sequential call to avoid any parallel overhead.
        # For multiple batches use a small pool (max 2) to keep concurrent load minimal.
        if len(batches) == 1:
            _apply_results(fetch_batch(batches[0]))
        else:
            with ThreadPoolExecutor(max_workers=min(max_workers, len(batches))) as executor:
                futures = {executor.submit(fetch_batch, batch): batch for batch in batches}
                for future in as_completed(futures):
                    try:
                        _apply_results(future.result())
                    except Exception as e:
                        logger.warning(f"[ISRCEnricher] Future error: {e}")

        logger.info(f"[ISRCEnricher] ISRC coverage achieved: {enriched_count}/{len(ids)}")
