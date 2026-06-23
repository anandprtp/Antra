"""
YouTube Music metadata fetcher.

Uses yt-dlp (already bundled in the PyInstaller backend) for metadata-only
extraction — no audio is downloaded from YouTube.  The resulting TrackMetadata
objects are fed into the standard SourceResolver chain (Tidal, Qobuz, Amazon,
Deezer mirror servers) so every output quality tier works exactly as it does
for Spotify or Apple Music URLs.

Supported URL shapes
--------------------
  music.youtube.com/watch?v=XXXX               single track
  music.youtube.com/playlist?list=XXXX         user / editorial playlist
  music.youtube.com/browse/MPREb_XXXX          album browse page
"""
import logging
import re
from typing import Optional

from antra.core.models import TrackMetadata

logger = logging.getLogger(__name__)

# Matches Topic-channel suffix YouTube Music adds to auto-generated artist channels
_TOPIC_RE = re.compile(r"\s*-\s*Topic$", re.IGNORECASE)


def is_youtube_music_url(url: str) -> bool:
    """Return True for any music.youtube.com URL."""
    return "music.youtube.com" in (url or "")


class YouTubeMusicFetcher:
    """Fetch track metadata from YouTube Music URLs via yt-dlp.

    No audio is ever downloaded from YouTube.  yt-dlp is used purely for
    its metadata extraction path (ISRCs, artist/album tags, duration).
    """

    def fetch(self, url: str, page_callback=None) -> list[TrackMetadata]:
        """Fetch track metadata from any YouTube Music URL.

        Returns a list of TrackMetadata objects.  Single-track URLs return a
        one-element list; playlist/album URLs return all tracks.

        page_callback, if provided, is called with the current (partial) track
        list every 50 tracks so the UI can display tracks progressively.
        """
        try:
            import yt_dlp
        except ImportError:
            raise RuntimeError(
                "yt-dlp is required for YouTube Music support but is not installed."
            )

        # Single track: has ?v= without a list= that overrides it
        is_single = (
            re.search(r"[?&]v=([A-Za-z0-9_-]+)", url) is not None
            and "list=" not in url
        )

        if is_single:
            return self._fetch_single(url, yt_dlp)
        return self._fetch_playlist(url, yt_dlp, page_callback=page_callback)

    # ── Single track ────────────────────────────────────────────────────────

    def _fetch_single(self, url: str, yt_dlp) -> list[TrackMetadata]:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "extract_flat": False,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            raise RuntimeError(f"[YouTube Music] Failed to fetch track metadata: {exc}") from exc

        if not info:
            raise RuntimeError("[YouTube Music] No metadata returned for this URL.")

        # YTM sometimes returns a tiny playlist wrapper even for single watch URLs
        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            if entries:
                info = entries[0]

        track = self._info_to_track(info, playlist_name=None, playlist_artwork=None, index=1)
        if not track:
            raise RuntimeError("[YouTube Music] Could not parse track metadata from this URL.")

        track.request_kind = "track"
        logger.info("[YouTube Music] Fetched single track: %s – %s (ISRC: %s)",
                    track.artist_string, track.title, track.isrc or "none")
        return [track]

    # ── Playlist / album ────────────────────────────────────────────────────

    def _fetch_playlist(
        self,
        url: str,
        yt_dlp,
        page_callback=None,
    ) -> list[TrackMetadata]:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "ignoreerrors": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            raise RuntimeError(f"[YouTube Music] Failed to fetch playlist metadata: {exc}") from exc

        if not info:
            raise RuntimeError("[YouTube Music] No metadata returned for this URL.")

        playlist_name = (
            (info.get("title") or info.get("playlist_title") or "").strip() or None
        )
        playlist_artwork = self._best_thumbnail(info)

        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError(
                "[YouTube Music] Playlist is empty or private. "
                "Make sure the playlist is public and the URL is correct."
            )

        # Album browse pages use /browse/ in the URL
        is_album = "/browse/" in url.lower()
        request_kind = "album" if is_album else "playlist"

        tracks: list[TrackMetadata] = []
        for index, entry in enumerate(entries, start=1):
            track = self._info_to_track(
                entry,
                playlist_name=playlist_name,
                playlist_artwork=playlist_artwork,
                index=index,
            )
            if not track:
                continue
            track.request_kind = request_kind
            if is_album and playlist_name and not track.album:
                track.album = playlist_name
            tracks.append(track)

            if page_callback and index % 50 == 0:
                try:
                    page_callback(list(tracks))
                except Exception:
                    pass

        if not tracks:
            raise RuntimeError("[YouTube Music] Could not extract any valid tracks from this URL.")

        logger.info(
            "[YouTube Music] Fetched %d tracks from '%s'",
            len(tracks),
            playlist_name or url,
        )
        return tracks

    # ── Entry → TrackMetadata conversion ───────────────────────────────────

    def _info_to_track(
        self,
        info: dict,
        playlist_name: Optional[str],
        playlist_artwork: Optional[str],
        index: int,
    ) -> Optional[TrackMetadata]:
        if not info:
            return None

        video_id = (info.get("id") or "").strip()
        if not video_id:
            return None

        # ── Title ──────────────────────────────────────────────────────────
        # YouTube Music populates `track` when the video is a music entry;
        # fall back to splitting the raw `title` on " - " (common YTM format).
        yt_track_title = (info.get("track") or "").strip()
        yt_artist_raw = (info.get("artist") or "").strip()
        raw_title = (info.get("title") or "").strip()

        if yt_track_title:
            title = yt_track_title
        elif " - " in raw_title:
            parts = raw_title.split(" - ", 1)
            if not yt_artist_raw:
                yt_artist_raw = parts[0].strip()
            title = parts[1].strip()
        else:
            title = raw_title

        if not title:
            return None

        # ── Artists ────────────────────────────────────────────────────────
        if yt_artist_raw:
            # yt-dlp may return semicolon or comma-separated artists
            artists = [a.strip() for a in re.split(r"\s*[;]\s*", yt_artist_raw) if a.strip()]
        else:
            uploader = (info.get("uploader") or info.get("channel") or "").strip()
            uploader = _TOPIC_RE.sub("", uploader).strip()
            artists = [uploader] if uploader else []

        # ── Album ──────────────────────────────────────────────────────────
        album = (info.get("album") or "").strip()
        if not album:
            album = playlist_name or ""

        # ── Duration ───────────────────────────────────────────────────────
        duration_s = info.get("duration")
        duration_ms = int(float(duration_s) * 1000) if duration_s else None

        # ── ISRC (available for many YTM tracks) ───────────────────────────
        isrc = (info.get("isrc") or "").strip() or None

        # ── Release date ───────────────────────────────────────────────────
        release_year: Optional[int] = info.get("release_year") or None
        raw_date = (info.get("release_date") or "").strip()
        release_date: Optional[str] = None
        if raw_date:
            if len(raw_date) == 8 and raw_date.isdigit():
                # yt-dlp YYYYMMDD format
                release_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
            else:
                release_date = raw_date
            if not release_year:
                try:
                    release_year = int(release_date[:4])
                except Exception:
                    pass
        elif info.get("upload_date"):
            # upload_date is also YYYYMMDD; use year only as a rough fallback
            upload = (info.get("upload_date") or "")
            if len(upload) >= 4:
                try:
                    release_year = release_year or int(upload[:4])
                except Exception:
                    pass

        # ── Track number ───────────────────────────────────────────────────
        track_number = info.get("track_number") or None

        # ── Artwork ────────────────────────────────────────────────────────
        artwork = self._best_thumbnail(info) or playlist_artwork

        return TrackMetadata(
            title=title,
            artists=artists,
            album=album or "Unknown Album",
            source_service="youtube",
            playlist_name=playlist_name,
            playlist_artwork_url=playlist_artwork,
            release_year=release_year,
            release_date=release_date,
            track_number=track_number,
            duration_ms=duration_ms,
            isrc=isrc,
            artwork_url=artwork,
        )

    @staticmethod
    def _best_thumbnail(info: dict) -> Optional[str]:
        """Return the highest-resolution thumbnail URL from a yt-dlp info dict."""
        thumbnails = info.get("thumbnails") or []
        for thumb in reversed(thumbnails):
            url = (thumb.get("url") or "").strip()
            if url and url.startswith("http"):
                return url
        fallback = (info.get("thumbnail") or "").strip()
        return fallback or None
