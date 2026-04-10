"""
Amazon Music URL fetcher — extracts track lists from Amazon Music URLs.

Supports:
  - Tracks:    https://music.amazon.com/tracks/{ASIN}
  - Albums:    https://music.amazon.com/albums/{ASIN}
  - Playlists: https://music.amazon.com/playlists/{ASIN}?marketplaceId=...

Strategy:
  Amazon Music is a React SPA that serves a minimal JS shell to normal browsers.
  However, when accessed with a crawler User-Agent (Googlebot), Amazon returns
  fully server-side-rendered HTML with track data embedded in custom web
  component attributes (<music-image-row>, <music-horizontal-item>,
  <music-detail-header>).

  No authentication required for publicly accessible content.
  For private playlists, set AMAZON_COOKIES_PATH to a Netscape cookies file.
"""

import html as html_module
import logging
import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests

from antra.core.models import TrackMetadata

logger = logging.getLogger(__name__)

_AMAZON_MUSIC_BASE = "https://music.amazon.com"
_URL_ASIN_RE = re.compile(
    r"music\.amazon\.com/(tracks|albums|playlists)/([A-Z0-9]{10})"
)

REQUEST_TIMEOUT = 20

# Googlebot UA causes Amazon Music to serve SSR HTML with full track data
_CRAWLER_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def is_amazon_music_url(url: str) -> bool:
    """Return True if the URL looks like an Amazon Music URL."""
    return "music.amazon.com" in url


class AmazonMusicFetcher:
    """
    Resolves Amazon Music URLs to lists of TrackMetadata.

    Does not download audio — just collects metadata for the Antra
    waterfall to act on.
    """

    def __init__(
        self,
        mirrors: Optional[list[str]] = None,
        cookies_path: str = "",
    ):
        self._mirrors = [m.rstrip("/") for m in (mirrors or []) if m]
        self._cookies_path = cookies_path

    # ── Public entry point ────────────────────────────────────────────────────

    def fetch(self, url: str) -> list[TrackMetadata]:
        """
        Resolve an Amazon Music URL to a list of TrackMetadata objects.
        Raises ValueError for unrecognised URLs.
        Raises RuntimeError if metadata cannot be retrieved.
        """
        url = url.strip()
        url_type, asin = self._parse_url(url)

        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        marketplace_id = (qs.get("marketplaceId") or ["ATVPDKIKX0DER"])[0]

        if url_type == "tracks":
            return self._fetch_track(asin, marketplace_id)
        elif url_type == "albums":
            return self._fetch_album(asin, marketplace_id)
        elif url_type == "playlists":
            return self._fetch_playlist(asin, marketplace_id)
        else:
            raise ValueError(f"[AmazonMusic] Unsupported URL type: {url_type}")

    # ── URL parsing ───────────────────────────────────────────────────────────

    def _parse_url(self, url: str) -> tuple[str, str]:
        """Return (url_type, asin). Raises ValueError on bad URLs."""
        m = _URL_ASIN_RE.search(url)
        if not m:
            raise ValueError(
                f"[AmazonMusic] Not a recognised Amazon Music URL: {url}\n"
                "Supported: music.amazon.com/tracks/..., /albums/..., /playlists/..."
            )
        return m.group(1), m.group(2)

    # ── Fetchers ──────────────────────────────────────────────────────────────

    def _fetch_track(self, asin: str, marketplace_id: str) -> list[TrackMetadata]:
        """Fetch a single track's metadata from the track detail page."""
        url = f"{_AMAZON_MUSIC_BASE}/tracks/{asin}"
        html = self._get_page(url, marketplace_id)
        if not html:
            raise RuntimeError(f"[AmazonMusic] Could not fetch track page for ASIN {asin}.")

        tracks = self._parse_track_page(html)
        if not tracks:
            raise RuntimeError(
                f"[AmazonMusic] Could not find track metadata in page for ASIN {asin}."
            )
        return tracks

    def _fetch_album(self, asin: str, marketplace_id: str) -> list[TrackMetadata]:
        """Fetch album track listing from the album detail page."""
        url = f"{_AMAZON_MUSIC_BASE}/albums/{asin}"
        html = self._get_page(url, marketplace_id)
        if not html:
            raise RuntimeError(f"[AmazonMusic] Could not fetch album page for ASIN {asin}.")

        tracks = self._parse_tracklist_page(html, url_type="album")
        if not tracks:
            raise RuntimeError(
                f"[AmazonMusic] Could not find track listing in album page for ASIN {asin}."
            )
        return tracks

    def _fetch_playlist(self, asin: str, marketplace_id: str) -> list[TrackMetadata]:
        """Fetch playlist track listing from the playlist detail page."""
        url = f"{_AMAZON_MUSIC_BASE}/playlists/{asin}?marketplaceId={marketplace_id}&musicTerritory=US"
        html = self._get_page(url, marketplace_id)
        if not html:
            raise RuntimeError(f"[AmazonMusic] Could not fetch playlist page for ASIN {asin}.")

        tracks = self._parse_tracklist_page(html, url_type="playlist")
        if not tracks:
            raise RuntimeError(
                f"[AmazonMusic] Could not find track listing in playlist page for ASIN {asin}.\n"
                "The playlist may be private. Set AMAZON_COOKIES_PATH in your .env "
                "to a Netscape-format cookies file from an authenticated Amazon Music session."
            )
        return tracks

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _get_page(self, url: str, marketplace_id: str) -> Optional[str]:
        """
        Fetch an Amazon Music page. Uses Googlebot UA which causes Amazon
        to return server-side-rendered HTML with embedded track data.
        Falls back to cookie-auth if configured and the SSR response has
        no tracks.
        """
        session = requests.Session()
        session.headers.update({**_BASE_HEADERS, "User-Agent": _CRAWLER_UA})

        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            logger.debug(f"[AmazonMusic] Page fetch failed: {e}")
            return None

        if resp.status_code in (401, 403):
            logger.debug("[AmazonMusic] Page requires authentication (crawler UA blocked)")
            if self._cookies_path:
                return self._get_page_with_cookies(url)
            return None

        if not resp.ok:
            logger.debug(f"[AmazonMusic] Page returned {resp.status_code}")
            return None

        return resp.text

    def _get_page_with_cookies(self, url: str) -> Optional[str]:
        """Retry the page request using Amazon session cookies."""
        import http.cookiejar

        try:
            cj = http.cookiejar.MozillaCookieJar()
            cj.load(self._cookies_path, ignore_discard=True, ignore_expires=True)
        except Exception as e:
            logger.warning(f"[AmazonMusic] Failed to load cookies from {self._cookies_path}: {e}")
            return None

        session = requests.Session()
        session.headers.update({**_BASE_HEADERS, "User-Agent": _BROWSER_UA})
        session.cookies = cj  # type: ignore[assignment]

        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            return resp.text if resp.ok else None
        except Exception as e:
            logger.debug(f"[AmazonMusic] Cookie-auth request failed: {e}")
            return None

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse_track_page(self, html: str) -> list[TrackMetadata]:
        """
        Parse a /tracks/{ASIN} page.
        Amazon uses <music-detail-header label="SONG"> for the track hero section.
        """
        # The detail header for a track has label="SONG" (or "EXPLICIT SONG"),
        # headline = track title, primary-text = artist name,
        # secondary-text = album name.
        headers_raw = re.findall(r'<music-detail-header\b([^>]+)>', html, re.DOTALL)
        for attrs in headers_raw:
            label = _get_dq('label', attrs).upper()
            if 'SONG' not in label and 'TRACK' not in label:
                continue
            title = _get_dq('headline', attrs)
            artist = _get_dq('primary-text', attrs)
            album = _get_dq('secondary-text', attrs)
            if not title:
                continue
            return [TrackMetadata(
                title=title,
                artists=[artist] if artist else [],
                album=album or "",
            )]

        # Fallback: look for any music-detail-header with a headline (track title)
        for attrs in headers_raw:
            title = _get_dq('headline', attrs)
            artist = _get_dq('primary-text', attrs)
            if title and artist:
                return [TrackMetadata(title=title, artists=[artist], album="")]

        return []

    def _parse_tracklist_page(self, html: str, url_type: str) -> list[TrackMetadata]:
        """
        Parse an album or playlist page.

        Playlists use <music-image-row> elements where track rows have
        a trackAsin parameter in primary-href:
          primary-text    = track title
          secondary-text-1 = artist
          secondary-text-2 = album
          duration        = "MM:SS"

        Albums use <music-horizontal-item> elements:
          primary-text    = track title
          secondary-text  = artist
          duration        = "MM:SS"
          primary-href contains trackAsin
        """
        tracks: list[TrackMetadata] = []

        # --- Playlist: music-image-row ---
        for attrs in re.findall(r'<music-image-row\b([^>]+)>', html, re.DOTALL):
            href = _get_dq('primary-href', attrs)
            if 'trackAsin=' not in href:
                continue
            title = _get_dq('primary-text', attrs)
            if not title:
                continue
            artist = _get_dq('secondary-text-1', attrs)
            album = _get_dq('secondary-text-2', attrs)
            dur_ms = _parse_duration(_get_dq('duration', attrs))
            tracks.append(TrackMetadata(
                title=title,
                artists=[artist] if artist else [],
                album=album or "",
                duration_ms=dur_ms,
            ))

        if tracks:
            return tracks

        # --- Album: music-horizontal-item ---
        for attrs in re.findall(r'<music-horizontal-item\b([^>]+)>', html, re.DOTALL):
            href = _get_dq('primary-href', attrs)
            if 'trackAsin=' not in href:
                continue
            title = _get_dq('primary-text', attrs)
            if not title:
                continue
            artist = _get_dq('secondary-text', attrs)
            # Albums don't embed the album name in track rows — get it from the detail header
            album = self._extract_album_name(html)
            dur_ms = _parse_duration(_get_dq('duration', attrs))
            tracks.append(TrackMetadata(
                title=title,
                artists=[artist] if artist else [],
                album=album or "",
                duration_ms=dur_ms,
            ))

        return tracks

    def _extract_album_name(self, html: str) -> str:
        """Extract album name from the page's detail header."""
        headers_raw = re.findall(r'<music-detail-header\b([^>]+)>', html, re.DOTALL)
        for attrs in headers_raw:
            label = _get_dq('label', attrs).upper()
            if 'ALBUM' in label or 'EP' in label:
                return _get_dq('primary-text', attrs)
        # Fallback: first detail header's primary-text
        if headers_raw:
            name = _get_dq('primary-text', headers_raw[0])
            if name:
                return name
        return ""

    # ── Artist discography ────────────────────────────────────────────────────

    def fetch_artist_discography_info(self, url: str) -> dict:
        """
        Return artist metadata + full album list for the discography picker UI.
        Fetches the artist page via the Googlebot SSR trick and parses album cards.
        """
        m = re.search(r"music\.amazon\.com/artists/([A-Z0-9a-z0-9_-]+)", url)
        if not m:
            raise ValueError(f"[AmazonMusic] Cannot parse artist ID from: {url}")
        artist_id = m.group(1)

        artist_url = f"{_AMAZON_MUSIC_BASE}/artists/{artist_id}"
        html = self._get_page(artist_url, "ATVPDKIKX0DER")
        if not html:
            raise RuntimeError(f"[AmazonMusic] Could not fetch artist page for {artist_id}")

        # Artist name from detail header
        artist_name = "Unknown Artist"
        artwork_url = None
        for attrs in re.findall(r'<music-detail-header\b([^>]+)>', html, re.DOTALL):
            name = _get_dq('primary-text', attrs)
            if name:
                artist_name = name
            img = _get_dq('image-src', attrs)
            if img:
                artwork_url = img
            break

        # Determine section type by tracking the nearest preceding music-text-header
        # Split HTML into sections by music-text-header headlines
        albums: list[dict] = []
        seen_ids: set[str] = set()

        # Find section boundaries by headline
        section_type = "album"
        # We'll scan the HTML linearly, updating section_type when we see a header
        combined = re.findall(
            r'(<music-text-header\b[^>]+>|<music-image-row\b[^>]+>|<music-horizontal-item\b[^>]+>)',
            html,
            re.DOTALL,
        )

        for tag in combined:
            if tag.startswith('<music-text-header'):
                headline = _get_dq('headline', tag).lower()
                if 'single' in headline or 'ep' in headline:
                    section_type = "single"
                elif 'compilation' in headline:
                    section_type = "compilation"
                elif 'album' in headline:
                    section_type = "album"
                continue

            # music-image-row or music-horizontal-item
            href = _get_dq('primary-href', tag)
            if '/albums/' not in href:
                continue

            album_asin_match = re.search(r'/albums/([A-Z0-9]{10})', href)
            if not album_asin_match:
                continue
            album_asin = album_asin_match.group(1)
            if album_asin in seen_ids:
                continue
            seen_ids.add(album_asin)

            name = _get_dq('primary-text', tag)
            if not name:
                continue

            secondary = _get_dq('secondary-text-1', tag) or _get_dq('secondary-text', tag)
            year = None
            year_m = re.search(r'\b(19|20)\d{2}\b', secondary)
            if year_m:
                year = int(year_m.group(0))

            img = _get_dq('image-src', tag)
            album_url = f"{_AMAZON_MUSIC_BASE}/albums/{album_asin}"
            albums.append({
                "id": album_asin,
                "url": album_url,
                "name": name,
                "type": section_type,
                "year": year,
                "track_count": 0,  # Amazon SSR doesn't expose track count on artist pages
                "artwork_url": img or None,
            })

        return {
            "artist_id": artist_id,
            "artist_name": artist_name,
            "artwork_url": artwork_url,
            "albums": albums,
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_dq(attr: str, text: str) -> str:
    """
    Extract a double-quoted HTML attribute value, unescaping HTML entities.
    Returns empty string if not found.
    """
    m = re.search(rf'\b{re.escape(attr)}="([^"]*)"', text)
    return html_module.unescape(m.group(1)) if m else ""


def _parse_duration(s: str) -> Optional[int]:
    """
    Parse a "MM:SS" or "H:MM:SS" duration string into milliseconds.
    Returns None for empty/invalid input.
    """
    if not s:
        return None
    parts = s.split(":")
    try:
        if len(parts) == 2:
            return (int(parts[0]) * 60 + int(parts[1])) * 1000
        elif len(parts) == 3:
            return (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])) * 1000
    except (ValueError, IndexError):
        pass
    return None
