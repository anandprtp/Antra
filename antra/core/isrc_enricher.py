"""
Spotify track ID -> ISRC enrichment.

Uses spclient's per-track metadata route
(``spclient.wg.spotify.com/metadata/4/track/{gid}``), NOT the public Web API
``api.spotify.com/v1/tracks``. With the web player's client id (which every TOTP
or sp_dc token carries) /v1/tracks answers 429 almost unconditionally — measured
again on 2026-09-16 (429, Retry-After 57s) — and that is why this enricher was
unwired from fetch_playlist_tracks in v1.1.3 (BUG-14: 14s of retries, 0 ISRCs).
The spclient route answers 200 for the same token and the same tracks, and it
also carries the track/disc number and the album date, so nothing /v1/tracks
supplied is lost.

It is one request per track instead of 50, so lookups run in parallel and stop
early the moment spclient itself starts throttling.
"""
import base64
import hashlib
import hmac
import logging
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

from antra.core.models import TrackMetadata

logger = logging.getLogger(__name__)

# Legacy fallback only. The maintained secret/version live in antra/core/spotify.py
# (_SP_TOTP_SECRET / _SP_TOTP_VERSION) and are tried first, so a future rotation
# is updated in exactly one place.
TOTP_SECRET = "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4DQOJUGQYTAMRRGA2TCMJSHE3TCMBY"
_LEGACY_TOTP_VERSION = "5"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
_GID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

# One request per track: bound a single call so a 5000-track playlist cannot
# stall the start of a download for minutes. Anything beyond this is simply
# matched by title/artist, as it was before; the cache fills it on later runs.
MAX_LOOKUPS_PER_CALL = 1000
# Measured 2026-09-16 from a residential connection, cold, no cache:
#   8 workers, new TLS connection per request ... 15 req/s  (p50 ~500ms)
#  16 workers, new TLS connection per request ... 17 req/s  (p50 ~900ms)
#   8 workers, pooled keep-alive connections .... 47 req/s  (p50 ~120ms)
#  16 workers, pooled keep-alive connections .... ~100 req/s, 1,310 lookups
#     back to back with zero 429s and the token endpoint still 200 afterwards.
# The handshake was the bottleneck, not the worker count, which is why the pool
# matters more than the number. THROTTLE_TRIP still stops a run the moment
# spclient pushes back, so a network that tolerates less degrades to "some
# tracks matched by title", never to a stuck download.
DEFAULT_WORKERS = 16
# Consecutive 429s from spclient before the remaining lookups are abandoned.
THROTTLE_TRIP = 3


def spotify_id_to_gid(spotify_id: str) -> Optional[str]:
    """Convert a 22-char base62 Spotify id to the 32-char hex GID spclient uses."""
    if not spotify_id or len(spotify_id) != 22:
        return None
    n = 0
    for char in spotify_id:
        idx = _GID_ALPHABET.find(char)
        if idx < 0:
            return None
        n = n * 62 + idx
    return format(n, "032x")


def _date_from_spclient(date: object) -> Optional[str]:
    """spclient gives {'year': 2022, 'month': 10, 'day': 28}; any part may be absent."""
    if not isinstance(date, dict):
        return None
    year = date.get("year")
    if not isinstance(year, int) or year <= 0:
        return None
    month, day = date.get("month"), date.get("day")
    if isinstance(month, int) and 1 <= month <= 12:
        if isinstance(day, int) and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
        return f"{year:04d}-{month:02d}"
    return f"{year:04d}"


class ISRCEnricher:
    def __init__(self, market="US"):
        self.market = market
        self.token = None
        self._token_lock = threading.Lock()
        self._session: Optional[requests.Session] = None

    def _open_session(self, workers: int) -> None:
        # One keep-alive pool shared by every worker: the per-request TLS handshake
        # was ~75% of each lookup's latency. Sized to the worker count so threads
        # never queue for a connection.
        sess = requests.Session()
        sess.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=max(1, workers)))
        self._session = sess

    def _close_session(self) -> None:
        sess, self._session = self._session, None
        if sess is not None:
            try:
                sess.close()
            except Exception:
                pass

    # ── token ────────────────────────────────────────────────────────────────
    def _generate_totp(self) -> str:
        key = base64.b32decode(TOTP_SECRET, casefold=True)
        timestamp = int(time.time()) // 30
        msg = struct.pack(">Q", timestamp)
        h = hmac.new(key, msg, hashlib.sha1).digest()
        offset = h[-1] & 0x0F
        code = struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF
        return str(code % 1000000).zfill(6)

    def _get_anonymous_token(self) -> str:
        attempts = []
        try:
            import pyotp
            from antra.core import spotify as _sp
            code = pyotp.TOTP(_sp._SP_TOTP_SECRET).now()
            attempts.append((code, str(_sp._SP_TOTP_VERSION)))
        except Exception:
            pass
        attempts.append((self._generate_totp(), _LEGACY_TOTP_VERSION))

        last_error: Optional[Exception] = None
        for code, version in attempts:
            try:
                r = requests.get(
                    "https://open.spotify.com/api/token",
                    params={
                        "reason": "init",
                        "productType": "web-player",
                        "totp": code,
                        "totpVer": version,
                        "totpServer": code,
                    },
                    headers={
                        "User-Agent": _UA,
                        "Accept": "application/json",
                        "app-platform": "WebPlayer",
                    },
                    timeout=10,
                )
                r.raise_for_status()
                token = r.json().get("accessToken")
                if token:
                    return token
            except Exception as e:
                last_error = e
        raise RuntimeError(f"could not obtain a Spotify token: {last_error}")

    def _refresh_token(self, stale: Optional[str]) -> Optional[str]:
        """Refresh once for all workers: only the first thread to see a 401 fetches."""
        with self._token_lock:
            if self.token and self.token != stale:
                return self.token
            try:
                self.token = self._get_anonymous_token()
            except Exception as e:
                logger.debug(f"[ISRCEnricher] token refresh failed: {e}")
            return self.token

    # ── lookup ───────────────────────────────────────────────────────────────
    def _fetch_track_meta_via_gid(self, spotify_id: str) -> tuple[str, Optional[dict]]:
        """Return (status, meta). status is 'ok', 'missing', 'throttled' or 'error'.

        'missing' is a definitive answer (spclient returned the track, or 404) and
        is safe to cache as a negative; 'throttled' and 'error' are weather and
        must never be cached.
        """
        gid = spotify_id_to_gid(spotify_id)
        if not gid:
            return "error", None
        url = f"https://spclient.wg.spotify.com/metadata/4/track/{gid}?market=from_token"
        for attempt in range(2):
            token = self.token
            try:
                r = (self._session or requests).get(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                        "User-Agent": _UA,
                    },
                    timeout=8,
                )
            except Exception as e:
                logger.debug(f"[ISRCEnricher] spclient error for {spotify_id}: {e}")
                return "error", None
            if r.status_code == 401 and attempt == 0:
                self._refresh_token(token)
                continue
            if r.status_code == 429:
                return "throttled", None
            if r.status_code == 404:
                return "missing", None
            if not r.ok:
                return "error", None
            try:
                data = r.json()
            except Exception:
                return "error", None
            isrc = None
            for ext in data.get("external_id") or []:
                if (ext.get("type") or "").lower() == "isrc" and ext.get("id"):
                    isrc = str(ext["id"]).strip().upper()
                    break
            meta = {
                "isrc": isrc,
                "track_number": data.get("number"),
                "disc_number": data.get("disc_number"),
                "release_date": _date_from_spclient((data.get("album") or {}).get("date")),
            }
            return ("ok" if isrc else "missing"), meta
        return "error", None

    def _fetch_isrc_via_gid(self, spotify_id: str) -> Optional[str]:
        """Single-track ISRC lookup (kept for callers of the old helper)."""
        if not self.token:
            try:
                self.token = self._get_anonymous_token()
            except Exception:
                return None
        _, meta = self._fetch_track_meta_via_gid(spotify_id)
        return (meta or {}).get("isrc")

    # ── enrichment ───────────────────────────────────────────────────────────
    @staticmethod
    def _apply_meta(track: TrackMetadata, meta: dict) -> bool:
        """Fill gaps only. Returns True when an ISRC was set."""
        filled = False
        isrc = meta.get("isrc")
        if isrc and not track.isrc:
            track.isrc = isrc
            filled = True

        # An Apple album keeps its own numbering and release identity; the
        # Spotify recording it was matched to may belong to a different release.
        if (
            (getattr(track, "source_service", "") or "").lower() == "apple"
            and (getattr(track, "request_kind", "") or "").lower() == "album"
        ):
            return filled

        rel = meta.get("release_date")
        if rel and not track.release_date and not track.release_year:
            track.release_date = rel
            track.release_year = int(rel[:4])
        number = meta.get("track_number")
        if isinstance(number, int) and number > 0 and not track.track_number:
            track.track_number = number
        disc = meta.get("disc_number")
        if isinstance(disc, int) and disc > 0 and track.disc_number is None:
            track.disc_number = disc
        return filled

    def enrich_tracks(self, tracks: list[TrackMetadata], max_workers: int = DEFAULT_WORKERS) -> int:
        """Fill missing ISRCs (and track/disc/date gaps). Returns ISRCs filled."""
        pending: dict[str, list[TrackMetadata]] = {}
        for t in tracks:
            if t.spotify_id and not t.isrc:
                pending.setdefault(t.spotify_id, []).append(t)
        if not pending:
            return 0

        # Persistent cache first (v1.1.8 FEAT-6): a fully-cached playlist makes no
        # network call at all — not even for a token.
        filled = 0
        cache = None
        try:
            from antra.core.isrc_cache import get_isrc_cache
            cache = get_isrc_cache()
        except Exception:
            cache = None
        if cache is not None:
            try:
                cached = cache.get_many(list(pending.keys()), include_negative=True)
            except Exception:
                cached = {}
            for tid, isrc in cached.items():
                for t in pending.pop(tid, []):
                    # isrc == "" is a cached negative: skip the lookup, set nothing
                    if isrc and not t.isrc:
                        t.isrc = isrc
                        filled += 1
            if cached:
                logger.info(
                    "[ISRCEnricher] %d ISRCs served from cache, %d left to fetch",
                    len(cached), len(pending),
                )
        if not pending:
            return filled

        ids = list(pending.keys())
        if len(ids) > MAX_LOOKUPS_PER_CALL:
            logger.info(
                "[ISRCEnricher] %d tracks need an ISRC; looking up the first %d this run",
                len(ids), MAX_LOOKUPS_PER_CALL,
            )
            ids = ids[:MAX_LOOKUPS_PER_CALL]

        try:
            self.token = self._get_anonymous_token()
        except Exception as e:
            logger.warning(f"[ISRCEnricher] No Spotify token for ISRC lookup: {e}")
            return filled

        started = time.monotonic()
        stop = threading.Event()
        throttle_lock = threading.Lock()
        consecutive_429 = [0]

        def work(tid: str) -> tuple[str, str, Optional[dict]]:
            if stop.is_set():
                return tid, "skipped", None
            status, meta = self._fetch_track_meta_via_gid(tid)
            with throttle_lock:
                if status == "throttled":
                    consecutive_429[0] += 1
                    if consecutive_429[0] >= THROTTLE_TRIP:
                        stop.set()
                else:
                    consecutive_429[0] = 0
            return tid, status, meta

        resolved: dict[str, str] = {}
        counts = {"ok": 0, "missing": 0, "throttled": 0, "error": 0, "skipped": 0}
        workers = max(1, min(max_workers, len(ids)))
        self._open_session(workers)
        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for fut in as_completed([ex.submit(work, tid) for tid in ids]):
                    try:
                        tid, status, meta = fut.result()
                    except Exception as e:
                        logger.debug(f"[ISRCEnricher] lookup crashed: {e}")
                        counts["error"] += 1
                        continue
                    counts[status] = counts.get(status, 0) + 1
                    if meta:
                        for t in pending.get(tid, []):
                            if self._apply_meta(t, meta):
                                filled += 1
                    # Only definitive answers are cached. A 429 or a transport error
                    # memoised as "no ISRC" would hide a real ISRC for a day.
                    if status == "ok":
                        resolved[tid] = meta["isrc"]
                    elif status == "missing":
                        resolved[tid] = ""
        finally:
            self._close_session()

        if stop.is_set():
            logger.warning(
                "[ISRCEnricher] spclient started throttling; %d lookups skipped this run",
                counts["skipped"],
            )
        if resolved and cache is not None:
            try:
                cache.put_many(resolved, source="spotify_spclient")
            except Exception:
                pass

        logger.info(
            "[ISRCEnricher] spclient: %d with ISRC, %d without, %d throttled, %d errors "
            "(%d lookups in %.1fs)",
            counts["ok"], counts["missing"], counts["throttled"], counts["error"],
            len(ids) - counts["skipped"], time.monotonic() - started,
        )
        return filled

    def enrich_via_gid(self, tracks, max_workers: int = DEFAULT_WORKERS) -> int:
        """Alias kept in step with the Antra-Web copy, which calls it by this name."""
        return self.enrich_tracks(tracks, max_workers=max_workers)


# --- Guessed-ISRC acceptance (BUG-MIX-1) ------------------------------------
# When a track has no ISRC, the website infers one from a Deezer text search.
# That is a guess about which RECORDING this is, and it used to be accepted on
# title/artist similarity >= 0.65 alone. Measured on the reported dance playlist,
# a wrong mix scores 0.76-1.00 against the right title, so the old bar accepted
# them; the ISRC then made every adapter return isrc_match=True for the wrong
# recording. Three independent signals must now agree.
GUESSED_ISRC_MIN_SCORE = 0.82
GUESSED_ISRC_DURATION_TOLERANCE_S = 5


def guessed_isrc_is_acceptable(
    track_title,
    track_artists,
    track_duration_ms,
    hit_title,
    hit_artist,
    hit_duration_s,
):
    """True when a text-search hit is a safe enough basis for borrowing its ISRC.

    Requires all three of: no recording-class / mix-designator mismatch between
    the titles, a high title+artist similarity, and a duration match. A missing
    duration on either side is a REJECT, not a pass -- the whole point is that
    the title alone cannot carry this decision.
    """
    from antra.utils.matching import duration_close, score_similarity, version_mismatch

    if not track_title or not hit_title:
        return False
    if version_mismatch(track_title, hit_title):
        return False
    score = score_similarity(track_title, list(track_artists or []), hit_title, hit_artist or "")
    if score < GUESSED_ISRC_MIN_SCORE:
        return False
    if not track_duration_ms or not hit_duration_s:
        return False
    try:
        return duration_close(
            float(track_duration_ms) / 1000.0,
            float(hit_duration_s),
            tolerance=GUESSED_ISRC_DURATION_TOLERANCE_S,
        )
    except (TypeError, ValueError):
        return False
