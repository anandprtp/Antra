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
import json
import logging
import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests

from antra.core.models import TrackMetadata

logger = logging.getLogger(__name__)

_AMAZON_MUSIC_BASE = "https://music.amazon.com"
_URL_ASIN_RE = re.compile(
    r"(music\.amazon\.[a-z\.]+)/(tracks|albums|playlists|user-playlists)/([A-Za-z0-9_-]+)"
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
    return "music.amazon." in url


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
        domain, url_type, asin = self._parse_url(url)

        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        marketplace_id = (qs.get("marketplaceId") or ["ATVPDKIKX0DER"])[0]

        if url_type == "tracks":
            return self._fetch_track(domain, asin, marketplace_id)
        elif url_type == "albums":
            return self._fetch_album(domain, asin, marketplace_id)
        elif url_type in ("playlists", "user-playlists"):
            return self._fetch_playlist(domain, url_type, asin, marketplace_id)
        else:
            raise ValueError(f"[AmazonMusic] Unsupported URL type: {url_type}")

    # ── URL parsing ───────────────────────────────────────────────────────────

    def _parse_url(self, url: str) -> tuple[str, str, str]:
        """Return (domain, url_type, asin). Raises ValueError on bad URLs."""
        m = _URL_ASIN_RE.search(url)
        if not m:
            raise ValueError(
                f"[AmazonMusic] Not a recognised Amazon Music URL: {url}\n"
                "Supported: music.amazon.*/tracks/..., /albums/..., /playlists/..., /user-playlists/..."
            )
        return m.group(1), m.group(2), m.group(3)

    # ── Fetchers ──────────────────────────────────────────────────────────────

    def _fetch_track(self, domain: str, asin: str, marketplace_id: str) -> list[TrackMetadata]:
        """Fetch a single track's metadata from the track detail page."""
        url = f"https://{domain}/tracks/{asin}"
        html = self._get_page(url, marketplace_id)
        if not html:
            raise RuntimeError(f"[AmazonMusic] Could not fetch track page for ASIN {asin}.")

        tracks = self._parse_track_page(html)
        if not tracks:
            raise RuntimeError(
                f"[AmazonMusic] Could not find track metadata in page for ASIN {asin}."
            )
        return tracks

    def _fetch_album(self, domain: str, asin: str, marketplace_id: str) -> list[TrackMetadata]:
        """Fetch album track listing from the album detail page."""
        url = f"https://{domain}/albums/{asin}"
        html = self._get_page(url, marketplace_id)
        if not html:
            raise RuntimeError(f"[AmazonMusic] Could not fetch album page for ASIN {asin}.")

        # Try JSON-LD first (schema.org MusicAlbum — reliable, Amazon always embeds it)
        tracks = self._parse_jsonld_album(html)
        if tracks:
            # JSON-LD gives flat position numbers — disc info is not in schema.org.
            # Supplement with disc headers extracted from the rendered HTML.
            self._assign_disc_numbers_from_html(html, tracks)
            return tracks

        # Fall back to web-component attribute parsing (older page format)
        tracks = self._parse_tracklist_page(html, url_type="album")
        if not tracks:
            raise RuntimeError(
                f"[AmazonMusic] Could not find track listing in album page for ASIN {asin}."
            )
        return tracks

    def _fetch_playlist(self, domain: str, url_type: str, asin: str, marketplace_id: str) -> list[TrackMetadata]:
        """Fetch playlist track listing from the playlist detail page."""
        url = f"https://{domain}/{url_type}/{asin}?marketplaceId={marketplace_id}"
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
            img = _get_dq('image-src', attrs)
            if not title:
                continue
            return [TrackMetadata(
                title=title,
                artists=[artist] if artist else [],
                album=album or "",
                artwork_url=img or None,
            )]

        # Fallback: look for any music-detail-header with a headline (track title)
        for attrs in headers_raw:
            title = _get_dq('headline', attrs)
            artist = _get_dq('primary-text', attrs)
            img = _get_dq('image-src', attrs)
            if title and artist:
                return [TrackMetadata(title=title, artists=[artist], album="", artwork_url=img or None)]

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
        name, playlist_img = self._extract_album_metadata(html)
        is_playlist = url_type in ("playlist", "playlists", "user-playlists")
        playlist_name = name if is_playlist and name else None

        for idx, attrs in enumerate(re.findall(r'<music-image-row\b([^>]+)>', html, re.DOTALL), start=1):
            href = _get_dq('primary-href', attrs)
            if 'trackAsin=' not in href:
                continue
            title = _get_dq('primary-text', attrs)
            if not title:
                continue
            artist = _get_dq('secondary-text-1', attrs)
            album = _get_dq('secondary-text-2', attrs)
            img = _get_dq('image-src', attrs) or playlist_img
            dur_ms = _parse_duration(_get_dq('duration', attrs))
            tracks.append(TrackMetadata(
                title=title,
                artists=[artist] if artist else [],
                album=album or "",
                duration_ms=dur_ms,
                artwork_url=img or None,
                playlist_name=playlist_name,
                playlist_position=idx if playlist_name else None,
            ))

        if tracks:
            return tracks

        # --- Album: music-horizontal-item ---
        # Parse disc headers and item positions together to assign disc numbers.
        album, album_img = self._extract_album_metadata(html)
        disc_header_re = re.compile(r'\b(?:Disc|CD)\s+(\d+)\b', re.IGNORECASE)
        disc_headers = [(m.start(), int(m.group(1))) for m in disc_header_re.finditer(html)]

        item_matches = list(re.finditer(r'<music-horizontal-item\b([^>]+)>', html, re.DOTALL))
        disc_track_counts: dict[int, int] = {}

        for item_m in item_matches:
            attrs = item_m.group(1)
            href = _get_dq('primary-href', attrs)
            if 'trackAsin=' not in href:
                continue
            title = _get_dq('primary-text', attrs)
            if not title:
                continue
            artist = _get_dq('secondary-text', attrs)
            dur_ms = _parse_duration(_get_dq('duration', attrs))

            # Determine disc number from most-recently-preceding disc header
            disc = 1
            if disc_headers:
                for hpos, hnum in disc_headers:
                    if hpos < item_m.start():
                        disc = hnum
            disc_track_counts[disc] = disc_track_counts.get(disc, 0) + 1
            track_num_in_disc = disc_track_counts[disc]

            tracks.append(TrackMetadata(
                title=title,
                artists=[artist] if artist else [],
                album=album or "",
                duration_ms=dur_ms,
                track_number=track_num_in_disc if not is_playlist else None,
                disc_number=disc if disc_headers and not is_playlist else None,
                artwork_url=album_img or None,
                playlist_name=playlist_name,
                playlist_position=len(tracks) + 1 if is_playlist else None,
            ))

        return tracks

    def _parse_jsonld_album(self, html: str) -> list[TrackMetadata]:
        """
        Parse schema.org JSON-LD embedded in the album page.
        Amazon always includes a <script type="application/ld+json"> block with
        the full MusicAlbum entity including track list and per-track ASINs.
        """
        # Find the JSON-LD script block
        m = re.search(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html,
            re.DOTALL | re.IGNORECASE,
        )
        if not m:
            # Fallback: find any script whose content starts with a MusicAlbum object
            for script_content in re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
                sc = script_content.strip()
                if '"MusicAlbum"' in sc and sc.startswith('{'):
                    m = type('m', (), {'group': lambda self, n: sc})()
                    break

        if not m:
            return []

        try:
            data = json.loads(m.group(1))
        except Exception as e:
            logger.debug(f"[AmazonMusic] JSON-LD parse failed: {e}")
            return []

        if data.get("@type") != "MusicAlbum":
            return []

        album_name = data.get("name", "")
        album_artist = data.get("byArtist", {}).get("name", "")
        release_date = (data.get("datePublished") or "")[:10]  # "2022-12-02"
        release_year = int(release_date[:4]) if release_date[:4].isdigit() else None

        _, artwork_url = self._extract_album_metadata(html)

        raw_tracks = data.get("track", [])
        if not raw_tracks:
            return []

        tracks: list[TrackMetadata] = []
        for item in raw_tracks:
            if item.get("@type") != "MusicRecording":
                continue
            title = item.get("name", "").strip()
            if not title:
                continue

            # ASIN from track URL: https://music.amazon.com/tracks/{ASIN}
            track_url = item.get("url", "")
            asin_m = re.search(r"/tracks/([A-Z0-9]{10})", track_url)
            amazon_asin = asin_m.group(1) if asin_m else None

            # ISO 8601 duration e.g. "PT3M42S" → ms
            duration_ms = _parse_iso_duration(item.get("duration", ""))
            position = item.get("position")

            tracks.append(TrackMetadata(
                title=title,
                artists=[album_artist] if album_artist else [],
                album=album_name,
                album_artists=[album_artist] if album_artist else [],
                release_date=release_date or None,
                release_year=release_year,
                track_number=int(position) if position else None,
                total_tracks=len(raw_tracks),
                duration_ms=duration_ms,
                amazon_asin=amazon_asin,
                artwork_url=artwork_url or None,
            ))

        logger.info(f"[AmazonMusic] JSON-LD: parsed {len(tracks)} tracks for album {album_name!r}")
        return tracks

    def _assign_disc_numbers_from_html(self, html: str, tracks: list[TrackMetadata]) -> None:
        """Scan the album HTML for disc section headers and stamp disc_number on tracks.

        Amazon album pages render disc separators as text like "Disc 1" / "Disc 2"
        (or "CD 1" / "CD 2") somewhere before the corresponding <music-horizontal-item>
        blocks in the SSR HTML.  We locate those headers and the track item elements,
        both by their byte position in the HTML, then assign each track the disc number
        of the most-recently-seen header above it.

        The method modifies `tracks` in place and also resets track_number to be
        1-based within each disc (so disc-2 track 1 stays "1", not "13").
        If no disc headers are found the list is left unchanged.
        """
        disc_header_re = re.compile(r'\b(?:Disc|CD)\s+(\d+)\b', re.IGNORECASE)
        disc_headers = [(m.start(), int(m.group(1))) for m in disc_header_re.finditer(html)]
        if not disc_headers:
            return

        item_positions = [m.start() for m in re.finditer(r'<music-horizontal-item\b', html)]
        if len(item_positions) != len(tracks):
            # Can't reliably line up HTML positions with JSON-LD tracks — skip
            logger.debug(
                f"[AmazonMusic] Disc detection: {len(item_positions)} HTML items vs "
                f"{len(tracks)} JSON-LD tracks — skipping disc assignment"
            )
            return

        disc_counts: dict[int, int] = {}
        for i, item_pos in enumerate(item_positions):
            disc = 1
            for hpos, hnum in disc_headers:
                if hpos < item_pos:
                    disc = hnum
            disc_counts[disc] = disc_counts.get(disc, 0) + 1
            tracks[i].disc_number = disc
            tracks[i].track_number = disc_counts[disc]

        logger.debug(
            f"[AmazonMusic] Disc assignment: "
            + ", ".join(f"disc {d}: {c} tracks" for d, c in sorted(disc_counts.items()))
        )

    def _extract_album_metadata(self, html: str) -> tuple[str, Optional[str]]:
        """Extract album name and artwork URL from the page's detail header."""
        headers_raw = re.findall(r'<music-detail-header\b([^>]+)>', html, re.DOTALL)
        for attrs in headers_raw:
            label = _get_dq('label', attrs).upper()
            if 'ALBUM' in label or 'EP' in label or 'PLAYLIST' in label:
                name = _get_dq('headline', attrs) or _get_dq('primary-text', attrs)
                return name, _get_dq('image-src', attrs) or None
        # Fallback: first detail header
        if headers_raw:
            name = _get_dq('headline', headers_raw[0]) or _get_dq('primary-text', headers_raw[0])
            img = _get_dq('image-src', headers_raw[0])
            if name:
                return name, img or None
        return "", None

    # ── Artist discography ────────────────────────────────────────────────────

    def fetch_artist_discography_info(self, url: str) -> dict:
        """
        Return artist metadata + full album list for the discography picker UI.
        Fetches the artist page via the Googlebot SSR trick and parses album cards.
        """
        m = re.search(r"(music\.amazon\.[a-z\.]+)/artists/([A-Z0-9a-z0-9_-]+)", url)
        if not m:
            raise ValueError(f"[AmazonMusic] Cannot parse artist ID from: {url}")
        domain = m.group(1)
        artist_id = m.group(2)

        artist_url = f"https://{domain}/artists/{artist_id}"
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
            album_url = f"https://{domain}/albums/{album_asin}"
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


def _parse_iso_duration(s: str) -> Optional[int]:
    """
    Parse an ISO 8601 duration string (e.g. "PT3M42S", "PT1H2M3S") into milliseconds.
    Returns None for empty/invalid input.
    """
    if not s:
        return None
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?', s)
    if not m:
        return None
    try:
        hours = int(m.group(1) or 0)
        minutes = int(m.group(2) or 0)
        seconds = float(m.group(3) or 0)
        return int((hours * 3600 + minutes * 60 + seconds) * 1000)
    except (ValueError, TypeError):
        return None
