"""
Album availability helpers for Antra surfaces.

This is intentionally an Antra-flavoured variant of the userscript idea:
- Spotify exposes album-wide market lists directly.
- Deezer exposes country availability per track, so we report full-vs-partial
  album coverage instead of pretending there is a clean album-wide unavailable
  list.
"""
from __future__ import annotations

import logging
import re
import time
import base64
import hashlib
import hmac
import struct
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

import requests

from antra.core.config import Config
from antra.core.external_music_fetcher import ExternalMusicFetcher
from antra.core.spotify import SpotifyClient, _GQL_ALBUM_HASH

logger = logging.getLogger(__name__)

_SPOTIFY_ALBUM_RE = re.compile(
    r"(?:spotify\.com/(?:intl-[a-z-]+/)?album/|spotify:album:)(?P<id>[A-Za-z0-9]{22})",
    re.IGNORECASE,
)
_SPOTIFY_ALLOWED_MARKETS_META_RE = re.compile(
    r'<meta\s+(?:property|name)="(?:og:|music:)?restrictions:country:allowed"\s+content="([^"]+)"',
    re.IGNORECASE,
)

_SPOTIFY_MARKETS = sorted({
    "AD", "AE", "AG", "AL", "AM", "AO", "AR", "AT", "AU", "AZ",
    "BA", "BB", "BD", "BE", "BF", "BG", "BH", "BI", "BJ", "BN",
    "BO", "BR", "BS", "BT", "BW", "BY", "BZ", "CA", "CD", "CG",
    "CH", "CI", "CL", "CM", "CO", "CR", "CV", "CY", "CZ", "DE",
    "DJ", "DK", "DM", "DO", "DZ", "EC", "EE", "EG", "ES", "FI",
    "FJ", "FM", "FR", "GA", "GB", "GD", "GE", "GH", "GM", "GN",
    "GQ", "GR", "GT", "GW", "GY", "HK", "HN", "HR", "HT", "HU",
    "ID", "IE", "IL", "IN", "IQ", "IS", "IT", "JM", "JO", "JP",
    "KE", "KG", "KH", "KI", "KM", "KN", "KR", "KW", "KZ", "LA",
    "LB", "LC", "LI", "LK", "LR", "LS", "LT", "LU", "LV", "LY",
    "MA", "MC", "MD", "ME", "MG", "MH", "MK", "ML", "MN", "MO",
    "MR", "MT", "MU", "MV", "MW", "MX", "MY", "MZ", "NA", "NE",
    "NG", "NI", "NL", "NO", "NP", "NR", "NZ", "OM", "PA", "PE",
    "PG", "PH", "PK", "PL", "PS", "PT", "PW", "PY", "QA", "RO",
    "RS", "RW", "SA", "SB", "SC", "SE", "SG", "SI", "SK", "SL",
    "SM", "SN", "SR", "ST", "SV", "SZ", "TD", "TG", "TH", "TJ",
    "TL", "TN", "TO", "TR", "TT", "TW", "TZ", "UA", "UG", "US",
    "UY", "UZ", "VC", "VE", "VN", "VU", "WS", "XK", "ZA", "ZM",
    "ZW",
})

def is_supported_album_url(url: str) -> bool:
    return bool(_SPOTIFY_ALBUM_RE.search(url)) or _looks_like_deezer_album(url)


def lookup_album_availability(url: str, cfg: Config) -> dict[str, Any]:
    spotify_id = _extract_spotify_album_id(url)
    if spotify_id:
        return _lookup_spotify_album(spotify_id, cfg)

    deezer_id = _extract_deezer_album_id(url, cfg)
    if deezer_id:
        return _lookup_deezer_album(deezer_id)

    raise ValueError("Availability intel currently supports Spotify and Deezer album links only.")


def _extract_spotify_album_id(url: str) -> Optional[str]:
    match = _SPOTIFY_ALBUM_RE.search((url or "").strip())
    return match.group("id") if match else None


def _looks_like_deezer_album(url: str) -> bool:
    value = (url or "").lower()
    return "deezer.com" in value and "/album/" in value


def _extract_deezer_album_id(url: str, cfg: Config) -> Optional[str]:
    try:
        kind, item_id = ExternalMusicFetcher(cfg)._extract_deezer_kind_id(url)
    except Exception:
        return None
    return item_id if kind == "album" else None


def _spotify_headers(cfg: Config) -> dict[str, str]:
    token = _spotify_fast_access_token()
    if not token:
        client = SpotifyClient(
            cfg.spotify_client_id,
            cfg.spotify_client_secret,
            cfg.spotify_market,
            redirect_uri=cfg.spotify_redirect_uri,
            auth_storage_path=cfg.spotify_auth_path,
            sp_dc=cfg.spotify_sp_dc,
        )
        token = client._get_anonymous_access_token()
    if not token:
        raise RuntimeError("Spotify availability lookup could not obtain a catalog token.")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
    }


def _spotify_totp_code() -> str:
    secret = base64.b32decode(
        "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4D"
        "QOJUGQYTAMRRGA2TCMJSHE3TCMBY",
        casefold=True,
    )
    counter = int(time.time() // 30)
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"


def _spotify_fast_access_token() -> str:
    try:
        code = _spotify_totp_code()
        resp = requests.get(
            "https://open.spotify.com/api/token",
            params={
                "reason": "init",
                "productType": "web-player",
                "totp": code,
                "totpVer": "61",
                "totpServer": code,
            },
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/145.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
            },
            timeout=8,
        )
        resp.raise_for_status()
        return str(resp.json().get("accessToken") or "")
    except Exception as exc:
        logger.debug("Fast Spotify token request failed: %s", exc)
        return ""


def _spotify_get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            if resp.status_code == 429 and attempt < 2:
                retry_after = resp.headers.get("Retry-After", "").strip()
                wait_s = float(retry_after) if retry_after.isdigit() else (1.5 + attempt)
                time.sleep(wait_s)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.0 + attempt)
                continue
            break
    raise RuntimeError(f"Spotify availability lookup failed: {last_error}")


def _spotify_partner_album(headers: dict[str, str], album_id: str, market: str = "US") -> dict[str, Any]:
    payload = {
        "variables": {
                "uri": f"spotify:album:{album_id}",
                "locale": "",
                "offset": 0,
                "limit": 300,
                "market": market,
            },
        "operationName": "getAlbum",
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": _GQL_ALBUM_HASH,
            }
        },
    }
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api-partner.spotify.com/pathfinder/v2/query",
                headers=headers,
                json=payload,
                timeout=8,
            )
            if resp.status_code == 429 and attempt < 2:
                retry_after = resp.headers.get("Retry-After", "").strip()
                wait_s = float(retry_after) if retry_after.isdigit() else (0.75 + attempt)
                time.sleep(wait_s)
                continue
            resp.raise_for_status()
            return ((resp.json().get("data") or {}).get("albumUnion") or {})
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.5 + attempt)
                continue
            break
    raise RuntimeError(f"Spotify partner lookup failed for market {market}: {last_error}")


def _spotify_market_state(payload: dict[str, Any]) -> str:
    tracks = ((payload.get("tracksV2") or {}).get("items") or [])
    playability: list[bool] = []
    for item in tracks:
        track = item.get("track") if isinstance(item.get("track"), dict) else item
        if not isinstance(track, dict):
            continue
        playable = (track.get("playability") or {}).get("playable")
        if playable is None:
            continue
        playability.append(bool(playable))

    if playability:
        if all(playability):
            return "ok"
        if any(playability):
            return "warn"
        return "muted"

    album_playable = (payload.get("playability") or {}).get("playable")
    if album_playable is True:
        return "ok"
    return "muted"


def _spotify_public_album_html(album_id: str) -> str:
    resp = requests.get(
        f"https://open.spotify.com/album/{album_id}",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.text


def _spotify_allowed_markets_from_html(html: str) -> list[str]:
    match = _SPOTIFY_ALLOWED_MARKETS_META_RE.search(html or "")
    if not match:
        return []
    raw = match.group(1).strip()
    if not raw:
        return []
    parts = re.split(r"[\s,]+", raw)
    result = []
    seen: set[str] = set()
    for part in parts:
        code = part.strip().upper()
        if len(code) != 2 or code in seen:
            continue
        seen.add(code)
        result.append(code)
    return sorted(result)


def _lookup_spotify_album(album_id: str, cfg: Config) -> dict[str, Any]:
    headers = _spotify_headers(cfg)
    album = _spotify_partner_album(headers, album_id, market="US")
    all_markets = list(_SPOTIFY_MARKETS)
    html_allowed_markets = []
    try:
        html_allowed_markets = _spotify_allowed_markets_from_html(_spotify_public_album_html(album_id))
    except Exception as exc:
        logger.debug("Spotify public album page fetch failed for %s: %s", album_id, exc)

    available = []
    partial = []
    resolved_markets = []
    failed_markets = []
    with ThreadPoolExecutor(max_workers=24) as pool:
        futures = {
            pool.submit(_spotify_partner_album, headers, album_id, market): market
            for market in all_markets
        }
        for future in as_completed(futures):
            market = futures[future]
            try:
                payload = future.result()
                resolved_markets.append(market)
                state = _spotify_market_state(payload)
                if state == "ok":
                    available.append(market)
                elif state == "warn":
                    partial.append(market)
            except Exception:
                failed_markets.append(market)
                logger.debug("Spotify availability probe failed for %s", market)

    available = sorted(set(available))
    partial = sorted(set(partial))
    unavailable = sorted(
        code for code in resolved_markets
        if code not in set(available) and code not in set(partial)
    )
    used_html_fallback = False
    note_prefix = (
        "Spotify coverage is verified market-by-market through partner API "
        "playability. Antra no longer trusts the public album-page "
        "restrictions meta by itself because some releases expose incomplete "
        "country lists there."
    )
    if (
        html_allowed_markets
        and not available
        and not partial
        and len(unavailable) == len(resolved_markets)
    ):
        used_html_fallback = True
        available = sorted(set(html_allowed_markets))
        partial = []
        resolved_markets = list(all_markets)
        unavailable = sorted(code for code in all_markets if code not in set(available))
        note_prefix = (
            "Spotify's partner probe reported the album as unavailable in every "
            "market, but the public album page exposes explicit country "
            "restrictions for this release. Antra used that explicit Spotify "
            "country list instead."
        )
    elif html_allowed_markets and set(html_allowed_markets) != set(available):
        logger.info(
            "Spotify availability HTML mismatch for %s: html=%d probe=%d",
            album_id,
            len(html_allowed_markets),
            len(available),
        )

    artists = [
        item.get("profile", {}).get("name", "")
        for item in ((album.get("artists") or {}).get("items") or [])
        if isinstance(item, dict)
    ]

    artwork = None
    if album.get("images"):
        images = [item for item in (album.get("images") or []) if isinstance(item, dict) and item.get("url")]
        if images:
            artwork = max(images, key=lambda item: (item.get("width") or 0) * (item.get("height") or 0)).get("url")
    else:
        sources = ((album.get("coverArt") or {}).get("sources") or [])
        if sources:
            artwork = max(
                (item for item in sources if isinstance(item, dict) and item.get("url")),
                key=lambda item: (item.get("width") or 0) * (item.get("height") or 0),
                default={},
            ).get("url")

    release_date = str(((album.get("date") or {}).get("isoString") or ""))
    if "T" in release_date:
        release_date = release_date.split("T", 1)[0]
    year = int(release_date[:4]) if len(release_date) >= 4 and release_date[:4].isdigit() else None

    return {
        "service": "spotify",
        "release_name": album.get("name") or "",
        "artist": ", ".join(artists),
        "release_type": str(album.get("type") or "album").lower(),
        "year": year,
        "artwork_url": artwork,
        "label": album.get("label") or "",
        "upc": "",
        "notes": [
            note_prefix,
            *(
                [
                    "Spotify's public album page exposed a different country "
                    "list for this release, so Antra ignored it and used the "
                    "verified probe result instead."
                ]
                if html_allowed_markets and not used_html_fallback and set(html_allowed_markets) != set(available)
                else []
            ),
            *(
                [f"{len(failed_markets)} market probes did not respond and were excluded from the unavailable count."]
                if failed_markets else
                []
            ),
        ],
        "stats": [
            {"label": "Full-album markets", "value": len(available)},
            {"label": "Partial markets", "value": len(partial)},
            {"label": "Unavailable markets", "value": len(unavailable)},
            {"label": "Markets confirmed", "value": len(resolved_markets)},
            {"label": "Tracks", "value": int(((album.get("tracksV2") or {}).get("totalCount") or 0))},
        ],
        "segments": [
            {"label": "Entire album available", "tone": "ok", "codes": available},
            {"label": "Only some tracks available", "tone": "warn", "codes": partial},
            {"label": "Unavailable on Spotify", "tone": "muted", "codes": unavailable},
        ],
    }


def _lookup_deezer_album(album_id: str) -> dict[str, Any]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/html, */*",
        }
    )

    album_resp = session.get(f"https://api.deezer.com/album/{album_id}", timeout=20)
    album_resp.raise_for_status()
    album = album_resp.json()
    if isinstance(album, dict) and album.get("error"):
        raise RuntimeError(f"Deezer API error: {album['error']}")

    track_items = ((album.get("tracks") or {}).get("data") or [])
    if not track_items:
        raise RuntimeError("Deezer album response did not include any tracks.")

    country_sets: list[set[str]] = []
    for track in track_items:
        track_id = track.get("id")
        if not track_id:
            continue
        resp = session.get(f"https://api.deezer.com/track/{track_id}", timeout=20)
        resp.raise_for_status()
        detail = resp.json()
        if isinstance(detail, dict) and detail.get("error"):
            logger.debug("Skipping Deezer track %s availability: %s", track_id, detail["error"])
            continue
        countries = {
            str(code).upper()
            for code in (detail.get("available_countries") or [])
            if code
        }
        country_sets.append(countries)

    if not country_sets:
        raise RuntimeError("Deezer track availability data could not be loaded for this album.")

    union = sorted(set().union(*country_sets))
    intersection = sorted(set.intersection(*country_sets) if country_sets else set())
    partial = sorted(code for code in union if code not in set(intersection))
    artist = ((album.get("artist") or {}).get("name") or "")

    return {
        "service": "deezer",
        "release_name": album.get("title") or "",
        "artist": artist,
        "release_type": "album",
        "year": int(str(album.get("release_date") or "")[:4]) if str(album.get("release_date") or "")[:4].isdigit() else None,
        "artwork_url": (
            album.get("cover_xl")
            or album.get("cover_big")
            or album.get("cover_medium")
            or ""
        ),
        "label": album.get("label") or "",
        "upc": album.get("upc") or "",
        "notes": [
            "Deezer exposes country lists per track, so Antra reports where the full album is available versus where coverage is only partial.",
        ],
        "stats": [
            {"label": "Full-album countries", "value": len(intersection)},
            {"label": "Partial-coverage countries", "value": len(partial)},
            {"label": "Tracks inspected", "value": len(country_sets)},
        ],
        "segments": [
            {"label": "Entire album available", "tone": "ok", "codes": intersection},
            {"label": "Only some tracks available", "tone": "warn", "codes": partial},
        ],
    }
