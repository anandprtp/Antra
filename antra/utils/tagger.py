"""
File tagger using mutagen.
Supports MP3 (ID3) and FLAC (VorbisComment).
Embeds: title, artists, album, year, track number, genre, artwork, lyrics.
"""
import logging
import os
import re
import threading
from dataclasses import dataclass
from io import BytesIO
from typing import Iterable, Optional

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
    TCOM,  # Composer
    TPUB,  # Publisher/Label
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

from antra.utils.fsutil import write_atomic
from antra.utils.longpath import extended_path
from antra.utils.matching import normalize, string_similarity
from antra_shared.filename_prefs import (
    build_context,
    migrate_legacy_templates,
    render_template,
    transform_text,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# v2.0.0 DMX-1 — per-tag write toggles.
#
# TAG_FIELDS is the complete set of tags a user may switch OFF. It is
# deliberately NOT an exhaustive 28-switch list. Toggles for `bpm`,
# `replayGain`, `copyright`, `involvedPeople`, `length` and `source` are all
# omitted: this tagger writes none of them, and a switch for a tag we never write is
# dead UI. Every id below maps to a real write in _tag_flac / _tag_mp3 /
# _tag_mp4 / embed_lyrics, and the test suite asserts that mapping by tagging
# real files and reading the tags back.
#
# The setting is stored as the list of DISABLED ids, not twenty booleans. That
# is what makes an absent key mean "everything on": a config.json written
# before DMX-1 has no `disabled_tags`, so it reads as the empty set and
# behaviour is byte-identical to today. Twenty plain bools would each read
# False from such a file and silently strip every tag — the *bool trap this
# project has already paid for on NotifyVolume, PreferExplicit and the FR-2
# ints, arriving twenty times at once.
TAG_FIELDS: tuple[str, ...] = (
    "title", "artist", "album", "album_artist",
    "date", "year", "track_number", "track_total", "disc_number",
    "genre", "composer", "label",
    "isrc", "barcode", "iswc",
    "cover", "lyrics", "synced_lyrics",
    "spotify_id", "audio_traits", "advisory",
)

# v2.0.0 ADV-1 (issue #43) — the iTunes content-advisory atom.
#
# MP4 `rtng` is the atom MP3Tag and friends surface under the name
# ITUNESADVISORY, which is how the request was worded. Apple's values are
# 0 = none, 1 = explicit, 2 = clean.
#
# Only 1 and 2 are ever written. An unknown explicitness writes NO atom at
# all, because 0 asserts "rated, and there is nothing to warn about" — a
# different claim from "no source told us". BUG-EXPL-1 is the cautionary
# tale directly upstream of this feature: an Optional[bool] whose None was
# silently read as a negative is exactly how `prefer_explicit` stayed dead
# for an entire release.
#
# MP4 only, deliberately. FLAC and MP3 have no standardised advisory field —
# anything written there is one tagger's private convention, and the request
# was scoped to ALAC and AAC.
ADVISORY_EXPLICIT = 1
ADVISORY_CLEAN = 2

# Date renderings for the `date` tag. ISO is the default and must stay the
# default: v1.1.3 established that Windows Media Player reads a full ISO date
# string and shows nothing useful for a bare year, so the other three are
# opt-in and the UI says so.
DATE_FORMATS: tuple[str, ...] = ("iso", "year", "dmy", "mdy")

_FULL_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def render_date(raw_date: Optional[str], year: Optional[int], fmt: str) -> Optional[str]:
    """Render the date tag in the user's chosen format.

    Reformatting needs a full Y-M-D; a release that only carries a year cannot
    become `D/M/Y`, so it degrades to what it has rather than inventing a day.
    An unrecognised format falls back to ISO — never to nothing.
    """
    text = (raw_date or "").strip() or (str(year) if year else "")
    if not text:
        return None
    if fmt == "year":
        return text[:4] if text[:4].isdigit() else text
    match = _FULL_DATE_RE.match(text)
    if not match or fmt not in ("dmy", "mdy"):
        return text
    y, m, d = match.groups()
    return "%s/%s/%s" % (d, m, y) if fmt == "dmy" else "%s/%s/%s" % (m, d, y)


def _sniff_image_mime(data: bytes, response_mime: Optional[str]) -> str:
    mime = (response_mime or "").split(";")[0].strip().lower()
    if mime in {"image/jpeg", "image/png"}:
        return mime

    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"

    return "image/jpeg"


DEFAULT_COVER_TEMPLATE = "cover"
DEFAULT_ARTIST_IMAGE_TEMPLATE = "folder"
DEFAULT_JPEG_QUALITY = 90
DEFAULT_EMBEDDED_ART_EDGE = 1000


@dataclass
class ArtworkPreferences:
    """v2.0.0 FR-2 — user control over cover art, embedded and on disk.

    Defaults reproduce the pre-FR-2 behaviour exactly for the embedded path
    (1000 px, quality 90 — the v1.1.6 BUG-F cap) and IMPROVE the sidecar path:
    local_artwork_size defaults to 0 (original size), which enables the
    byte-for-byte fast path. Before FR-2 every sidecar was re-encoded at JPEG
    q95, measured across four real Apple 3000 px covers as 0.8-16.9% LARGER
    than the original it replaced (mean +9.5%) and never byte-identical.
    """

    cover_image_template: str = DEFAULT_COVER_TEMPLATE
    local_artwork_size: int = 0
    local_artwork_format: str = "jpg"          # jpg | png | both
    jpeg_image_quality: int = DEFAULT_JPEG_QUALITY
    embedded_artwork_size: int = DEFAULT_EMBEDDED_ART_EDGE
    embedded_artwork_png: bool = False
    save_artist_image: bool = False
    artist_image_template: str = DEFAULT_ARTIST_IMAGE_TEMPLATE

    def __post_init__(self):
        self.cover_image_template = (self.cover_image_template or "").strip() or DEFAULT_COVER_TEMPLATE
        self.artist_image_template = (self.artist_image_template or "").strip() or DEFAULT_ARTIST_IMAGE_TEMPLATE
        fmt = (self.local_artwork_format or "").strip().lower()
        self.local_artwork_format = fmt if fmt in {"jpg", "png", "both"} else "jpg"
        self.local_artwork_size = max(0, int(self.local_artwork_size or 0))
        self.embedded_artwork_size = max(0, int(self.embedded_artwork_size or 0))
        self.jpeg_image_quality = min(100, max(50, int(self.jpeg_image_quality or DEFAULT_JPEG_QUALITY)))
        self.embedded_artwork_png = bool(self.embedded_artwork_png)
        self.save_artist_image = bool(self.save_artist_image)


# Containers tag() can write. Any other extension makes tag() return False BY
# DESIGN (the file is intact, it just cannot hold tags).
TAGGABLE_EXTENSIONS = frozenset({".flac", ".mp3", ".m4a", ".mp4"})


def tag_problem_for(file_path: str, tag_ok: bool) -> Optional[str]:
    """Why tag() returned False: "unsupported" (container cannot hold tags) or
    "failed" (a taggable container whose writer raised). None when it succeeded."""
    if tag_ok:
        return None
    ext = os.path.splitext(file_path or "")[1].lower()
    return "failed" if ext in TAGGABLE_EXTENSIONS else "unsupported"


class FileTagger:
    _MAX_EMBEDDED_ART_EDGE = 1000

    def __init__(
        self,
        write_antra_tags: bool = True,
        artwork_prefs: Optional[ArtworkPreferences] = None,
        filename_preferences: Optional[dict] = None,
        disabled_tags: Optional[Iterable[str]] = None,
        date_format: str = "iso",
        write_id3v1: bool = True,
    ):
        # Cache artwork by URL so albums only hit the CDN once regardless of
        # how many tracks share the same artwork_url.
        self._artwork_cache: dict[str, Optional[tuple[bytes, str, int, int, int]]] = {}
        self._raw_artwork_cache: dict[str, Optional[list[tuple[bytes, str]]]] = {}
        self._artist_image_url_cache: dict[str, Optional[str]] = {}
        self._artist_image_cache: dict[str, Optional[list[tuple[bytes, str]]]] = {}
        self._sidecar_lock = threading.Lock()
        # v1.1.8 FEAT-4 — record which Antra version/source produced the file.
        self.write_antra_tags = write_antra_tags
        # v2.0.0 FR-2 — cover art naming/size/format/quality.
        self.artwork = artwork_prefs or ArtworkPreferences()
        # v2.0.0 DMX-2 — migrated rather than stored raw, so every text
        # transform is clamped to a known value here instead of at each write
        # site. The organizer already passes a migrated dict; a caller that
        # builds one by hand (or an older config.json missing the keys) gets
        # the same no-op defaults either way.
        self.filename_preferences = migrate_legacy_templates(filename_preferences or {})
        # v2.0.0 DMX-1 — tags the user has switched off. Unknown ids are kept
        # rather than dropped: a config written by a newer build must not have
        # its choices silently discarded when an older build reads it.
        self.disabled_tags = frozenset(
            str(name).strip().lower() for name in (disabled_tags or ()) if str(name).strip()
        )
        self.date_format = date_format if date_format in DATE_FORMATS else "iso"
        self.write_id3v1 = write_id3v1

    def _want(self, field: str) -> bool:
        """True when this tag should be written. An unknown id is always on."""
        return field not in self.disabled_tags

    def _text(self, track: TrackMetadata):
        """v2.0.0 DMX-2 — the title / artist strings to WRITE.

        Output only. transform_text does not mutate the track, so the resolver,
        `strip_collab` and the identity gate keep seeing the catalogue's
        original strings — which is what stops BUG-1's wrong-audio class from
        coming back through a casing or feat. setting.
        """
        return transform_text(track, self.filename_preferences)

    def _date_tag(self, track: TrackMetadata, iso_only: bool = False) -> Optional[str]:
        """The rendered date tag.

        `iso_only` is for ID3: TDRC is a STRUCTURED timestamp, not free text,
        and mutagen parses whatever it is given. Measured — handing it
        "12/04/2024" produces "0012-04-2024", i.e. a silently corrupted year.
        So MP3 keeps ISO for `dmy`/`mdy` rather than writing a wrong date;
        `year` is left alone because a bare year is valid ISO 8601. FLAC's
        DATE and MP4's (c)day are free text and honour all four.
        """
        fmt = self.date_format
        if iso_only and fmt in ("dmy", "mdy"):
            fmt = "iso"
        return render_date(track.release_date, track.release_year, fmt)

    @staticmethod
    def _year_tag(track: TrackMetadata) -> Optional[str]:
        """The bare 4-digit year, independent of `date_format`.

        Kept separate because the FLAC YEAR field exists precisely so Windows
        Media Player has something to read when DATE is formatted for humans.
        """
        text = (track.release_date or "").strip() or (
            str(track.release_year) if track.release_year else ""
        )
        head = text[:4]
        return head if head.isdigit() else None

    @staticmethod
    def _advisory_value(
        track: TrackMetadata,
        delivered_explicit: Optional[bool] = None,
    ) -> Optional[int]:
        """The `rtng` value for this file, or None to write no atom at all.

        `delivered_explicit` is what the winning adapter said about the file
        it actually handed us; `track.is_explicit` is what the catalogue said
        about the recording that was ASKED for. They are not the same claim.
        When the chain falls back to a clean master because no source carries
        the explicit one, the track still reads True — stamping `1` onto that
        file would be a tag asserting something the audio does not contain, so
        the delivered flag wins whenever a source stated one.

        Most adapters leave it None (they report nothing either way), which is
        why this is a preference and not a replacement.
        """
        flag = delivered_explicit if delivered_explicit is not None else track.is_explicit
        if flag is None:
            return None
        return ADVISORY_EXPLICIT if flag else ADVISORY_CLEAN

    @property
    def _id3_v1(self) -> int:
        # mutagen: 2 = create/update an ID3v1 tag, 0 = leave the v1 area
        # untouched. 0 rather than 1 so switching this off never writes one.
        return 2 if self.write_id3v1 else 0

    @staticmethod
    def _antra_provenance(track: TrackMetadata) -> list[tuple[str, str]]:
        """Provenance key/value pairs written into every tagged file (FEAT-4).

        The version comes from `antra.__version__`, the single source of truth,
        so it cannot drift from the label shown in the UI. `ANTRA_SOURCE` is
        recorded alongside it because "which version wrote this" and "where did
        it come from" are the same question when auditing a library for folders
        that need re-fetching.
        """
        from antra import __version__ as _antra_version
        import datetime as _dt

        pairs = [
            ("ANTRA_VERSION", str(_antra_version)),
            ("ANTRA_DOWNLOADED", _dt.datetime.now(_dt.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")),
        ]
        source = (getattr(track, "source_service", None) or "").strip()
        if source:
            pairs.append(("ANTRA_SOURCE", source))
        return pairs

    def tag(
        self,
        file_path: str,
        track: TrackMetadata,
        provenance: bool = True,
        delivered_explicit: Optional[bool] = None,
    ) -> bool:
        """Tag file at file_path with all available metadata. Returns True on success.

        `delivered_explicit` (ADV-1) is the winning adapter's statement about
        the file on disk, and is preferred over the track's own flag for the
        advisory atom. It defaults to None so a re-tag — which has no adapter
        result to consult — falls back to the catalogue, and so every existing
        caller keeps working unchanged.

        provenance=False skips ANTRA_VERSION / ANTRA_DOWNLOADED / ANTRA_SOURCE.
        A re-tag (v2.0.0 DMX-5 tag_only) passes False: those tags record which
        build wrote the AUDIO, and stamping the current version onto a file an
        older build downloaded would defeat FEAT-4's whole purpose — finding
        folders written by a version with a known bug.
        """
        # MusicBrainz fallback enrichment
        from antra.core.musicbrainz_fetcher import enrich_metadata
        if not track.genres and track.isrc:
            logger.debug(f"[Tagger] Genres missing for {track.title}, querying MusicBrainz...")
            enrich_metadata(track)

        # v1.1.8 BUG-6: use the extended-length form so tagging works on paths
        # over the Windows 260-char limit instead of failing with [Errno 2]
        # and leaving an untagged file that later gets duplicated (BUG-3).
        file_path = extended_path(file_path)
        ext = os.path.splitext(file_path)[1].lower()
        try:
            if ext == ".flac":
                self._tag_flac(file_path, track)
            elif ext == ".mp3":
                self._tag_mp3(file_path, track)
            elif ext in {".m4a", ".mp4"}:
                self._tag_mp4(file_path, track, delivered_explicit)
            else:
                logger.warning(f"Unsupported format for tagging: {ext}")
                self._write_lyrics_sidecars(file_path, track)
                return False

            if provenance:
                self._write_antra_provenance(file_path, track)

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

    def _render_sidecar_stem(self, template: str, default: str, track: TrackMetadata) -> str:
        """Render a cover/artist filename from the shared filename tokens.

        render_template sanitises the result, so path separators and traversal
        segments cannot escape the folder; an empty render falls back to the
        documented default rather than the literal "Unknown".
        """
        try:
            context = build_context(track, self.filename_preferences)
            stem = render_template(template, context, self.filename_preferences)
        except Exception as e:
            logger.debug("Cover template %r failed to render (%s); using %r", template, e, default)
            return default
        stem = (stem or "").strip()
        if not stem or stem == "Unknown":
            return default
        return stem

    def save_cover_art_sidecar(
        self, file_path: str, track: TrackMetadata, folder: Optional[str] = None
    ) -> Optional[str]:
        if not track.artwork_url:
            return None

        # folder: the release folder when it is not the file's parent — with
        # per-disc CD<n>/ folders (v2.0.0 DMX-4) the cover belongs one level up,
        # where players look for it, not duplicated into every disc folder.
        folder = folder or os.path.dirname(file_path)
        if not folder:
            return None

        stem = self._render_sidecar_stem(
            self.artwork.cover_image_template, DEFAULT_COVER_TEMPLATE, track
        )

        with self._sidecar_lock:
            # An unchanged template keeps the pre-FR-2 guard exactly: if this
            # folder already has a cover under any of the names players look
            # for (including one written by another tool), do not add a second.
            # A CUSTOMISED template checks the target names instead — the whole
            # point of setting it is to get that specific filename on disk.
            if self.artwork.cover_image_template == DEFAULT_COVER_TEMPLATE:
                existing = self._existing_cover_sidecar(folder)
                if existing:
                    return existing
            else:
                for ext in (".jpg", ".jpeg", ".png"):
                    candidate = os.path.join(folder, stem + ext)
                    if os.path.exists(candidate):
                        return candidate

            return self._write_image_sidecars(
                folder, stem, self._fetch_raw_artwork(track.artwork_url), file_path
            )

    def save_artist_image_sidecar(self, artist_folder: Optional[str], track: TrackMetadata) -> Optional[str]:
        """Write an artist image into the artist folder (v2.0.0 FR-2).

        The artist folder only exists in the standard layout; callers pass None
        when the library is flat, so nothing is ever written at the library root.
        """
        if not self.artwork.save_artist_image or not artist_folder:
            return None
        if not os.path.isdir(artist_folder):
            return None

        artist = ""
        if getattr(track, "album_artists", None):
            artist = track.album_artists[0]
        elif getattr(track, "artists", None):
            artist = track.artists[0]
        artist = (artist or "").strip()
        if not artist:
            return None

        stem = self._render_sidecar_stem(
            self.artwork.artist_image_template, DEFAULT_ARTIST_IMAGE_TEMPLATE, track
        )

        with self._sidecar_lock:
            for ext in (".jpg", ".jpeg", ".png"):
                candidate = os.path.join(artist_folder, stem + ext)
                if os.path.exists(candidate):
                    return candidate

            url = self._resolve_artist_image_url(artist)
            if not url:
                return None
            return self._write_image_sidecars(
                artist_folder, stem, self._fetch_artist_image(url), artist_folder
            )

    def _write_image_sidecars(
        self,
        folder: str,
        stem: str,
        blobs: Optional[list],
        context_path: str,
    ) -> Optional[str]:
        if not blobs:
            return None
        written = None
        for data, mime in blobs:
            ext = ".png" if mime == "image/png" else ".jpg"
            out_path = os.path.join(folder, stem + ext)
            try:
                write_atomic(out_path, data)
                logger.debug("Saved image sidecar: %s", out_path)
                written = written or out_path
            except Exception as e:
                logger.warning("Failed to save image sidecar for %s: %s", context_path, e)
        return written

    def _resolve_artist_image_url(self, artist: str) -> Optional[str]:
        """Deezer free artist search — no auth, and it covers regional catalogues.

        The name is VERIFIED before the picture is accepted. An unverified match
        would embed the wrong artist's photo, which is the BUG-1 failure mode in
        a new place, so a weak hit is dropped rather than guessed at.
        """
        key = artist.lower()
        if key in self._artist_image_url_cache:
            return self._artist_image_url_cache[key]

        url = None
        try:
            resp = requests.get(
                "https://api.deezer.com/search/artist",
                params={"q": artist, "limit": 3},
                timeout=8,
            )
            resp.raise_for_status()
            for hit in (resp.json().get("data") or []):
                name = str(hit.get("name") or "")
                if not name:
                    continue
                if string_similarity(normalize(artist), normalize(name)) < 0.85:
                    continue
                picture = hit.get("picture_xl") or hit.get("picture_big") or ""
                if picture:
                    url = picture
                    break
        except Exception as e:
            logger.debug("Artist image lookup failed for %r: %s", artist, e)

        self._artist_image_url_cache[key] = url
        return url

    def _fetch_artist_image(self, url: str) -> Optional[list]:
        if url in self._artist_image_cache:
            return self._artist_image_cache[url]
        result = None
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            result = self._prepare_sidecar_artworks(resp.content, resp.headers.get("Content-Type"))
        except Exception as e:
            logger.warning("Failed to download artist image from %s: %s", url, e)
        self._artist_image_cache[url] = result
        return result

    # ── FLAC ──────────────────────────────────────────────────────────────

    def _write_antra_provenance(self, file_path: str, track: TrackMetadata) -> None:
        """Write ANTRA_VERSION / ANTRA_DOWNLOADED / ANTRA_SOURCE (v1.1.8 FEAT-4).

        Written as a separate pass (the same pattern `embed_lyrics` already uses)
        so the three per-container tagging methods stay untouched. Failures are
        swallowed: provenance is a convenience for library auditing and must
        never be able to fail an otherwise good download.
        """
        if not self.write_antra_tags:
            return
        pairs = self._antra_provenance(track)
        ext = os.path.splitext(file_path)[1].lower()
        try:
            if ext == ".flac":
                from mutagen.flac import FLAC
                audio = FLAC(file_path)
                for key, value in pairs:
                    audio[key.lower()] = value
                audio.save()
            elif ext == ".mp3":
                from mutagen.id3 import ID3, TXXX
                try:
                    audio = ID3(file_path)
                except Exception:
                    audio = ID3()
                for key, value in pairs:
                    audio.add(TXXX(encoding=3, desc=key, text=value))
                audio.save(file_path)
            elif ext in {".m4a", ".mp4"}:
                from mutagen.mp4 import MP4
                audio = MP4(file_path)
                for key, value in pairs:
                    # Freeform atoms must be bytes and namespaced.
                    audio[f"----:com.antra:{key}"] = [value.encode("utf-8")]
                audio.save()
        except Exception as e:
            logger.debug(f"[Tagger] Could not write Antra provenance tags to {file_path}: {e}")

    def _tag_flac(self, path: str, track: TrackMetadata):
        audio = FLAC(path)

        # v2.0.0 DMX-1 — every write below is gated by self._want(<field>).
        # The field ids are the ones in TAG_FIELDS; anything not switched off
        # behaves exactly as it did before DMX-1.
        tx = self._text(track)
        if tx.title and self._want("title"):
            audio["title"] = tx.title
        if tx.artist_values and self._want("artist"):
            # artist_values is the MULTI-VALUE form: with multi_artist_separator
            # on "default" this is the artist list exactly as before, which is
            # the correct Vorbis representation. A chosen separator collapses
            # it to one joined string.
            audio["artist"] = tx.artist_values
        if tx.album_artist and self._want("album_artist"):
            audio["albumartist"] = [tx.album_artist]
        if track.album and self._want("album"):
            audio["album"] = track.album

        date_str = self._date_tag(track)
        if date_str and self._want("date"):
            audio["date"] = date_str
        # YEAR is written from the raw date, never from the formatted one:
        # Windows Media Player reads YEAR, and it must stay 4 digits whatever
        # `date_format` renders DATE as.
        year_str = self._year_tag(track)
        if year_str and self._want("year"):
            audio["year"] = year_str

        if track.track_number and self._want("track_number"):
            tn = str(track.track_number)
            if track.total_tracks and self._want("track_total"):
                tn += f"/{track.total_tracks}"
            audio["tracknumber"] = tn
        if track.disc_number and self._want("disc_number"):
            audio["discnumber"] = str(track.disc_number)
        if track.genres and self._want("genre"):
            audio["genre"] = track.genres
        if track.isrc and self._want("isrc"):
            audio["isrc"] = track.isrc
        if track.upc and self._want("barcode"):
            audio["barcode"] = track.upc
        if track.iswc and self._want("iswc"):
            audio["iswc"] = track.iswc
        if track.audio_traits and self._want("audio_traits"):
            audio["audio_traits"] = track.audio_traits
        if track.spotify_id and self._want("spotify_id"):
            audio["spotify_id"] = track.spotify_id
        if track.composer and self._want("composer"):
            audio["composer"] = [track.composer]
        lbl = getattr(track, "label", None)
        if lbl and self._want("label"):
            audio["organization"] = [lbl]

        if self._want("cover"):
            artwork = self._fetch_artwork(track.artwork_url)
            if artwork:
                artwork_data, mime, width, height, depth = artwork
                audio.clear_pictures()
                pic = Picture()
                pic.type = 3  # Cover (front)
                pic.mime = mime
                pic.desc = "Cover"
                pic.data = artwork_data
                pic.width = width
                pic.height = height
                pic.depth = depth
                pic.colors = 0
                audio.add_picture(pic)

        audio.save()

    # ── MP3 ───────────────────────────────────────────────────────────────

    def _tag_mp3(self, path: str, track: TrackMetadata):
        try:
            audio = ID3(path)
        except Exception:
            audio = ID3()

        tx = self._text(track)
        if tx.title and self._want("title"):
            audio.add(TIT2(encoding=3, text=tx.title))
        if tx.artist_joined and self._want("artist"):
            # ID3v2.3 has no clean multi-value text, so this is always a single
            # joined string — ", " by default, which is what track.artist_string
            # produced before DMX-2.
            audio.add(TPE1(encoding=3, text=tx.artist_joined))
        if tx.album_artist and self._want("album_artist"):
            audio.add(TPE2(encoding=3, text=tx.album_artist))
        if track.album and self._want("album"):
            audio.add(TALB(encoding=3, text=track.album))

        date_str = self._date_tag(track, iso_only=True)
        if date_str and self._want("date"):
            audio.add(TDRC(encoding=3, text=date_str))
        if track.track_number and self._want("track_number"):
            tn = str(track.track_number)
            if track.total_tracks and self._want("track_total"):
                tn += f"/{track.total_tracks}"
            audio.add(TRCK(encoding=3, text=tn))
        if track.disc_number and self._want("disc_number"):
            audio.add(TPOS(encoding=3, text=str(track.disc_number)))
        if track.genres and self._want("genre"):
            audio.add(TCON(encoding=3, text=", ".join(track.genres)))
        if track.isrc and self._want("isrc"):
            audio.add(TSRC(encoding=3, text=track.isrc))
        if track.upc and self._want("barcode"):
            audio.add(TXXX(encoding=3, desc="BARCODE", text=track.upc))
        if track.iswc and self._want("iswc"):
            audio.add(TXXX(encoding=3, desc="ISWC", text=track.iswc))
        if track.audio_traits and self._want("audio_traits"):
            audio.add(TXXX(encoding=3, desc="AUDIO_TRAITS", text=", ".join(track.audio_traits)))
        if track.spotify_id and self._want("spotify_id"):
            audio.add(TXXX(encoding=3, desc="SPOTIFYID", text=track.spotify_id))
        if track.composer and self._want("composer"):
            audio.add(TCOM(encoding=3, text=track.composer))
        lbl = getattr(track, "label", None)
        if lbl and self._want("label"):
            audio.add(TPUB(encoding=3, text=lbl))

        # Artwork
        if self._want("cover"):
            artwork = self._fetch_artwork(track.artwork_url)
            if artwork:
                artwork_data, mime, _width, _height, _depth = artwork
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
        # v1 is DMX-1's `write_id3v1`: 2 creates/updates the legacy tag some old
        # hardware still needs, 0 leaves the file's v1 area alone entirely.
        audio.save(path, v1=self._id3_v1, v2_version=3)

    # ── MP4 / M4A ────────────────────────────────────────────────────────────

    def _tag_mp4(
        self,
        path: str,
        track: TrackMetadata,
        delivered_explicit: Optional[bool] = None,
    ):
        audio = MP4(path)

        tx = self._text(track)
        if tx.title and self._want("title"):
            audio["\xa9nam"] = [tx.title]
        if tx.artist_values and self._want("artist"):
            audio["\xa9ART"] = tx.artist_values
        if tx.album_artist and self._want("album_artist"):
            audio["aART"] = [tx.album_artist]
        if track.album and self._want("album"):
            audio["\xa9alb"] = [track.album]

        date_str = self._date_tag(track)
        if date_str and self._want("date"):
            audio["\xa9day"] = [date_str]
        if track.track_number and self._want("track_number"):
            total = track.total_tracks or 0
            if not self._want("track_total"):
                total = 0
            audio["trkn"] = [(track.track_number, total)]
        if track.disc_number and self._want("disc_number"):
            audio["disk"] = [(track.disc_number, 0)]
        if track.genres and self._want("genre"):
            audio["\xa9gen"] = track.genres
        if track.spotify_id and self._want("spotify_id"):
            audio["----:com.apple.iTunes:SPOTIFYID"] = [MP4FreeForm(track.spotify_id.encode("utf-8"))]
        if track.isrc and self._want("isrc"):
            audio["----:com.apple.iTunes:ISRC"] = [MP4FreeForm(track.isrc.encode("utf-8"))]
        if self._want("advisory"):
            # v2.0.0 ADV-1 (issue #43) — ITUNESADVISORY. None means no source
            # stated it, and no atom is written; see ADVISORY_EXPLICIT above.
            advisory = self._advisory_value(track, delivered_explicit)
            if advisory is not None:
                audio["rtng"] = [advisory]
        if track.composer and self._want("composer"):
            audio["\xa9wrt"] = [track.composer]
        lbl = getattr(track, "label", None)
        if lbl and self._want("label"):
            audio["----:com.apple.iTunes:LABEL"] = [MP4FreeForm(lbl.encode("utf-8"))]

        if self._want("cover"):
            artwork = self._fetch_artwork(track.artwork_url)
            if artwork:
                artwork_data, mime, _width, _height, _depth = artwork
                image_format = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
                audio["covr"] = [MP4Cover(artwork_data, imageformat=image_format)]

        audio.save()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _fetch_artwork(self, url: Optional[str]) -> Optional[tuple[bytes, str, int, int, int]]:
        """Artwork prepared for EMBEDDING (one copy per track)."""
        if not url:
            return None
        if url in self._artwork_cache:
            return self._artwork_cache[url]

        result: Optional[tuple[bytes, str, int, int, int]] = None
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                result = self._transform_artwork(
                    resp.content,
                    resp.headers.get("Content-Type"),
                    max_edge=self.artwork.embedded_artwork_size,
                    quality=self.artwork.jpeg_image_quality,
                    want_png=self.artwork.embedded_artwork_png,
                )
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
    def _existing_cover_sidecar(folder: str) -> Optional[str]:
        for name in ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png"):
            path = os.path.join(folder, name)
            if os.path.exists(path):
                return path
        return None

    def _fetch_raw_artwork(self, url: Optional[str]) -> Optional[list[tuple[bytes, str]]]:
        """Artwork prepared for the SIDECAR (one file per folder).

        Returns one entry per requested output format, so local_artwork_format
        "both" writes a .jpg and a .png from a single download.
        """
        if not url:
            return None
        if url in self._raw_artwork_cache:
            return self._raw_artwork_cache[url]

        result: Optional[list[tuple[bytes, str]]] = None
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                result = self._prepare_sidecar_artworks(resp.content, resp.headers.get("Content-Type"))
                break
            except Exception as e:
                if attempt < 2:
                    import time
                    time.sleep(1.5 ** attempt)
                else:
                    logger.warning(f"Failed to download sidecar artwork from {url}: {e}")

        self._raw_artwork_cache[url] = result
        return result

    def _prepare_sidecar_artworks(self, data: bytes, response_mime: Optional[str]) -> list[tuple[bytes, str]]:
        fmt = self.artwork.local_artwork_format
        wants = [True] if fmt == "png" else ([False] if fmt == "jpg" else [False, True])
        out: list[tuple[bytes, str]] = []
        for want_png in wants:
            blob, mime, _w, _h, _d = self._transform_artwork(
                data,
                response_mime,
                max_edge=self.artwork.local_artwork_size,
                quality=self.artwork.jpeg_image_quality,
                want_png=want_png,
            )
            out.append((blob, mime))
        return out

    def _transform_artwork(
        self,
        data: bytes,
        response_mime: Optional[str],
        *,
        max_edge: int,
        quality: int,
        want_png: bool,
    ) -> tuple[bytes, str, int, int, int]:
        """Resize/recompress artwork only when a preference actually asks for it.

        Fast path (v2.0.0 FR-2): when the source is already in the requested
        container, is within the size cap, and the quality slider is untouched,
        the ORIGINAL BYTES are returned unchanged — no pixel decode, no
        re-encode. Pillow still opens the header to read the real dimensions,
        which costs ~0.02 ms against ~17 ms for a full decode (measured, 947x)
        and is what lets the fast path keep the FLAC Picture width/height/depth
        the v1.1.6 BUG-F fix added for Windows Media Player.
        """
        mime = _sniff_image_mime(data, response_mime)
        target_mime = "image/png" if want_png else "image/jpeg"

        if Image is None:
            # No Pillow: behave exactly as before — embed what was downloaded.
            return data, mime, 0, 0, 0

        try:
            probe = Image.open(BytesIO(data))
            width, height = probe.size
        except Exception as e:
            logger.warning("Artwork header unreadable (%s); using it as-is", e)
            return data, mime, 0, 0, 0

        oversize = bool(max_edge) and max(width, height) > max_edge
        recompress = quality != DEFAULT_JPEG_QUALITY
        container_ok = mime == target_mime

        if container_ok and not oversize and not (recompress and not want_png):
            depth = 32 if mime == "image/png" else 24
            return data, mime, width, height, depth

        try:
            image = Image.open(BytesIO(data))
            image.load()

            if oversize:
                resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
                image.thumbnail((max_edge, max_edge), resampling)
                width, height = image.size

            out = BytesIO()
            if want_png:
                if image.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                    image = image.convert("RGB")
                image.save(out, format="PNG", optimize=True)
                depth = 32 if image.mode in ("RGBA", "LA") else 24
                return out.getvalue(), "image/png", width, height, depth

            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            image.save(out, format="JPEG", quality=quality, optimize=True)
            return out.getvalue(), "image/jpeg", width, height, 24
        except Exception as e:
            # Artwork must never break a download — keep the original.
            logger.warning("Artwork processing failed (%s); using it as-is", e)
            return data, mime, width, height, 24 if mime == "image/jpeg" else 32

    def _write_lyrics_sidecars(self, file_path: str, track: TrackMetadata):
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

        if track.synced_lyrics and self._want("synced_lyrics"):
            try:
                write_atomic(base + ".lrc", track.synced_lyrics)
            except Exception as e:
                logger.warning(f"Failed to write synced lyrics sidecar for {file_path}: {e}")

        if track.lyrics and self._want("lyrics"):
            try:
                write_atomic(base + ".txt", track.lyrics)
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
        # v2.0.0 DMX-1 — the two lyric kinds are separate toggles. Blanking
        # them here rather than branching per container means every downstream
        # fallback (MP3's USLT falling back to the LRC, MP4's ©lyr taking
        # whichever exists) keeps working unchanged.
        if not self._want("lyrics"):
            lyrics = ""
        if not self._want("synced_lyrics"):
            synced_lyrics = ""

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
                # The toggle names the TAG, not the source: with `lyrics`
                # off, LYRICS stays absent even though the LRC would
                # otherwise backfill it as readable plain text.
                if self._want("lyrics"):
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
                if best and self._want("lyrics"):
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
                audio.save(path, v1=self._id3_v1, v2_version=3)

            elif ext in (".m4a", ".mp4", ".aac"):
                audio = MP4(path)
                # M4A only supports plain text lyrics via \xa9lyr atom
                # Use LRC as plain text if no plain lyrics available
                if best_plain and self._want("lyrics"):
                    audio["\xa9lyr"] = [best_plain]
                audio.save()

            else:
                from logging import getLogger
                getLogger(__name__).debug(f"[Tagger] Unsupported format for lyric embedding: {ext}")

        except Exception as e:
            from logging import getLogger
            getLogger(__name__).warning(f"[Tagger] Failed to embed lyrics into {path}: {e}")
