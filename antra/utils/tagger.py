"""
File tagger using mutagen.
Supports MP3 (ID3) and FLAC (VorbisComment).
Embeds: title, artists, album, year, track number, genre, artwork, lyrics.
"""
import logging
import os
from io import BytesIO
from typing import Optional

import requests
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    ID3,
    TIT2,  # Title
    TPE1,  # Artist
    TPE2,  # Album artist
    TALB,  # Album
    TDRC,  # Year
    TRCK,  # Track number
    TPOS,  # Disc number
    TCON,  # Genre
    APIC,  # Artwork
    USLT,  # Unsynced lyrics
    SYLT,  # Synced lyrics
    TSRC,  # ISRC
    TXXX,  # Custom text
    Encoding,
)
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.mp3 import MP3
from antra.utils.lyrics import validate_and_strip_lrc, lrc_to_sylt_frames
try:
    from PIL import Image
except ImportError:
    Image = None

from antra.core.models import TrackMetadata

logger = logging.getLogger(__name__)


def _sniff_image_mime(data: bytes, response_mime: Optional[str]) -> str:
    mime = (response_mime or "").split(";")[0].strip().lower()
    if mime in {"image/jpeg", "image/png"}:
        return mime

    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"

    return "image/jpeg"


class FileTagger:
    def __init__(self):
        # Cache artwork by URL so albums only hit the CDN once regardless of
        # how many tracks share the same artwork_url.
        self._artwork_cache: dict[str, Optional[tuple[bytes, str]]] = {}

    def tag(
        self,
        file_path: str,
        track: TrackMetadata,
    ) -> bool:
        """Tag file at file_path with all available metadata. Returns True on success."""
        # MusicBrainz fallback enrichment
        from antra.core.musicbrainz_fetcher import enrich_metadata
        if not track.genres and track.isrc:
            logger.debug(f"[Tagger] Genres missing for {track.title}, querying MusicBrainz...")
            enrich_metadata(track)

        ext = os.path.splitext(file_path)[1].lower()
        try:
            if ext == ".flac":
                self._tag_flac(file_path, track)
            elif ext == ".mp3":
                self._tag_mp3(file_path, track)
            elif ext in {".m4a", ".mp4"}:
                self._tag_mp4(file_path, track)
            else:
                logger.warning(f"Unsupported format for tagging: {ext}")
                self._write_lyrics_sidecars(file_path, track)
                return False
            
            self.embed_lyrics(
                file_path,
                track.lyrics or "",
                track.synced_lyrics or "",
                track.duration_ms or 0
            )
            self._write_lyrics_sidecars(file_path, track)
            logger.debug(f"Tagged: {file_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to tag {file_path}: {e}")
            return False

    # ── FLAC ──────────────────────────────────────────────────────────────

    def _tag_flac(self, path: str, track: TrackMetadata):
        audio = FLAC(path)

        audio["title"] = track.title
        audio["artist"] = track.artists
        album_artist_str = ", ".join(track.album_artists) if track.album_artists else track.primary_artist
        audio["albumartist"] = [album_artist_str]
        audio["album"] = track.album
        date_str = track.release_date or (str(track.release_year) if track.release_year else None)
        if date_str:
            audio["date"] = date_str
            # Also write YEAR (4-digit) — Windows Media Player reads YEAR, not DATE
            year_str = date_str[:4] if len(date_str) >= 4 and date_str[:4].isdigit() else None
            if year_str:
                audio["year"] = year_str
        if track.track_number:
            tn = str(track.track_number)
            if track.total_tracks:
                tn += f"/{track.total_tracks}"
            audio["tracknumber"] = tn
        if track.disc_number:
            audio["discnumber"] = str(track.disc_number)
        if track.genres:
            audio["genre"] = track.genres
        if track.isrc:
            audio["isrc"] = track.isrc
        if track.upc:
            audio["barcode"] = track.upc
        if track.iswc:
            audio["iswc"] = track.iswc
        if track.audio_traits:
            audio["audio_traits"] = track.audio_traits
        if track.spotify_id:
            audio["spotify_id"] = track.spotify_id

        artwork = self._fetch_artwork(track.artwork_url)
        if artwork:
            artwork_data, mime = artwork
            audio.clear_pictures()
            pic = Picture()
            pic.type = 3  # Cover (front)
            pic.mime = mime
            pic.desc = "Cover"
            pic.data = artwork_data
            audio.add_picture(pic)

        audio.save()

    # ── MP3 ───────────────────────────────────────────────────────────────

    def _tag_mp3(self, path: str, track: TrackMetadata):
        try:
            audio = ID3(path)
        except Exception:
            audio = ID3()

        audio.add(TIT2(encoding=3, text=track.title))
        audio.add(TPE1(encoding=3, text=track.artist_string))
        album_artist_str = ", ".join(track.album_artists) if track.album_artists else track.primary_artist
        audio.add(TPE2(encoding=3, text=album_artist_str))
        audio.add(TALB(encoding=3, text=track.album))

        date_str = track.release_date or (str(track.release_year) if track.release_year else None)
        if date_str:
            audio.add(TDRC(encoding=3, text=date_str))
        if track.track_number:
            tn = str(track.track_number)
            if track.total_tracks:
                tn += f"/{track.total_tracks}"
            audio.add(TRCK(encoding=3, text=tn))
        if track.disc_number:
            audio.add(TPOS(encoding=3, text=str(track.disc_number)))
        if track.genres:
            audio.add(TCON(encoding=3, text=", ".join(track.genres)))
        if track.isrc:
            audio.add(TSRC(encoding=3, text=track.isrc))
        if track.upc:
            audio.add(TXXX(encoding=3, desc="BARCODE", text=track.upc))
        if track.iswc:
            audio.add(TXXX(encoding=3, desc="ISWC", text=track.iswc))
        if track.audio_traits:
            audio.add(TXXX(encoding=3, desc="AUDIO_TRAITS", text=", ".join(track.audio_traits)))
        if track.spotify_id:
            audio.add(TXXX(encoding=3, desc="SPOTIFYID", text=track.spotify_id))

        # Artwork
        artwork = self._fetch_artwork(track.artwork_url)
        if artwork:
            artwork_data, mime = artwork
            audio.delall("APIC")
            audio.add(APIC(
                encoding=3,
                mime=mime,
                type=3,
                desc="Cover",
                data=artwork_data,
            ))

        # Save as ID3v2.3 instead of mutagen's native v2.4 so Windows Explorer
        # and standard mobile players can reliably read the Cover Art APIC frame.
        audio.save(path, v1=2, v2_version=3)

    # ── MP4 / M4A ────────────────────────────────────────────────────────────

    def _tag_mp4(self, path: str, track: TrackMetadata):
        audio = MP4(path)

        audio["\xa9nam"] = [track.title]
        audio["\xa9ART"] = track.artists
        album_artist_str = ", ".join(track.album_artists) if track.album_artists else track.primary_artist
        audio["aART"] = [album_artist_str]
        audio["\xa9alb"] = [track.album]

        date_str = track.release_date or (str(track.release_year) if track.release_year else None)
        if date_str:
            audio["\xa9day"] = [date_str]
        if track.track_number:
            audio["trkn"] = [(track.track_number, track.total_tracks or 0)]
        if track.disc_number:
            audio["disk"] = [(track.disc_number, 0)]
        if track.genres:
            audio["\xa9gen"] = track.genres
        if track.spotify_id:
            audio["----:com.apple.iTunes:SPOTIFYID"] = [MP4FreeForm(track.spotify_id.encode("utf-8"))]
        if track.isrc:
            audio["----:com.apple.iTunes:ISRC"] = [MP4FreeForm(track.isrc.encode("utf-8"))]

        artwork = self._fetch_artwork(track.artwork_url)
        if artwork:
            artwork_data, mime = artwork
            image_format = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(artwork_data, imageformat=image_format)]

        audio.save()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _fetch_artwork(self, url: Optional[str]) -> Optional[tuple[bytes, str]]:
        if not url:
            return None
        if url in self._artwork_cache:
            return self._artwork_cache[url]

        result = None
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                result = FileTagger._normalize_artwork(resp.content, resp.headers.get("Content-Type"))
                break
            except Exception as e:
                if attempt < 2:
                    import time
                    time.sleep(1.5 ** attempt)
                else:
                    logger.warning(f"Failed to download artwork from {url}: {e}")

        self._artwork_cache[url] = result
        return result

    @staticmethod
    def _normalize_artwork(data: bytes, response_mime: Optional[str]) -> tuple[bytes, str]:
        mime = _sniff_image_mime(data, response_mime)
        if mime in {"image/jpeg", "image/png"}:
            return data, mime

        if Image is None:
            return data, mime

        image = Image.open(BytesIO(data))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")

        out = BytesIO()
        image.save(out, format="JPEG", quality=92)
        return out.getvalue(), "image/jpeg"

    @staticmethod
    def _write_lyrics_sidecars(file_path: str, track: TrackMetadata):
        """
        Writes .lrc and .txt sidecar files for external players.

        Skipped for formats that already carry lyrics in their tags:
          - FLAC  → SYNCEDLYRICS / LYRICS VorbisComment tags
          - MP3   → SYLT (synced) / USLT (plain) ID3 frames
          - M4A   → ©lyr atom
        For any other extension (e.g. .ogg, .opus) sidecars are still written.
        """
        ext = os.path.splitext(file_path)[1].lower()

        # These formats have embedded lyrics written by embed_lyrics(); no sidecar needed.
        _EMBEDDED_FORMATS = {".flac", ".mp3", ".m4a", ".mp4", ".aac"}

        if ext in _EMBEDDED_FORMATS:
            return

        base, _ = os.path.splitext(file_path)

        if track.synced_lyrics:
            try:
                with open(base + ".lrc", "w", encoding="utf-8") as handle:
                    handle.write(track.synced_lyrics)
            except Exception as e:
                logger.warning(f"Failed to write synced lyrics sidecar for {file_path}: {e}")

        if track.lyrics:
            try:
                with open(base + ".txt", "w", encoding="utf-8") as handle:
                    handle.write(track.lyrics)
            except Exception as e:
                logger.warning(f"Failed to write plain lyrics sidecar for {file_path}: {e}")

    def embed_lyrics(
        self,
        path: str,
        lyrics: str = "",
        synced_lyrics: str = "",
        duration_ms: int = 0,
    ) -> None:
        """
        Embed lyrics into audio file metadata.
        Prioritizes synced (LRC) lyrics over plain text.
        Validates LRC timestamps against duration before embedding.
        """
        # Validate and clean LRC if we have duration info
        if synced_lyrics and duration_ms:
            synced_lyrics = validate_and_strip_lrc(synced_lyrics, duration_ms)

        # Best available lyric string for formats that don't support synced
        best_plain = synced_lyrics or lyrics  # LRC is readable as plain text too

        ext = os.path.splitext(path)[1].lower()

        try:
            if ext == ".flac":
                audio = FLAC(path)
                if synced_lyrics:
                    audio["SYNCEDLYRICS"] = [synced_lyrics]
                if lyrics:
                    audio["LYRICS"] = [lyrics]
                elif synced_lyrics:
                    audio["LYRICS"] = [synced_lyrics]
                audio.save()

            elif ext == ".mp3":
                try:
                    audio = ID3(path)
                except Exception:
                    audio = ID3()
                # Plain/unsynced lyrics
                best = lyrics or synced_lyrics
                if best:
                    audio.add(USLT(
                        encoding=Encoding.UTF8,
                        lang="eng",
                        desc="",
                        text=best,
                    ))
                # Synced lyrics via SYLT
                if synced_lyrics:
                    frames = lrc_to_sylt_frames(synced_lyrics)
                    if frames:
                        audio.add(SYLT(
                            encoding=Encoding.UTF8,
                            lang="eng",
                            format=2,   # milliseconds
                            type=1,     # lyrics
                            desc="",
                            text=frames,
                        ))
                audio.save(path, v1=2, v2_version=3)

            elif ext in (".m4a", ".mp4", ".aac"):
                audio = MP4(path)
                # M4A only supports plain text lyrics via \xa9lyr atom
                # Use LRC as plain text if no plain lyrics available
                if best_plain:
                    audio["\xa9lyr"] = [best_plain]
                audio.save()

            else:
                from logging import getLogger
                getLogger(__name__).debug(f"[Tagger] Unsupported format for lyric embedding: {ext}")

        except Exception as e:
            from logging import getLogger
            getLogger(__name__).warning(f"[Tagger] Failed to embed lyrics into {path}: {e}")
