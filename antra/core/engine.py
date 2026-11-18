"""
Download engine — orchestrates resolve → download → tag → organize.
"""
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Optional

from mutagen import File as MutagenFile

from antra.core.control import DownloadController
from antra.core.events import EngineEvent, EngineEventType
from antra.core.models import AudioFormat, TrackMetadata, DownloadResult, DownloadStatus
from antra.core.resolver import SourceResolver
from antra.sources.base import RateLimitedError
from antra.utils.matching import duration_close
from antra.utils.lyrics import LyricsFetcher
from antra.utils.post_command import PostCommandConfig, PostCommandRunner
from antra.utils.organizer import LibraryOrganizer, SUPPORTED_AUDIO_EXTENSIONS
from antra.utils.tagger import FileTagger, ArtworkPreferences, tag_problem_for
from antra.utils.transcoder import AudioTranscoder

logger = logging.getLogger(__name__)

# errno values that indicate the output filesystem is no longer accessible
# (NAS disconnected, drive ejected, SMB session dropped after sleep, etc.)
_MOUNT_LOST_ERRNOS = frozenset({
    13,   # EACCES / EPERM  — permission denied (SMB session dropped)
    57,   # ENOTCONN        — socket not connected (macOS SMB after sleep)
    5,    # EIO             — I/O error (drive I/O failure)
    30,   # EROFS           — read-only filesystem (mount degraded)
    116,  # ESTALE          — stale NFS/SMB file handle
})


def _is_mount_lost_error(exc: BaseException) -> bool:
    """Return True if the exception looks like the output filesystem vanished."""
    return isinstance(exc, OSError) and exc.errno in _MOUNT_LOST_ERRNOS


def _is_server_error(exc: BaseException) -> bool:
    """Return True if the exception looks like a remote server 5xx failure.

    Used by the circuit breaker to distinguish between "track not found / auth
    issue" (which should not trip the breaker) and "the mirror server itself is
    down / returning 500" (which should rate-limit the adapter globally after
    3 consecutive failures so subsequent tracks skip it immediately).
    """
    import re as _re
    # Strip URLs BEFORE matching. requests embeds the full request URL in its
    # error text, so a bare \b5\d{2}\b match reads the URL as a status code:
    # measured, a 404 for track id 512 gives
    #   "404 Client Error: Not Found for url: .../api/stream/512"
    # which matched and counted a DEFINITIVE licensing refusal toward the 5xx
    # circuit breaker. Same class as the mirror servers' _URL_IN_ERROR_RE,
    # where matching the raw string once classified every Qobuz 403 as a
    # signature fault and drained that pool 72 -> 0.
    msg = _re.sub(r"https?://\S+", " ", str(exc)).lower()
    # Catch explicit HTTP status codes (500, 502, 503, 504, 507…)
    if _re.search(r"\b5\d{2}\b", msg):
        return True
    # Catch phrased server errors from mirror adapters
    return any(kw in msg for kw in (
        "server error", "internal error",
        "service unavailable", "bad gateway",
    ))


def _summarize_source_error(msg: str) -> str:
    """Condense an adapter download-failure message into a short chain tag.

    Used for the per-track source-chain summary so users can see, at a glance,
    why each source could not deliver the track (rather than just the last error).
    """
    m = (msg or "").lower()
    if any(c in m for c in ("500", "502", "503", "504", "507")) or any(
        kw in m for kw in ("server error", "service unavailable", "bad gateway", "internal error")
    ):
        return "server error"
    if any(kw in m for kw in ("only available as high", "quality unconfirmable",
                              "quality mismatch", "lossless unavailable")):
        return "no lossless (AAC only)"
    if "truncated" in m or "preview" in m:
        return "truncated/preview"
    if "rate" in m and "limit" in m:
        return "rate-limited"
    if "no matching source" in m or "no source" in m or "no catalog match" in m:
        return "no catalog match"
    return (msg or "failed").strip()[:50]


@dataclass
class EngineConfig:
    max_retries: int = 3
    retry_delay: float = 5.0
    fetch_lyrics: bool = True
    fetch_artwork: bool = True
    save_cover_art_sidecar: bool = False
    # v2.0.0 PORT-2 - sidecar files, all default off.
    sidecar_cue: bool = False
    sidecar_nfo: bool = False
    sidecar_json: bool = False
    # v2.0.0 FR-2 - cover art naming / size / format / quality.
    artwork_prefs: Optional[ArtworkPreferences] = None
    filename_preferences: Optional[dict] = None
    output_format: str = "source"
    strict_matching: bool = False
    max_workers: int = 1
    # v1.1.8 FEAT-3 — fail a track rather than keep a lossy substitute when a
    # lossless format was requested.
    strict_format: bool = True
    # v1.1.8 FEAT-2 — never re-encode one lossy format into another.
    prevent_lossy_transcode: bool = True
    # v1.1.8 FEAT-4 — stamp ANTRA_VERSION / ANTRA_SOURCE / ANTRA_DOWNLOADED.
    write_antra_tags: bool = True
    # v2.0.0 DMX-1 — per-tag write toggles, date rendering, legacy ID3v1.
    disabled_tags: tuple[str, ...] = ()
    date_format: str = "iso"
    write_id3v1: bool = True
    # Threads that resolve upcoming tracks while other tracks download, so a
    # download worker never spends its slot on a network search. 0 disables it
    # and restores the pre-v1.1.8 behaviour exactly.
    #
    # Deliberately left at 0 for single-worker (free) runs: with 1 worker a
    # prefetch thread would double the concurrent request load the mirrors see
    # from free users, and FEAT-7's rule is that the free tier must not change.
    prefetch_resolves: int = 0
    # v2.0.0 DMX-6 - post-download command hook. None = never runs.
    post_command: Optional[PostCommandConfig] = None
    # v2.0.0 SF-2 - ReplayGain 2.0 tags. Off by default: a full decode per track.
    replaygain: bool = False
    # Audio fingerprint verification against the track's store preview.
    verify_audio: bool = False
    verify_audio_threshold: float = 0.35


class DownloadEngine:
    def __init__(
        self,
        resolver: SourceResolver,
        organizer: LibraryOrganizer,
        lyrics_fetcher: Optional[LyricsFetcher] = None,
        config: Optional[EngineConfig] = None,
        event_callback: Optional[Callable[[EngineEvent], None]] = None,
        controller: Optional[DownloadController] = None,
    ):
        self.resolver = resolver
        self.organizer = organizer
        self.lyrics = lyrics_fetcher
        # cfg MUST be assigned before the tagger/transcoder — both read from it.
        self.cfg = config or EngineConfig()
        self.tagger = FileTagger(
            write_antra_tags=getattr(self.cfg, "write_antra_tags", True),
            artwork_prefs=getattr(self.cfg, "artwork_prefs", None),
            filename_preferences=getattr(self.cfg, "filename_preferences", None)
            or getattr(organizer, "filename_preferences", None),
            disabled_tags=getattr(self.cfg, "disabled_tags", ()),
            date_format=getattr(self.cfg, "date_format", "iso"),
            write_id3v1=getattr(self.cfg, "write_id3v1", True),
        )
        self.transcoder = AudioTranscoder(
            prevent_lossy_transcode=getattr(self.cfg, "prevent_lossy_transcode", True),
        )
        self.post_command = PostCommandRunner(
            getattr(self.cfg, "post_command", None),
            library_root=str(getattr(organizer, "root", "") or ""),
        )
        self.event_callback = event_callback
        self.controller = controller
        self._emit_lock = threading.Lock()
        # Set when a mount-loss error is detected mid-batch so remaining workers
        # can abort immediately instead of producing per-track error messages.
        self._output_lost = threading.Event()
        self._output_lost_message: str = ""
        # Per-adapter consecutive server-error counter (survives across tracks).
        # When an adapter hits 3 consecutive 5xx failures it is rate-limited for
        # 5 minutes so the resolver stops selecting it for subsequent tracks.
        self._adapter_server_errors: dict[str, int] = {}
        self._adapter_server_errors_lock = threading.Lock()
        # Resolutions computed ahead of time by the prefetcher, keyed by the
        # 1-based track index. Each entry is consumed exactly once, by the first
        # resolve attempt for that track (see _take_prefetched).
        self._prefetched: dict[int, tuple] = {}
        self._prefetch_lock = threading.Lock()
        self._prefetch_pool: Optional[ThreadPoolExecutor] = None
        self._prefetch_stop = threading.Event()
        # How far behind the current track an unclaimed entry may sit before it
        # is pruned; set from max_workers when prefetching starts.
        self._prefetch_slack = 1
        # Resolves claimed but not yet stored, counted against the queue budget.
        self._prefetch_inflight = 0
        # v2.0.0 SF-2 - per-file loudness kept until the album pass consumes it.
        self._rg_measurements: dict[str, object] = {}
        self._rg_lock = threading.Lock()

    def _signal_output_lost(self, exc: OSError) -> None:
        """Record the first mount-loss error so workers can abort fast."""
        if not self._output_lost.is_set():
            self._output_lost_message = (
                f"Output directory became inaccessible mid-download "
                f"(errno {exc.errno}: {exc.strerror}). "
                "This usually means a NAS/network drive disconnected (e.g. Mac sleep). "
                "Remaining tracks skipped — re-queue to resume."
            )
            logger.error(f"  [MOUNT LOST]  {self._output_lost_message}")
            self._output_lost.set()

    def _emit(self, event_type: EngineEventType, **kwargs):
        if not self.event_callback:
            return
        with self._emit_lock:
            try:
                self.event_callback(EngineEvent(type=event_type, **kwargs))
            except Exception as e:
                logger.debug(f"Event callback failed: {e}")

    @staticmethod
    def _hydrate_track_metadata(track: TrackMetadata, result) -> None:
        if (not track.album or track.album == "Unknown Album") and result.album:
            track.album = result.album
        if not track.artwork_url and getattr(result, "artwork_url", None):
            track.artwork_url = result.artwork_url

    def _fetch_lyrics_if_needed(self, track: TrackMetadata) -> None:
        if not self.cfg.fetch_lyrics or not self.lyrics:
            return
        if track.lyrics or track.synced_lyrics:
            return
        try:
            plain, synced = self.lyrics.fetch(track)
            track.lyrics = plain
            track.synced_lyrics = synced
        except Exception as e:
            logger.debug(f"  ℹ  Lyrics fetch failed: {e}")

    @staticmethod
    def _enrich_genres_if_needed(track: TrackMetadata) -> None:
        """Populate track.genres from MusicBrainz when Spotify didn't provide any."""
        if track.genres or not track.isrc:
            return
        try:
            from antra.utils.musicbrainz import fetch_genres
            genres = fetch_genres(track.isrc)
            if genres:
                track.genres = genres
                logger.debug(f"  [MB]  Genres for '{track.title}': {', '.join(genres)}")
        except Exception as e:
            logger.debug(f"  [MB]  Genre fetch failed: {e}")

    @staticmethod
    def _enrich_track_metadata_if_needed(track: TrackMetadata) -> None:
        """Fill missing metadata (ISRC, track number, release date, genre, artwork) from Deezer + iTunes."""
        from antra.utils.matching import score_similarity
        import re as _re

        needs_isrc = not track.isrc
        needs_track_num = not track.track_number
        needs_disc = not track.disc_number
        needs_date = not track.release_year and not track.release_date
        needs_genre = not track.genres
        needs_composer = not track.composer
        _is_spotify_art = bool(track.artwork_url and "i.scdn.co" in track.artwork_url)
        needs_art = not track.artwork_url or _is_spotify_art

        if not any([needs_isrc, needs_track_num, needs_disc, needs_date, needs_genre, needs_art, needs_composer]):
            return
        if not track.title or not track.artists:
            return

        try:
            import requests as _req
        except ImportError:
            return

        artist = track.artists[0]
        title = track.title

        # ── Deezer free API: ISRC, track position, disc, release date, artwork ──
        try:
            resp = _req.get(
                "https://api.deezer.com/search",
                params={"q": f'artist:"{artist}" track:"{title}"', "limit": 5},
                timeout=8,
            )
            if resp.status_code == 200:
                for hit in resp.json().get("data") or []:
                    hit_title = hit.get("title") or ""
                    hit_artist = (hit.get("artist") or {}).get("name") or ""
                    if score_similarity(title, track.artists, hit_title, hit_artist) < 0.60:
                        continue
                    if needs_isrc and hit.get("isrc"):
                        track.isrc = hit["isrc"]
                        needs_isrc = False
                        logger.debug("[MetaEnrich] ISRC from Deezer: %s", title)
                    if needs_track_num and hit.get("track_position"):
                        track.track_number = int(hit["track_position"])
                        needs_track_num = False
                        logger.debug("[MetaEnrich] Track# from Deezer: %s -> %s", title, track.track_number)
                    if needs_disc and hit.get("disk_number"):
                        track.disc_number = int(hit["disk_number"])
                        needs_disc = False
                    if needs_date:
                        rd = (hit.get("album") or {}).get("release_date") or ""
                        if rd:
                            track.release_date = rd
                            try:
                                track.release_year = int(rd[:4])
                            except (ValueError, TypeError):
                                pass
                            needs_date = False
                            logger.debug("[MetaEnrich] Date from Deezer: %s -> %s", title, rd)
                    if needs_art:
                        cover_xl = (hit.get("album") or {}).get("cover_xl") or ""
                        if cover_xl:
                            track.artwork_url = cover_xl
                            needs_art = False
                            logger.debug("[MetaEnrich] Art from Deezer: %s", title)
                    break
        except Exception as e:
            logger.debug("[MetaEnrich] Deezer failed for %r: %s", title, e)

        # ── iTunes Search API: track#, disc#, year, genre, composer, artwork ──
        if any([needs_track_num, needs_disc, needs_date, needs_genre, needs_art, needs_composer]):
            try:
                resp = _req.get(
                    "https://itunes.apple.com/search",
                    params={"term": f"{artist} {title}", "entity": "song", "limit": 8, "country": "us"},
                    timeout=8,
                )
                if resp.status_code == 200:
                    for hit in resp.json().get("results") or []:
                        if hit.get("wrapperType") != "track":
                            continue
                        hit_title = hit.get("trackName") or ""
                        hit_artist = hit.get("artistName") or ""
                        if score_similarity(title, track.artists, hit_title, hit_artist) < 0.60:
                            continue
                        if needs_track_num and hit.get("trackNumber"):
                            track.track_number = int(hit["trackNumber"])
                            needs_track_num = False
                            logger.debug("[MetaEnrich] Track# from iTunes: %s -> %s", title, track.track_number)
                        if needs_disc and hit.get("discNumber"):
                            track.disc_number = int(hit["discNumber"])
                            needs_disc = False
                        if needs_date and not track.release_year:
                            rd = hit.get("releaseDate") or ""
                            if rd and len(rd) >= 4 and rd[:4].isdigit():
                                track.release_year = int(rd[:4])
                                track.release_date = rd[:10]
                                needs_date = False
                                logger.debug("[MetaEnrich] Year from iTunes: %s -> %s", title, track.release_year)
                        if needs_genre and hit.get("primaryGenreName"):
                            track.genres = [hit["primaryGenreName"]]
                            needs_genre = False
                            logger.debug("[MetaEnrich] Genre from iTunes: %s -> %s", title, track.genres)
                        if needs_composer and hit.get("composerName"):
                            track.composer = hit["composerName"]
                            needs_composer = False
                        if needs_art and hit.get("artworkUrl100"):
                            track.artwork_url = _re.sub(r"\d+x\d+bb", "3000x3000bb", hit["artworkUrl100"])
                            needs_art = False
                            logger.debug("[MetaEnrich] Art from iTunes: %s", title)
                        break
            except Exception as e:
                logger.debug("[MetaEnrich] iTunes failed for %r: %s", title, e)

    @staticmethod
    def _metadata_debug_snapshot(track: TrackMetadata) -> dict:
        """Compact metadata snapshot for post-resolve / pre-tag diagnostics."""
        return {
            "title": track.title,
            "album": track.album,
            "artists": track.artists or [],
            "album_artists": track.album_artists or [],
            "isrc": track.isrc or "",
            "genres": track.genres or [],
            "composer": track.composer or "",
            "release_year": track.release_year,
            "release_date": track.release_date or "",
            "track_number": track.track_number,
            "disc_number": track.disc_number,
            "artwork_url": bool(track.artwork_url),
        }

    @classmethod
    def _log_pre_tag_metadata_diagnostics(
        cls,
        track: TrackMetadata,
        result,
        file_path: str,
        adapter_name: str,
        before_snapshot: dict,
    ) -> None:
        """Log a structured before/after enrichment snapshot and flag missing key tags."""
        source_meta = getattr(result, "source_metadata", None) or {}
        after_snapshot = cls._metadata_debug_snapshot(track)
        logger.debug(
            "  [META] pre-tag adapter=%s file=%s source_meta=%s before=%s after=%s",
            adapter_name,
            file_path,
            source_meta,
            before_snapshot,
            after_snapshot,
        )
        if source_meta.get("isrc") and not track.isrc:
            logger.warning(
                "  [META] Resolver returned ISRC %r for '%s' via %s, but track.isrc is still empty before tagging.",
                source_meta.get("isrc"),
                track.title,
                adapter_name,
            )
        if source_meta.get("isrc") and not track.genres:
            diagnostics = getattr(track, "_antra_meta_diag", None) or {}
            logger.warning(
                "  [META] Genre still missing before tagging '%s' via %s despite resolver ISRC %r. "
                "This means post-resolve enrichment did not recover genre. diagnostics=%s",
                track.title,
                adapter_name,
                source_meta.get("isrc"),
                diagnostics,
            )

    @staticmethod
    def _audio_format_from_path(file_path: str) -> AudioFormat | None:
        ext = file_path.lower().rsplit(".", 1)[-1] if "." in file_path else ""
        basic = {
            "flac": AudioFormat.FLAC,
            "mp3": AudioFormat.MP3,
            "aac": AudioFormat.AAC,
        }.get(ext)
        if basic is not None:
            return basic
        if ext == "m4a":
            try:
                audio = MutagenFile(file_path)
                codec = str(getattr(getattr(audio, "info", None), "codec", "") or "").lower()
                if codec.startswith("alac"):
                    return AudioFormat.ALAC
            except Exception:
                pass
            return AudioFormat.AAC
        return None

    @classmethod
    def _quality_label_from_file(
        cls,
        file_path: str,
        fallback_format: AudioFormat | None,
        fallback_label: str,
        source_kbps: int | None = None,
    ) -> str:
        """`source_kbps` is the bitrate of the LOSSY file we were handed before any re-encode.
        A 128 kbps stream transcoded to a 320 kbps MP3 is still 128 kbps of audio, so the label
        never shows more than the source had."""
        try:
            audio = MutagenFile(file_path)
        except Exception:
            audio = None
        info = getattr(audio, "info", None)
        detected_format = cls._audio_format_from_path(file_path) or fallback_format
        if info and detected_format is not None:
            sample_rate = getattr(info, "sample_rate", None)
            bit_depth = getattr(info, "bits_per_sample", None)
            bitrate = getattr(info, "bitrate", None)
            fmt = detected_format.value.upper()
            if detected_format in {AudioFormat.FLAC, AudioFormat.ALAC}:
                if bit_depth and sample_rate:
                    return f"{fmt} {int(bit_depth)}-bit/{int(sample_rate) // 1000}kHz"
                if bit_depth:
                    return f"{fmt} {int(bit_depth)}-bit"
                return fmt
            file_kbps = int(bitrate) // 1000 if bitrate else None
            if source_kbps and (not file_kbps or source_kbps < file_kbps):
                return f"{fmt} {int(source_kbps)}kbps"
            if file_kbps:
                return f"{fmt} {file_kbps}kbps"
            return fmt
        return fallback_label

    @staticmethod
    def _lossy_source_bitrate_kbps(file_path: str, result) -> int | None:
        """Real bitrate of a lossy download, measured from the file; the adapter's own claim
        (`quality_kbps`) is only the fallback, because adapters report what they asked for
        (JioSaavn "320", YouTube's flat search `abr`) and not what arrived."""
        bitrate = None
        try:
            info = getattr(MutagenFile(file_path), "info", None)
            bitrate = getattr(info, "bitrate", None)
        except Exception:
            pass
        if not bitrate:
            from antra.utils.runtime import get_ffprobe_exe
            ffprobe = get_ffprobe_exe() or shutil.which("ffprobe")
            if ffprobe:
                try:
                    out = subprocess.run(
                        [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
                         "stream=bit_rate:format=bit_rate", "-of", "default=noprint_wrappers=1:nokey=1", file_path],
                        capture_output=True, text=True, timeout=15,
                    ).stdout.split()
                    bitrate = next((int(v) for v in out if v.isdigit() and int(v) > 0), None)
                except Exception:
                    bitrate = None
        if bitrate:
            return int(round(int(bitrate) / 1000))
        claimed = getattr(result, "quality_kbps", None)
        return int(claimed) if claimed else None

    @staticmethod
    def _source_is_lossy_only(adapter) -> bool:
        """A source that only has lossy audio (YouTube, JioSaavn, Apple without its wrapper) is
        reached only when no lossless service had the track. FEAT-2 must not then leave the
        user with a container they did not ask for, so these may be re-encoded to the target."""
        return bool(getattr(adapter, "is_last_resort", False) or getattr(adapter, "always_lossy", False))

    def _should_convert_output(self, file_path: str, output_format: str, allow_lossy_reencode: bool = False) -> bool:
        return self.transcoder.needs_conversion(file_path, output_format, allow_lossy_reencode)

    @staticmethod
    def _format_conversion_log(file_path: str, output_format: str) -> str:
        ext = os.path.splitext(file_path)[1].lower() or "source"
        base_format = output_format.split("-")[0] if output_format.endswith(("-16", "-24")) else output_format
        if base_format in {"lossless", "flac"}:
            return f"Preparing FLAC output from {ext}"
        if base_format == "alac":
            return f"Preparing ALAC output from {ext}"
        if base_format in {"aac", "m4a", "mp3"}:
            return f"Transcoding to {base_format.upper()} from {ext}"
        return f"Converting to {output_format} from {ext}"

    def _requires_lossless_output(self) -> bool:
        return self.cfg.output_format in {"flac", "lossless", "alac", "lossless-16", "lossless-24", "alac-16", "alac-24"}

    def _is_lossy_output_mode(self) -> bool:
        return self.cfg.output_format in {"mp3", "aac", "m4a"}

    @staticmethod
    def _probe_duration_seconds(file_path: str) -> float | None:
        # Mutagen is unreliable for some Apple-wrapper ALAC M4A files and can
        # report short bogus durations (for example ~15s for a full-length song).
        # Prefer ffprobe for MP4-family containers so lossless Apple downloads
        # are not falsely flagged as truncated.
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".m4a":
            return DownloadEngine._probe_duration_seconds_with_ffprobe(file_path)
        try:
            audio = MutagenFile(file_path)
        except Exception:
            return None
        if not audio or not getattr(audio, "info", None):
            return DownloadEngine._probe_duration_seconds_with_ffprobe(file_path)
        length = getattr(audio.info, "length", None)
        if length is None:
            return DownloadEngine._probe_duration_seconds_with_ffprobe(file_path)
        try:
            return float(length)
        except (TypeError, ValueError):
            return DownloadEngine._probe_duration_seconds_with_ffprobe(file_path)

    @staticmethod
    def _probe_duration_seconds_with_ffprobe(file_path: str) -> float | None:
        from antra.utils.runtime import get_ffprobe_exe
        ffprobe = get_ffprobe_exe() or shutil.which("ffprobe")
        if not ffprobe:
            return None
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    file_path,
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        try:
            return float(result.stdout.strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _container_is_unreadable(file_path: str) -> bool:
        """True only when a probing tool ran successfully as a process and still
        could not read an audio duration — i.e. the container is genuinely
        broken (v1.1.8 BUG-4, the "1.2 MB won't play" stubs).

        Deliberately conservative about the difference between "this file is
        broken" and "we could not check": if ffprobe is missing, cannot be
        launched, or times out, this returns False so a working download is
        never rejected just because the toolchain is unavailable. That
        distinction is the whole point — `_probe_duration_seconds` collapses
        both cases to None, which is why an unplayable stub previously sailed
        through as "nothing to report".
        """
        from antra.utils.longpath import extended_path
        file_path = extended_path(file_path)
        try:
            audio = MutagenFile(file_path)
            if audio is not None and getattr(audio, "info", None) is not None:
                length = getattr(audio.info, "length", None)
                if length and float(length) > 0:
                    return False
        except Exception:
            pass

        from antra.utils.runtime import get_ffprobe_exe
        ffprobe = get_ffprobe_exe() or shutil.which("ffprobe")
        if not ffprobe:
            return False  # no tool — cannot judge, so do not reject
        try:
            proc = subprocess.run(
                [
                    ffprobe, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    file_path,
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception:
            return False  # could not run it — cannot judge
        if proc.returncode != 0:
            return True   # ffprobe ran and rejected the file
        try:
            return float(proc.stdout.strip()) <= 0
        except (TypeError, ValueError):
            return True   # ran fine but produced no usable duration

    _LOSSLESS_OUTPUT_FORMATS = {
        "flac", "lossless", "lossless-16", "lossless-24",
        "alac", "alac-16", "alac-24",
    }

    def _audio_verification_problem(self, file_path: str, track) -> str | None:
        """Reason to reject `file_path` as the wrong recording, else None.

        Returns None for every inconclusive outcome (no preview for the track,
        no fingerprinting tool, network failure) so a track is only ever failed
        on positive evidence that the audio is a different recording.
        """
        try:
            from antra.utils.audio_verify import verify_file

            result = verify_file(
                file_path,
                isrc=getattr(track, "isrc", "") or "",
                title=getattr(track, "title", "") or "",
                artist=(getattr(track, "artists", None) or [""])[0],
                duration_ms=getattr(track, "duration_ms", None),
                threshold=self.cfg.verify_audio_threshold,
            )
        except Exception as exc:  # noqa: BLE001 - verification must never fail a good download
            logger.debug("  [VERIFY] check failed for %s: %s", getattr(track, "title", ""), exc)
            return None
        if result.is_mismatch:
            logger.info("  [VERIFY] %s: rejected, %s", track.title, result.reason)
            return (
                f"Audio does not match '{track.title}' — the file contains a different "
                f"recording (fingerprint difference {result.score:.2f}, limit "
                f"{self.cfg.verify_audio_threshold:.2f}) — retrying with next source"
            )
        if result.verdict == "match":
            logger.info("  [VERIFY] %s: audio matches the requested recording", track.title)
        else:
            logger.debug("  [VERIFY] %s: not checked — %s", track.title, result.reason)
        return None

    def _strict_format_problem(self, file_path: str) -> str | None:
        """Reject a lossy file delivered under a lossless output format
        (v1.1.8 FEAT-3).

        This is the "I asked for FLAC 16-bit and got MP3s" report, and it is
        genuinely part bug: the transcoder deliberately refuses to convert a lossy
        source into a lossless container (that would fabricate fake lossless, and
        that refusal is correct) — but nothing then rejected the file, so the
        lossy download was simply *kept*. The right outcome is to fail the track
        so another source can be tried, which is what this does.
        """
        if not getattr(self.cfg, "strict_format", True):
            return None
        target = (self.cfg.output_format or "").lower()
        if target not in self._LOSSLESS_OUTPUT_FORMATS:
            return None
        try:
            if not self.transcoder._is_lossy(file_path):
                return None
        except Exception:
            return None
        delivered = os.path.splitext(file_path)[1].lstrip(".").upper() or "lossy"
        return (
            f"only a lossy {delivered} copy was available but {target.upper()} was "
            f"requested — no lossless source had this track"
        )

    @staticmethod
    def _final_delivery_problem(file_path: str) -> str | None:
        """Last gate before a track is counted as delivered (v1.1.8 BUG-6 defect B).

        A track may only be marked done if the file is something Python can
        actually see and read. This is deliberately narrow — it checks
        *accessibility*, not tag success: a legitimately unsupported container
        (`tag_ok = False` on an otherwise perfect file) must still pass, because
        failing those would regress every format we can download but not tag.

        The case this exists for is a file that is inaccessible to Python while
        looking fine to ffmpeg — on Windows an over-MAX_PATH path is created and
        read happily by ffmpeg but raises FileNotFoundError from `open()`,
        `os.path.getsize()` and mutagen. Previously such a track was tagged
        (failing with a logged warning) and then still counted as a success.
        """
        from antra.utils.longpath import extended_path, path_too_long
        try:
            size = os.path.getsize(extended_path(file_path))
        except OSError as e:
            if path_too_long(file_path):
                return (
                    f"the file path is {len(file_path)} characters, over the Windows "
                    "260-character limit — choose a shorter music library folder, or "
                    "shorten the filename template in Settings"
                )
            return (
                f"final file is not accessible ({e.__class__.__name__}: {e.strerror or e})"
            )
        if size == 0:
            return "final file is empty (0 bytes)"
        return None

    @classmethod
    def _is_truncated_download(cls, file_path: str, expected_duration_ms: int | None) -> bool:
        return cls._get_truncation_reason(file_path, expected_duration_ms) is not None

    @classmethod
    def _get_truncation_reason(
        cls,
        file_path: str,
        expected_duration_ms: int | None,
        result_duration_ms: int | None = None,
        strict_matching: bool = False,
    ) -> str | None:
        # Existence, size-floor and container checks run for EVERY track, including
        # short ones (v1.1.8 BUG-6). Previously the whole function returned None
        # for anything under 60s, so a track like a 29-second Goldberg variation
        # received NO validation at all and a broken 8 KB file was accepted and
        # reported [Complete]. Duration *comparison* is still skipped for short
        # tracks (metadata durations are unreliable at that length) — but
        # "does this file exist and is it plausibly a real audio file" is not a
        # question we should ever decline to ask.
        tiny_lossless_reason = cls._get_tiny_lossless_file_reason(file_path, expected_duration_ms)
        if tiny_lossless_reason is not None:
            return tiny_lossless_reason

        if not expected_duration_ms or expected_duration_ms < 60000:
            # Too short for a reliable duration comparison, but we have now
            # confirmed the file exists and is not implausibly small. Still
            # verify the container actually decodes before accepting it.
            if cls._container_is_unreadable(file_path):
                return "unreadable audio container (truncated or aborted download)"
            return None

        actual_seconds = cls._probe_duration_seconds(file_path)
        if actual_seconds is None:
            # Container-integrity check (v1.1.8 BUG-4). A file we just downloaded
            # that no probing tool can read a duration from is a broken container,
            # not a pass. This is the ~1.2 MB unplayable stub: it clears the size
            # floor, fails to probe, and previously fell through to the FLAC size
            # check, which cannot parse it either and so reported nothing wrong.
            if cls._container_is_unreadable(file_path):
                return "unreadable audio container (truncated or aborted download)"
            return cls._get_flac_truncation_reason(file_path)
        expected_seconds = expected_duration_ms / 1000.0

        def _severe_mismatch(expected_s: float, actual_s: float) -> bool:
            shorter = (
                actual_s < expected_s * 0.8
                and (expected_s - actual_s) >= 20
            )
            longer = (
                actual_s > expected_s * 1.3
                and (actual_s - expected_s) >= 45
            )
            return shorter or longer

        # Strict mode: hard duration gate against the REQUESTED metadata, checked
        # FIRST. This used to sit below the source-consistency early return, which
        # made it unreachable — the engine passes the downloaded file's own probed
        # duration as result_duration_ms, so the early return always fired first
        # and strict mode never rejected a wrong-length delivery (v1.1.8 BUG-1).
        if strict_matching and not duration_close(expected_seconds, actual_seconds, tolerance=8):
            return (
                f"strict duration mismatch: got {actual_seconds:.1f}s "
                f"but expected {expected_seconds:.1f}s"
            )

        # If the source result reported its own duration and the file matches it,
        # the download is complete — but only when the source duration is itself
        # reasonably close to the expected metadata duration. Otherwise a mirror
        # can serve the wrong recording (or a preview) while staying internally
        # self-consistent, which would let bad audio slip through.
        if result_duration_ms and result_duration_ms >= 60000:
            result_seconds = result_duration_ms / 1000.0
            source_matches_expected = not _severe_mismatch(expected_seconds, result_seconds)
            if source_matches_expected and abs(actual_seconds - result_seconds) <= result_seconds * 0.05 + 5:
                # File matches the source's own declared duration — not truncated.
                return cls._get_flac_truncation_reason(file_path)

        # Gross duration mismatch check for all formats. This catches both
        # preview clips (~30s) and completely different full tracks.
        if _severe_mismatch(expected_seconds, actual_seconds):
            return (
                f"duration mismatch: got {actual_seconds:.1f}s "
                f"but expected {expected_seconds:.1f}s"
            )

        # Secondary file-size check for FLAC files.
        return cls._get_flac_truncation_reason(file_path)

    @staticmethod
    def _get_tiny_lossless_file_reason(file_path: str, expected_duration_ms: int | None) -> str | None:
        if not file_path.lower().endswith((".flac", ".m4a")):
            return None
        try:
            from antra.utils.longpath import extended_path
            actual_size = os.path.getsize(extended_path(file_path))
        except OSError:
            # Cannot even stat it — this is never an acceptable delivery. On
            # Windows this is also how an over-MAX_PATH file presents: ffmpeg can
            # create it but Python cannot see it at all (v1.1.8 BUG-6).
            return "downloaded file is missing"

        if not expected_duration_ms:
            return None

        expected_seconds = expected_duration_ms / 1000.0
        # A full-length lossless file in the low hundreds of KB is always a bad
        # delivery: preview HTML, an aborted stream, or a header-only container.
        # Short tracks get a proportionally smaller floor rather than being
        # exempted entirely (v1.1.8 BUG-6): a 29-second FLAC is still megabytes,
        # so an 8 KB file is unambiguously broken, but a 512 KB floor would be
        # too aggressive for a genuinely tiny interlude.
        # For short tracks the per-second term does the work and the absolute
        # floor is only a backstop against near-empty files. Keeping it at 64 KB
        # left a genuine 5-second interlude (68 KB) passing by just 4% — too
        # tight. 16 KB keeps ~4x headroom there while still rejecting the 8 KB
        # broken deliveries this bug is about.
        absolute_floor = 512 * 1024 if expected_seconds >= 60 else 16 * 1024
        per_second_floor = expected_seconds * 2 * 1024
        min_expected_size = int(max(absolute_floor, min(per_second_floor, 2 * 1024 * 1024)))
        if actual_size < min_expected_size:
            return (
                f"implausibly small lossless file: got {actual_size / 1024:.0f}KB "
                f"for expected {expected_seconds:.1f}s track"
            )
        return None

    @classmethod
    def _is_truncated_flac_by_size(cls, file_path: str) -> bool:
        return cls._get_flac_truncation_reason(file_path) is not None

    @staticmethod
    def _get_flac_truncation_reason(file_path: str) -> str | None:
        """
        Detect truncated FLAC downloads by comparing actual file size against
        the minimum expected size based on the FLAC header's own metadata.

        FLAC headers write the total sample count up front, so Mutagen
        reports the full *intended* duration even when the file was truncated
        mid-stream.  This check catches those cases.

        Only runs on .flac files. Small hi-res acoustic masters can compress
        much harder than a fixed size floor suggests, so we treat a low
        size-to-PCM ratio as suspicious and confirm it with a real decode
        probe before declaring the file truncated.
        """
        if not file_path.lower().endswith(".flac"):
            return None

        try:
            from mutagen.flac import FLAC as FLACFile

            audio = FLACFile(file_path)
            if not audio or not audio.info:
                return None

            bits = getattr(audio.info, "bits_per_sample", None)
            rate = getattr(audio.info, "sample_rate", None)
            channels = getattr(audio.info, "channels", None)
            length = getattr(audio.info, "length", None)

            if not all((bits, rate, channels, length)):
                return None
            if length < 60:
                return None  # Don't flag short tracks

            actual_size = os.path.getsize(file_path)
            # Raw PCM size for the declared duration
            raw_pcm_bytes = length * rate * channels * (bits / 8)
            # FLAC typically compresses to 50-70% of raw.
            # Use 0.25 as a suspicion threshold only. Some valid sparse masters
            # can dip below this, especially 24-bit/96kHz acoustic material.
            min_expected_bytes = raw_pcm_bytes * 0.25

            if actual_size < min_expected_bytes:
                ratio = actual_size / raw_pcm_bytes if raw_pcm_bytes > 0 else 0
                logger.debug(
                    f"[Engine] FLAC size check: {file_path} is {actual_size / (1024*1024):.1f}MB "
                    f"vs suspicious floor {min_expected_bytes / (1024*1024):.1f}MB "
                    f"(ratio={ratio:.2f}, {bits}bit/{rate}Hz/{length:.0f}s) — running decode probe"
                )
                if DownloadEngine._fails_flac_decode_probe(file_path):
                    return (
                        f"suspicious FLAC failed decode probe "
                        f"(ratio={ratio:.2f}, {bits}bit/{rate}Hz/{length:.0f}s)"
                    )

        except Exception as e:
            logger.debug(f"[Engine] FLAC size check failed: {e}")

        return None

    @staticmethod
    def _fails_flac_decode_probe(file_path: str) -> bool:
        """Return True when ffmpeg cannot fully decode the FLAC cleanly."""
        try:
            from antra.utils.runtime import get_ffmpeg_exe

            ffmpeg = get_ffmpeg_exe() or shutil.which("ffmpeg")
            if not ffmpeg:
                logger.debug("[Engine] FLAC decode probe skipped — ffmpeg unavailable")
                return False

            result = subprocess.run(
                [ffmpeg, "-v", "error", "-i", file_path, "-f", "null", "-"],
                capture_output=True,
                text=True,
                timeout=45,
            )
        except Exception as e:
            logger.debug(f"[Engine] FLAC decode probe failed to run: {e}")
            return False

        stderr = (result.stderr or "").strip()
        if result.returncode != 0 or stderr:
            logger.debug(
                f"[Engine] FLAC decode probe failed for {file_path}: "
                f"exit={result.returncode} stderr={stderr[-300:]}"
            )
            return True

        return False

    @staticmethod
    def _discard_file(path: str) -> None:
        import os
        from antra.utils.longpath import extended_path

        try:
            # extended_path so an over-MAX_PATH file can actually be deleted —
            # otherwise a partial from a long-path album is undeletable by us and
            # stays in the library forever (v1.1.8 BUG-6).
            target = extended_path(path)
            if target and os.path.exists(target):
                os.remove(target)
        except OSError:
            pass

    @staticmethod
    def _output_base_snapshot(output_base: str) -> set[str]:
        """Files already sitting at this output stem before a download attempt.

        Used to guarantee cleanup (v1.1.8 BUG-4): adapters stream straight into
        their final path, so an exception mid-transfer leaves a partial file on
        disk that is never tagged and never removed — which is exactly the
        reported signature ("the 30-second ones have no metadata"). Snapshotting
        first means we only ever delete files THIS attempt created, so a
        previously-completed download is never touched.
        """
        import glob
        import os

        try:
            return set(glob.glob(glob.escape(output_base) + "*"))
        except OSError:
            return set()

    # A leftover we are willing to delete must be exactly the output stem plus a
    # single short extension, optionally with a .part suffix, or a transcoder
    # temp. Deliberately strict: a bare "stem*" glob would also match a DIFFERENT
    # track that merely shares the prefix ("01 - Song.flac" vs
    # "01 - Song (Live).flac"), and with parallel workers that sibling could be
    # written by another thread mid-attempt and wrongly deleted.
    _CLEANUP_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,6}(\.part)?$")

    @classmethod
    def _cleanup_failed_attempt(cls, output_base: str, before: set[str]) -> None:
        """Remove any file that appeared at this output stem during a failed
        download attempt. A file that has not passed validation AND tagging must
        never be left visible in the library (v1.1.8 BUG-4)."""
        import glob
        import os

        try:
            leftovers = set(glob.glob(glob.escape(output_base) + "*")) - before
        except OSError:
            return
        for stale in leftovers:
            remainder = stale[len(output_base):]
            is_temp = remainder.startswith(".antra-convert.")
            if not is_temp and not cls._CLEANUP_SUFFIX_RE.match(remainder):
                # Belongs to a different track that happens to share the stem.
                continue
            try:
                if os.path.isfile(stale):
                    os.remove(stale)
                    logger.info(
                        "  [CLEAN] Removed partial file from failed attempt: %s",
                        os.path.basename(stale),
                    )
            except OSError:
                pass

    # ── v2.0.0 DMX-5 — filename_conflict_behavior ────────────────────────────

    # Suffix for a file set aside while an overwrite re-download is in flight.
    # Not an audio extension, so neither the identity index nor the library
    # scanner can mistake it for a track, and it does not match
    # _CLEANUP_SUFFIX_RE, so a failed attempt's cleanup can never delete it.
    _OVERWRITE_STASH_SUFFIX = ".antra-overwrite-old"

    def _stash_for_overwrite(
        self, track: TrackMetadata, output_base: str, stash: dict[str, str]
    ) -> None:
        """Move the existing file aside before an overwrite re-download.

        Adapters write straight into their final path, and the BUG-4 cleanup
        only removes files an attempt CREATED. Overwriting in place therefore
        meant a failed re-download truncated a perfectly good file and then left
        the stub behind. With the original moved aside it is restored on
        failure and deleted only once the replacement has fully succeeded.
        """
        from pathlib import Path
        from antra.utils.longpath import extended_path

        # Recover a track hidden by a run that was killed mid-overwrite. The desktop Stop
        # button kills the whole process tree, so no `except` ever ran: the good file stayed
        # renamed to .antra-overwrite-old and silently vanished from the library. Restoring
        # only when the original is MISSING cannot clobber a newer file. Runs in every
        # conflict mode, since a stale stash is unwanted whatever is configured now.
        for ext in SUPPORTED_AUDIO_EXTENSIONS:
            original = output_base + ext
            stale = original + self._OVERWRITE_STASH_SUFFIX
            try:
                if os.path.isfile(extended_path(stale)) and not os.path.exists(extended_path(original)):
                    os.replace(extended_path(stale), extended_path(original))
                    logger.info(f"  [OVERWRITE]  Restored a file left aside by an interrupted run: "
                                f"{os.path.basename(original)}")
            except OSError as e:
                logger.warning(f"  [OVERWRITE]  Could not restore {os.path.basename(original)}: {e}")

        if self.organizer.conflict_behavior != "overwrite":
            return

        for ext in SUPPORTED_AUDIO_EXTENSIONS:
            original = output_base + ext
            if original in stash or not os.path.isfile(extended_path(original)):
                continue
            try:
                # A different recording that merely shares the stem is never
                # ours to replace (get_output_path would normally have given
                # this request a "(2)" stem already — belt and braces).
                if self.organizer._existing_file_is_positively_different(track, Path(original)):
                    continue
            except Exception:
                pass
            backup = original + self._OVERWRITE_STASH_SUFFIX
            try:
                os.replace(extended_path(original), extended_path(backup))
                stash[original] = backup
                logger.info(f"  [OVERWRITE]  Replacing existing file: {os.path.basename(original)}")
            except OSError as e:
                logger.warning(
                    f"  [OVERWRITE]  Could not set aside {os.path.basename(original)} ({e}); "
                    "it will be overwritten in place"
                )

    @staticmethod
    def _finish_overwrite_stash(stash: dict[str, str], *, success: bool) -> None:
        from antra.utils.longpath import extended_path

        for original, backup in stash.items():
            try:
                if success:
                    if os.path.exists(extended_path(backup)):
                        os.remove(extended_path(backup))
                elif os.path.exists(extended_path(backup)):
                    # os.replace also clears any stub a failed attempt left at
                    # the original name.
                    os.replace(extended_path(backup), extended_path(original))
                    logger.info(
                        f"  [OVERWRITE]  Re-download failed — restored the original: "
                        f"{os.path.basename(original)}"
                    )
            except OSError as e:
                logger.warning(f"  [OVERWRITE]  Could not finalise {os.path.basename(original)}: {e}")
        stash.clear()

    def _retag_existing(self, track: TrackMetadata, path: str) -> bool:
        """tag_only: refresh an existing file's tags without re-downloading it.

        No resolver search runs — avoiding the mirrors is the point — so the
        metadata is the fetcher's plus the free enrichment chain (Deezer /
        iTunes / MusicBrainz / lyrics / artwork upgrade), not a source adapter's.
        """
        self._fetch_lyrics_if_needed(track)
        try:
            from antra.core.metadata_enricher import MetadataEnricher
            MetadataEnricher.enrich(track, None)
        except Exception:
            self._enrich_track_metadata_if_needed(track)
            self._enrich_genres_if_needed(track)
        ok = self.tagger.tag(path, track, provenance=False)
        if ok:
            logger.info(f"  [RETAG]  Re-tagged existing file: {track.title}")
        else:
            logger.warning(f"  [RETAG]  Could not re-tag {path} — the file was left as it was")
        return ok

    def download_track(
        self,
        track: TrackMetadata,
        track_index: Optional[int] = None,
        track_total: Optional[int] = None,
    ) -> DownloadResult:
        """Full pipeline for a single track."""
        stash: dict[str, str] = {}
        try:
            result = self._download_track_impl(track, track_index, track_total, stash)
        except BaseException:
            self._finish_overwrite_stash(stash, success=False)
            raise
        self._finish_overwrite_stash(
            stash, success=result.status == DownloadStatus.COMPLETED
        )
        # v2.0.0 DMX-6. Fired here rather than inside the impl for one
        # reason: in overwrite mode the displaced original is still sitting
        # beside the new file as .antra-overwrite-old until the line above
        # runs, and a hook that scans the folder (a rescan, a beets import)
        # would see it. This is also the more explicit place for the
        # COMPLETED gate: a SKIPPED track is already in the library and
        # changed nothing on disk, so re-triggering a rescan for it would be
        # pure noise. Contained - PostCommandRunner swallows every failure,
        # so a hook can never fail a track.
        if result.status == DownloadStatus.COMPLETED and result.file_path:
            self.post_command.run_for_track(
                file_path=result.file_path,
                track=track,
                source=result.source_used or "",
                release_folder=self.organizer.release_folder_for(result.file_path) or "",
            )
        return result

    def _download_track_impl(
        self,
        track: TrackMetadata,
        track_index: Optional[int],
        track_total: Optional[int],
        overwrite_stash: dict[str, str],
    ) -> DownloadResult:
        # 1. Resume check — only skip if the existing file meets the current output format.
        existing = self.organizer.is_already_downloaded(track)
        if existing:
            # In lossless-only mode, don't accept a previously-downloaded lossy file.
            # Re-download it as lossless instead.
            if self._requires_lossless_output():
                ext = os.path.splitext(existing)[1].lower()
                lossy_extensions = {".mp3", ".aac", ".m4a"}
                # .m4a could be ALAC (lossless) — check the actual codec
                if ext in lossy_extensions:
                    is_lossy_file = True
                    if ext == ".m4a":
                        try:
                            from mutagen import File as _MF
                            _audio = _MF(existing)
                            _codec = str(getattr(getattr(_audio, "info", None), "codec", "") or "").lower()
                            # alac codec = lossless; mp4a = AAC = lossy
                            is_lossy_file = "alac" not in _codec
                        except Exception:
                            pass  # can't probe → assume lossy, re-download
                    if is_lossy_file:
                        logger.info(
                            f"  [REDOWNLOAD]  '{track.title}' exists as lossy {ext} "
                            f"but lossless mode is active — re-downloading as lossless."
                        )
                        existing = None  # fall through to download

            if existing:
                existing = self.organizer.ensure_playlist_copy(track, existing)
                retagged = False
                retag_path = self.organizer.retag_target(track, existing)
                if retag_path:
                    retagged = self._retag_existing(track, retag_path)
                if self.cfg.save_cover_art_sidecar:
                    self.tagger.save_cover_art_sidecar(
                        existing, track, folder=self.organizer.release_folder_for(existing)
                    )
                if getattr(self.cfg, "artwork_prefs", None) and self.cfg.artwork_prefs.save_artist_image:
                    self.tagger.save_artist_image_sidecar(
                        self.organizer.artist_folder_for(existing), track
                    )
                if not retagged:
                    logger.info(f"  [SKIP]  Skipping (already downloaded): {track.title}")
                self._emit(
                    EngineEventType.TRACK_SKIPPED,
                    track=track,
                    track_index=track_index,
                    track_total=track_total,
                    file_path=existing,
                    message=(
                        "Re-tagged existing file." if retagged else "Track already exists on disk."
                    ),
                )
                return DownloadResult(
                    track=track,
                    status=DownloadStatus.SKIPPED,
                    file_path=existing,
                )

        # 2. Fetch lyrics once (before download, non-blocking)
        self._fetch_lyrics_if_needed(track)

        excluded_adapters: set[str] = set()
        # Adapters that were rate-limited get a second chance after all other
        # sources are exhausted (rate limit may have cleared by then).
        rate_limited_adapters: set[str] = set()
        # Once an adapter has been given its second chance, permanently exclude it.
        rate_limited_retried: set[str] = set()
        last_error: Optional[str] = None
        last_source: Optional[str] = None
        attempted_sources: list[str] = []  # ordered list of sources that were tried
        used_lossy_fallback: bool = False  # flag for post-download warning
        source_lossy_kbps: int | None = None  # bitrate of the lossy file as downloaded
        # Per-adapter outcome across the whole track (search + download), so the
        # full source chain can be surfaced if the track ultimately fails.
        source_chain: dict[str, str] = {}

        while True:
            # 3. Resolve — skip both permanently-excluded and currently rate-limited adapters.
            all_excluded = excluded_adapters | rate_limited_adapters
            # A prefetched answer is valid only while nothing is excluded — i.e.
            # the first attempt. Once a source has failed and been excluded, the
            # precomputed result may well be the one that just failed, so every
            # later iteration resolves fresh. Popping makes it single-use, so a
            # retry that leaves the excluded set empty still resolves fresh.
            prefetched = self._take_prefetched(track_index) if not all_excluded else None
            if prefetched is not None:
                # The report was captured in the prefetch thread. It has to be
                # carried with the resolution because last_resolve_report() is
                # thread-local — reading it here would return this worker's own,
                # which never ran a search and is empty.
                resolution, resolve_report = prefetched
            else:
                resolution = self.resolver.resolve(track, excluded_adapters=all_excluded)
                resolve_report = self.resolver.last_resolve_report()
            # Merge this cycle's search outcomes (no-match / search-error / found)
            # into the running chain. Excluded adapters aren't re-searched, so a
            # prior download-failure reason for them is preserved; the selected
            # adapter's "found" entry is overridden below if its download fails.
            source_chain.update(resolve_report)
            if not resolution:
                # Before giving up: if any adapters were rate-limited and haven't
                # had their one retry yet, unblock them and try again.
                newly_retryable = rate_limited_adapters - rate_limited_retried
                if newly_retryable:
                    logger.info(
                        f"  [RATE]  All other sources exhausted — retrying rate-limited: "
                        f"{', '.join(newly_retryable)}"
                    )
                    rate_limited_retried |= newly_retryable
                    rate_limited_adapters.clear()
                    continue

                if last_error:
                    user_error = last_error
                elif attempted_sources:
                    user_error = (
                        f"No matching source found — tried: {', '.join(attempted_sources)}"
                    )
                else:
                    fmt = self.cfg.output_format or "auto"
                    user_error = (
                        f"No source could find this track in {fmt.upper()} mode. "
                        "The track may not be in the catalog of any configured lossless service."
                    )
                if (
                    getattr(track, "amazon_asin", None)
                    and self._is_lossy_output_mode()
                    and "amazon" in excluded_adapters
                ):
                    user_error = (
                        "Amazon could not provide a playable file for this track, "
                        "and no safe YouTube fallback match was found."
                    )
                # Surface the full per-source outcome so it's clear every source was
                # tried (and why each one couldn't deliver) — not just the last error.
                if source_chain:
                    chain_str = ", ".join(
                        f"{name}: {reason}" for name, reason in source_chain.items()
                    )
                    logger.info(f"  [CHAIN]  {track.title} — sources tried → {chain_str}")
                self.organizer.mark_failed(track, user_error)
                self._emit(
                    EngineEventType.TRACK_FAILED,
                    track=track,
                    track_index=track_index,
                    track_total=track_total,
                    source=last_source,
                    error=user_error,
                )
                return DownloadResult(
                    track=track,
                    status=DownloadStatus.FAILED,
                    source_used=last_source,
                    error_message=user_error,
                    attempt_count=self.cfg.max_retries,
                )

            result, adapter = resolution
            # Track if we ended up using a lossy source in lossless-prefer mode
            # (so we can emit a post-download warning). The resolver already handles
            # the "prefer lossless, fall back to lossy as last resort" logic.
            if self._requires_lossless_output() and not result.is_lossless:
                used_lossy_fallback = True
            self._hydrate_track_metadata(track, result)
            adapter.hydrate_track_metadata(track, result)
            self._fetch_lyrics_if_needed(track)
            # Layout must use post-hydration metadata (album/year from the resolver, etc.)
            try:
                output_base = self.organizer.get_output_path(track)
            except OSError as e:
                if _is_mount_lost_error(e):
                    self._signal_output_lost(e)
                raise
            self._emit(
                EngineEventType.TRACK_RESOLVED,
                track=track,
                track_index=track_index,
                track_total=track_total,
                source=adapter.name,
                quality_label=result.quality_label,
                message=f"Resolved via {adapter.name}",
            )

            file_path: Optional[str] = None
            final_error: Optional[Exception] = None
            # Before the first attempt, so each attempt's cleanup snapshot sees
            # the backup as pre-existing and can never remove it (DMX-5).
            self._stash_for_overwrite(track, output_base, overwrite_stash)

            for attempt in range(1, self.cfg.max_retries + 1):
                self._last_attempt_start = time.time()
                # Snapshot BEFORE the try so the except handler can never see it
                # unbound, no matter where the attempt fails (v1.1.8 BUG-4).
                _attempt_snapshot = self._output_base_snapshot(output_base)
                try:
                    source_text = adapter.name
                    if adapter.name == "soulseek" and result.stream_id:
                        parts = str(result.stream_id).split("|")
                        if len(parts) >= 1:
                            source_text = f"soulseek({parts[0]})"
                            
                    # In Atmos mode the adapter's own label describes what it found
                    # in the catalogue (FLAC / ALAC), not what is being fetched —
                    # the ?format=atmos request returns an EC-3 spatial stream. The
                    # log and the UI badge said "(FLAC)" / "(ALAC 16-bit/44kHz)" for
                    # Atmos downloads, which read as the feature silently not
                    # working even on the run that produced a genuine Atmos file.
                    if (self.cfg.output_format or "").lower().startswith("atmos"):
                        source_quality = "Dolby Atmos"
                    else:
                        source_quality = result.quality_label
                        if getattr(result, "sample_rate", None):
                            source_quality += f" / {result.sample_rate / 1000}kHz"

                    self._emit(
                        EngineEventType.TRACK_DOWNLOAD_ATTEMPT,
                        track=track,
                        track_index=track_index,
                        track_total=track_total,
                        source=source_text,
                        quality_label=source_quality,
                        attempt=attempt,
                    )

                    if attempt == 1:
                        logger.info(
                            f"  \U0001f4e5 [Downloading] [{track_index}/{track_total}] {track.title} by {track.artist_string} ({source_quality})"
                        )
                    else:
                        logger.info(
                            f"  \U0001f501 [Retry {attempt}] [{track_index}/{track_total}] {track.title} ({source_quality})"
                        )
                    candidate_path = adapter.download(result, output_base)
                    # Probe actual duration before transcoding — used as the
                    # authoritative reference for the truncation check below.
                    # Amazon OPUS streams may be a different edit than the
                    # Spotify metadata suggests; probing before conversion
                    # gives us the true source duration.
                    # Measured BEFORE any re-encode and reset every attempt, so a later lossless
                    # source never inherits an earlier lossy source's cap.
                    source_lossy_kbps = (
                        None if result.is_lossless
                        else self._lossy_source_bitrate_kbps(candidate_path, result)
                    )
                    _pre_transcode_duration_s = self._probe_duration_seconds(candidate_path)
                    source_duration_ms: int | None = (
                        int(_pre_transcode_duration_s * 1000)
                        if _pre_transcode_duration_s is not None else None
                    )
                    if self._should_convert_output(
                        candidate_path, self.cfg.output_format,
                        allow_lossy_reencode=self._source_is_lossy_only(adapter),
                    ):
                        logger.info(
                            "  [FMT]  %s: %s",
                            self._format_conversion_log(candidate_path, self.cfg.output_format),
                            track.title,
                        )
                        try:
                            candidate_path = self.transcoder.convert(
                                candidate_path, self.cfg.output_format,
                                allow_lossy_reencode=self._source_is_lossy_only(adapter),
                                max_source_kbps=source_lossy_kbps,
                            )
                        except RuntimeError as conv_err:
                            # ffmpeg failed — discard the corrupt source so it does not
                            # linger on disk and re-raise so the engine falls through to
                            # the next adapter (Apple DRM-locked M4A being the primary case).
                            self._discard_file(candidate_path)
                            raise RuntimeError(
                                f"[{adapter.name}] Audio conversion failed — "
                                f"source file may be corrupt or DRM-protected: {conv_err}"
                            ) from conv_err
                        except (KeyError, ValueError) as conv_err:
                            # Unsupported format string (e.g. 'lossless-24' in old binary) —
                            # keep the file as-is rather than crashing the whole engine.
                            logger.warning(
                                f"  [FMT]  Format conversion skipped ({conv_err}) — "
                                f"keeping source file: {candidate_path}"
                            )
                    # FEAT-3: a lossy file delivered under a lossless output
                    # format must fail the track, not be silently kept. Checked
                    # after conversion so an ALAC→FLAC style remux has already
                    # happened and only a genuinely lossy delivery is rejected.
                    strict_format_reason = self._strict_format_problem(candidate_path)
                    if strict_format_reason is not None:
                        self._discard_file(candidate_path)
                        raise RuntimeError(
                            f"[{adapter.name}] {strict_format_reason}"
                        )
                    truncation_reason = self._get_truncation_reason(
                        candidate_path,
                        track.duration_ms,
                        result_duration_ms=source_duration_ms,
                        strict_matching=self.cfg.strict_matching,
                    )
                    if truncation_reason is not None:
                        self._discard_file(candidate_path)
                        raise RuntimeError(
                            f"[{adapter.name}] Download appears truncated for {track.title} "
                            f"({truncation_reason})"
                        )
                    # Quality verification: if the adapter claimed 24-bit hi-res but
                    # the actual file is 16-bit, discard and try the next source.
                    # Amazon in particular hardcodes bit_depth=24 in search results
                    # but sometimes serves 16-bit streams (track-level quality < album).
                    claimed_bit_depth = getattr(result, "bit_depth", None)
                    if (
                        claimed_bit_depth is not None
                        and claimed_bit_depth >= 24
                        and (self.cfg.output_format or "").lower() in {"lossless", "lossless-24", "flac", "alac", "source", ""}
                        and candidate_path.lower().endswith((".flac", ".m4a"))
                    ):
                        try:
                            from mutagen import File as _MF
                            _audio = _MF(candidate_path)
                            _actual_bd = getattr(getattr(_audio, "info", None), "bits_per_sample", None)
                            if _actual_bd and _actual_bd < 24:
                                self._discard_file(candidate_path)
                                raise RuntimeError(
                                    f"[{adapter.name}] Quality mismatch for '{track.title}': "
                                    f"claimed {claimed_bit_depth}-bit but delivered {_actual_bd}-bit — "
                                    f"retrying with next source"
                                )
                        except RuntimeError:
                            raise
                        except Exception:
                            pass  # Probe failed — accept the file as-is
                    # Audio fingerprint check: everything above this point
                    # reads metadata, so a source that returns a completely
                    # different recording under the right tags passes them all.
                    # Compares the audio against the track's free 30s store
                    # preview; only a positive mismatch rejects the file, a
                    # missing preview or tool leaves the download untouched.
                    if self.cfg.verify_audio:
                        verify_reason = self._audio_verification_problem(candidate_path, track)
                        if verify_reason is not None:
                            self._discard_file(candidate_path)
                            raise RuntimeError(f"[{adapter.name}] {verify_reason}")
                    file_path = candidate_path
                    break
                except Exception as e:
                    if _is_mount_lost_error(e):
                        self._signal_output_lost(e)
                    else:
                        # Guarantee cleanup on EVERY failure path (v1.1.8 BUG-4).
                        # Adapters that stream straight into their final path
                        # (qobuz, qobuz_mirror, apple) leave a partial file behind
                        # when the transfer dies mid-stream; it is never tagged and
                        # was never removed, so it stayed in the library looking
                        # like a successful — but metadata-less — 30s/stub track.
                        # Skipped when the output mount vanished: the files may
                        # still be intact and simply unreachable right now.
                        self._cleanup_failed_attempt(output_base, _attempt_snapshot)
                    final_error = e
                    last_error = str(e)
                    last_source = adapter.name
                    if adapter.name not in attempted_sources:
                        attempted_sources.append(adapter.name)
                    # A download failure is more informative than the search outcome —
                    # override the chain entry for this adapter.
                    source_chain[adapter.name] = _summarize_source_error(str(e))
                    self.resolver.record_album_source_failure(track, adapter.name)
                    adapter.mark_failed_result(result, e)

                    # Rate-limited: skip to next source immediately — no sleep, no retry.
                    if isinstance(e, RateLimitedError):
                        logger.info(f"  [RATE]  {adapter.name} rate-limited — falling back to next source immediately")
                        if adapter.name in rate_limited_retried:
                            # Already gave this adapter its one retry — permanently exclude.
                            excluded_adapters.add(adapter.name)
                        else:
                            # Defer for a possible second chance after other sources are tried.
                            rate_limited_adapters.add(adapter.name)
                        break

                    will_retry = attempt < self.cfg.max_retries and adapter.should_retry_download(result, e)
                    if adapter.name == "hifi" and "all quality levels failed" in str(e).lower():
                        logger.info("  [INFO]  HiFi mirrors could not provide a valid stream. Trying next source...")
                    elif will_retry:
                        if "appears truncated" in str(e):
                            logger.info(f"  [TRUNC]  Attempt {attempt} truncated — retrying... ({e})")
                        else:
                            logger.debug(f"  [RETRY] Attempt {attempt} failed, retrying... ({e})")
                    else:
                        # Final failure for this adapter — surface it
                        logger.warning(f"  [WARN]  Attempt {attempt} failed: {e}")
                    if will_retry:
                        time.sleep(self.cfg.retry_delay)
                        continue
                    break

            # Final delivery gate (v1.1.8 BUG-6 defect B). A track must never be
            # counted as done when the file is not actually readable from Python.
            # Setting file_path = None here deliberately falls through to the
            # adapter-exclusion logic below and re-resolves against the next
            # source — the same graceful path a download failure takes. It is NOT
            # raised, because this block is not inside a try and an exception here
            # would escape download_track and take the worker with it.
            if file_path:
                delivery_problem = self._final_delivery_problem(file_path)
                if delivery_problem:
                    logger.warning(
                        f"  [WARN]  {adapter.name} delivery rejected for '{track.title}': "
                        f"{delivery_problem} — discarding and trying the next source"
                    )
                    self._discard_file(file_path)
                    self._cleanup_failed_attempt(output_base, _attempt_snapshot)
                    self.resolver.record_outcome(adapter.name, False)
                    final_error = RuntimeError(f"[{adapter.name}] {delivery_problem}")
                    # Same bookkeeping as the exception path: an adapter that is
                    # never excluded after a failure (Soulseek) would otherwise be
                    # handed this exact candidate again, forever.
                    self.resolver.record_album_source_failure(track, adapter.name)
                    adapter.mark_failed_result(result, final_error)
                    last_error = str(final_error)
                    last_source = adapter.name
                    if adapter.name not in attempted_sources:
                        attempted_sources.append(adapter.name)
                    source_chain[adapter.name] = "delivered an unreadable file"
                    file_path = None

            if file_path:
                # 4. Enrich metadata from winning adapter + free APIs + lyrics + art
                pre_enrich_snapshot = self._metadata_debug_snapshot(track)
                try:
                    from antra.core.metadata_enricher import MetadataEnricher
                    MetadataEnricher.enrich(track, result)
                except Exception:
                    self._enrich_track_metadata_if_needed(track)
                    self._enrich_genres_if_needed(track)
                self._log_pre_tag_metadata_diagnostics(
                    track,
                    result,
                    file_path,
                    adapter.name,
                    pre_enrich_snapshot,
                )
                logger.debug(
                    "  [TAG]  %s | album=%r artwork=%s lyrics=%s synced=%s genres=%s",
                    file_path,
                    track.album,
                    bool(track.artwork_url),
                    bool(track.lyrics),
                    bool(track.synced_lyrics),
                    track.genres or [],
                )
                # v2.0.0 ADV-1 (issue #43) — the advisory atom describes the
                # FILE, so the winning result's flag is handed over alongside
                # the track. MetadataEnricher only fills track.is_explicit when
                # it is None (the catalogue wins), so without this a fallback
                # to a clean master would still be tagged explicit.
                tag_ok = self.tagger.tag(
                    file_path,
                    track,
                    delivered_explicit=getattr(result, "is_explicit", None),
                )
                # DECISION (AUDIT-DESKTOP-16): the audio is valid and the cause is
                # deterministic, so the track stays COMPLETED and the file is kept;
                # what must not happen is staying SILENT about it.
                tags_problem = tag_problem_for(file_path, tag_ok)
                if tags_problem == "unsupported":
                    logger.warning(
                        f"  [WARN]  {os.path.basename(file_path)} was saved WITHOUT tags: "
                        "this file type cannot hold embedded tags."
                    )
                elif tags_problem == "failed":
                    logger.warning(
                        f"  [WARN]  {os.path.basename(file_path)} was saved WITHOUT tags: "
                        "the tag writer failed (see the error above). Re-tag it from the library once the cause is fixed."
                    )
                if self.cfg.save_cover_art_sidecar:
                    self.tagger.save_cover_art_sidecar(
                        file_path, track, folder=self.organizer.release_folder_for(file_path)
                    )
                if getattr(self.cfg, "artwork_prefs", None) and self.cfg.artwork_prefs.save_artist_image:
                    self.tagger.save_artist_image_sidecar(
                        self.organizer.artist_folder_for(file_path), track
                    )
                # v2.0.0 SF-2. After tagging, so the tagger's save cannot
                # clobber the gain, and before TRACK_COMPLETED / the DMX-6
                # track hook, so anything reacting to "done" sees the tags.
                self._apply_track_replaygain(file_path, track)

                # 5. Mark done
                self.organizer.mark_downloaded(track, file_path)
                # Persist a successful delivery so this adapter is preferred within
                # its tier on future downloads / sessions (SF-1).
                self.resolver.record_outcome(adapter.name, True)
                actual_bit_depth = None
                try:
                    audio = MutagenFile(file_path)
                    actual_bit_depth = getattr(getattr(audio, "info", None), "bits_per_sample", None)
                except Exception:
                    pass
                self.resolver.record_album_source_success(
                    track,
                    adapter.name,
                    result,
                    actual_bit_depth=actual_bit_depth,
                )

                size_mb = os.path.getsize(file_path) / (1024 * 1024) if os.path.exists(file_path) else 0
                attempt_time = getattr(self, "_last_attempt_start", time.time())
                elapsed = time.time() - attempt_time
                
                logger.info(
                    f"  \u2728 [Complete] [{track_index}/{track_total}] {track.title} by {track.artist_string}"
                )
                if used_lossy_fallback:
                    logger.warning(
                        f"  \u26a0\ufe0f  [{track.title}] No lossless source available — "
                        f"downloaded as {result.quality_label} from {adapter.name}. "
                        f"Not true lossless."
                    )
                completed_audio_format = self._audio_format_from_path(file_path) or result.audio_format
                completed_quality_label = self._quality_label_from_file(
                    file_path,
                    completed_audio_format,
                    result.quality_label,
                    source_kbps=source_lossy_kbps,
                )
                self._emit(
                    EngineEventType.TRACK_COMPLETED,
                    track=track,
                    track_index=track_index,
                    track_total=track_total,
                    source=adapter.name,
                    file_path=file_path,
                    quality_label=completed_quality_label,
                    tags_problem=tags_problem,
                )
                return DownloadResult(
                    track=track,
                    status=DownloadStatus.COMPLETED,
                    file_path=file_path,
                    source_used=adapter.name,
                    audio_format=completed_audio_format,
                    tags_problem=tags_problem,
                )

            # Rate-limited adapters already placed in rate_limited_adapters above — skip
            # the regular exclude logic so they don't also land in excluded_adapters.
            if isinstance(final_error, RateLimitedError):
                # Reliability signal: the adapter had the track but is overloaded (SF-1).
                self.resolver.record_outcome(adapter.name, False)
                continue

            # Truncated downloads: the adapter found the track but the stream ended early
            # (network blip, proxy cut it off). Don't permanently exclude — instead:
            # 1. Mark the adapter as globally rate-limited in the resolver (120s cooldown)
            #    so ALL parallel workers immediately start preferring other adapters.
            #    Without this, workers running in parallel each independently queue on the
            #    broken adapter, discovering the truncation one at a time.
            # 2. Defer the adapter to the end of this track's queue (rate_limited_adapters)
            #    so Amazon/HiFi get a fair shot first; the adapter gets one last retry if
            #    nothing else works (useful when the adapter is the only one that can find
            #    the track, e.g. featured-artist titles that defeat Amazon/HiFi search).
            if final_error is not None and "appears truncated" in str(final_error):
                # Signal all parallel workers to stop queuing on this adapter.
                self.resolver._mark_rate_limited(adapter.name, cooldown_seconds=120)
                # Reliability signal: a truncated/preview stream is a delivery failure (SF-1).
                self.resolver.record_outcome(adapter.name, False)

                if adapter.name in rate_limited_retried:
                    # Already had its second chance and still truncated — give up.
                    excluded_adapters.add(adapter.name)
                    logger.info(f"  [NEXT]  {adapter.name} truncated on second attempt — no more retries")
                else:
                    logger.info(
                        f"  [TRUNC]  {adapter.name} truncated — trying other sources first, "
                        f"will retry {adapter.name} as last resort if nothing else works"
                    )
                    rate_limited_adapters.add(adapter.name)
                continue

            should_exclude = True
            if final_error is not None:
                should_exclude = adapter.should_exclude_adapter_after_failure(result, final_error)

            if should_exclude:
                excluded_adapters.add(adapter.name)
                if (
                    adapter.name == "amazon"
                    and getattr(track, "amazon_asin", None)
                    and self._is_lossy_output_mode()
                ):
                    logger.info("  [NEXT]  Amazon could not provide a usable file — trying YouTube fallback...")
                else:
                    logger.info(f"  [NEXT]  {adapter.name} failed after retries, trying next source...")
                # Circuit breaker: if the failure looks like a server-side 5xx
                # (not a missing track or auth issue), count consecutive failures.
                # After 3 in a row, rate-limit the adapter globally for 5 minutes
                # so it is skipped for all subsequent tracks in this session.
                if final_error is not None and _is_server_error(final_error):
                    # Reliability signal: server-side 5xx delivery failure (SF-1).
                    self.resolver.record_outcome(adapter.name, False)
                    with self._adapter_server_errors_lock:
                        count = self._adapter_server_errors.get(adapter.name, 0) + 1
                        self._adapter_server_errors[adapter.name] = count
                    if count >= 3:
                        logger.warning(
                            f"  [CIRCUIT]  {adapter.name} has failed with server errors "
                            f"{count} times — marking unavailable for 5 minutes."
                        )
                        self.resolver._mark_rate_limited(adapter.name, cooldown_seconds=300)
                        with self._adapter_server_errors_lock:
                            self._adapter_server_errors[adapter.name] = 0
                else:
                    # Non-server-error failure (404, auth, no match) resets the counter.
                    with self._adapter_server_errors_lock:
                        self._adapter_server_errors.pop(adapter.name, None)
            else:
                logger.info(f"  [NEXT]  {adapter.name} candidate failed, trying another match from the same source...")

    # ------------------------------------------------------------------
    # Resolve prefetch
    #
    # A download worker used to run resolve -> download -> tag inline, so a
    # worker busy searching the mirrors was not moving any bytes. With N workers
    # the actual number of concurrent transfers was therefore
    # N x download_time / (resolve_time + download_time) — visibly fewer than N.
    #
    # These helpers resolve upcoming tracks on a small side pool so the download
    # workers find their answer already waiting. The retry/exclusion loop in
    # download_track is untouched: a prefetched answer is only ever used for the
    # FIRST resolve of a track, which is the only point at which the excluded set
    # is empty and a precomputed result is therefore still valid.
    #
    # Safe to hold: no adapter returns a signed URL from search() (all 22 call
    # sites pass download_url=None), so a resolution is just "which adapter,
    # which id" and cannot go stale while it waits in the map.
    # ------------------------------------------------------------------

    def _take_prefetched(self, track_index: Optional[int]) -> Optional[tuple]:
        """Consume the prefetched resolution for a track, if one is ready."""
        if not track_index:
            return None
        with self._prefetch_lock:
            entry = self._prefetched.pop(track_index, None)
            # Drop anything left behind the pack. A worker that resolved a track
            # itself (because it got there first) would otherwise leave an entry
            # nobody can ever claim, silently eating the lookahead budget and
            # throttling the prefetcher down to nothing.
            stale = [k for k in self._prefetched if k < track_index - self._prefetch_slack]
            for k in stale:
                self._prefetched.pop(k, None)
            return entry

    def _prefetch_worker(self, tracks: list[TrackMetadata], counter: list, lookahead: int) -> None:
        while not self._prefetch_stop.is_set():
            if self._output_lost.is_set():
                return
            if self.controller:
                self.controller.wait_if_paused()
                if self.controller.is_cancelled():
                    return

            # Back-pressure and the index claim share one lock acquisition. Split
            # across two, several threads pass the depth check together and each
            # then adds an entry, overshooting the bound by one per thread.
            with self._prefetch_lock:
                # Count in-flight resolves against the budget too. Checking only
                # the map lets every thread claim while the map is still short,
                # and the bound is then overshot by one per thread once they all
                # store their result.
                if len(self._prefetched) + self._prefetch_inflight >= lookahead:
                    index = None
                else:
                    index = counter[0]
                    counter[0] += 1
                    self._prefetch_inflight += 1
            if index is None:
                time.sleep(0.15)
                continue
            if index >= len(tracks):
                with self._prefetch_lock:
                    self._prefetch_inflight -= 1
                return

            track = tracks[index]
            # try/finally around the whole claim: every path out of here —
            # already-downloaded, resolve error, no match — must release the
            # in-flight slot, or the budget drains and prefetching stops dead.
            try:
                try:
                    # A track already on disk is skipped before it ever resolves,
                    # so resolving it here would be pure waste.
                    if self.organizer.is_already_downloaded(track):
                        continue
                except Exception:
                    pass

                try:
                    resolution = self.resolver.resolve(track, excluded_adapters=set())
                    # last_resolve_report() is thread-local, so it must be
                    # captured here, in the thread that did the resolving.
                    # Reading it later from the download worker would return
                    # that worker's own report, which never ran a search.
                    report = dict(self.resolver.last_resolve_report() or {})
                except Exception as exc:
                    logger.debug(f"  [PREFETCH]  resolve failed for '{track.title}': {exc}")
                    continue

                # Deliberately only cache a hit. A miss is often transient (a
                # mirror briefly unreachable), and caching it would fail the
                # track without the fresh attempt it would have had before.
                if resolution is None:
                    continue

                with self._prefetch_lock:
                    self._prefetched[index + 1] = (resolution, report)
            finally:
                with self._prefetch_lock:
                    self._prefetch_inflight -= 1

    def _start_prefetch(self, tracks: list[TrackMetadata]) -> None:
        threads = max(0, int(getattr(self.cfg, "prefetch_resolves", 0) or 0))
        if threads <= 0 or len(tracks) < 2:
            return
        threads = min(threads, len(tracks) - 1)
        workers = max(1, int(self.cfg.max_workers))
        lookahead = max(2, workers + 2)

        # Start PAST the tracks the download workers claim the instant the pool
        # opens. Starting at 0 makes the prefetcher race the workers for the same
        # first N tracks, lose (they are already resolving inline), and throw all
        # of that work away — measured as exactly zero cache hits.
        start = min(workers, len(tracks) - 1)
        self._prefetch_slack = workers

        self._prefetch_stop.clear()
        with self._prefetch_lock:
            self._prefetched.clear()
            self._prefetch_inflight = 0

        counter = [start]
        self._prefetch_pool = ThreadPoolExecutor(
            max_workers=threads, thread_name_prefix="antra-prefetch"
        )
        for _ in range(threads):
            self._prefetch_pool.submit(self._prefetch_worker, tracks, counter, lookahead)
        logger.info(
            f"  [PREFETCH]  Resolving ahead on {threads} thread(s), "
            f"queue depth {lookahead} — download slots stay busy."
        )

    def _stop_prefetch(self) -> None:
        self._prefetch_stop.set()
        pool, self._prefetch_pool = self._prefetch_pool, None
        if pool is not None:
            # Do not block on in-flight resolves: a mirror search can sit on a
            # long timeout, and the user cancelling must not wait for it.
            pool.shutdown(wait=False)
        with self._prefetch_lock:
            self._prefetched.clear()

    def _warn_if_platform_pin_unavailable(self, tracks: list[TrackMetadata]) -> None:
        """Emit ONE notice when the pasted platform cannot serve this download.

        `source_preference` narrows the adapter chain before the resolver is even
        constructed (ALAC -> Apple only), so a platform pin can be structurally
        impossible for the whole job. The resolver fails open rather than failing
        every track, which is the right outcome — but silently substituting a
        different service looks exactly like the platform setting being ignored.
        """
        if not tracks:
            return
        service = getattr(tracks[0], "source_service", None)
        resolver = getattr(self, "resolver", None)
        checker = getattr(resolver, "platform_pin_is_available", None)
        if not service or checker is None:
            return
        try:
            if checker(service):
                return
            available = ", ".join(
                dict.fromkeys(resolver._service_label(a.name) for a in resolver.adapters)
            ) or "no other source"
            pinned = resolver._service_label(service)
        except Exception:  # advisory only — never let it fail a download
            return
        self._emit(
            EngineEventType.NOTICE,
            message=(
                f"{pinned} cannot be used for this format or plan — the available "
                f"source is {available}. These tracks will be matched on "
                f"{available} instead."
            ),
        )

    def _write_sidecars(self, results: list[DownloadResult]) -> None:
        """Write .cue / .nfo / .json beside the audio, from the files on disk.

        SKIPPED counts, and that is deliberate: on the desktop a skipped track's
        `file_path` points at a real, already-owned file that is part of the
        same release, so excluding it would make a re-run of a mostly-complete
        album emit a cue describing only the track that happened to be missing.

        Nothing here may fail a download that already succeeded, hence the bare
        except — same rule as the cover-art sidecar and the provenance tags.
        """
        if not (self.cfg.sidecar_cue or self.cfg.sidecar_nfo or self.cfg.sidecar_json):
            return
        try:
            from antra.utils import sidecars as _sidecars

            entries = [
                (r.track, r.file_path)
                for r in results
                if r.file_path
                and r.status in (DownloadStatus.COMPLETED, DownloadStatus.SKIPPED)
            ]
            if not entries:
                return
            _sidecars.write_release_sidecars(
                entries,
                want_cue=self.cfg.sidecar_cue,
                want_nfo=self.cfg.sidecar_nfo,
                want_json=self.cfg.sidecar_json,
                download_format=self.cfg.output_format or "",
            )
        except Exception as e:
            logger.warning(f"  [WARN]  Could not write sidecar files: {e}")

    def _replaygain_active(self) -> bool:
        if not getattr(self.cfg, "replaygain", False):
            return False
        # Spatial mixes are skipped: players do not apply ReplayGain to them,
        # and a stereo-referenced gain on a 5.1 EC-3 bed is not meaningful.
        return not (self.cfg.output_format or "").lower().startswith("atmos")

    def _apply_track_replaygain(self, file_path: str, track: TrackMetadata) -> None:
        """Measure one delivered file and write its track gain/peak.

        Contained: any failure is logged and the download stands.
        """
        if not self._replaygain_active():
            return
        try:
            from antra.utils import replaygain as rg

            if os.path.splitext(file_path)[1].lower() not in rg.SUPPORTED_EXTENSIONS:
                logger.debug(f"  [RG]  Skipped (container not tagged): {file_path}")
                return
            loud = rg.measure(file_path)
            if loud is None:
                logger.info(f"  [RG]  No loudness measured for {track.title} - ReplayGain not written")
                return
            if not rg.write_tags(
                file_path,
                track=(loud.gain_db, loud.peak),
                clear_album=True,
                id3_v1=self.tagger._id3_v1,
            ):
                logger.info(f"  [RG]  Could not write ReplayGain tags for {track.title}")
                return
            # Only release tracks are kept for the album pass. Playlist rows are
            # never grouped, and holding ~10 blocks/second for a 2,000-track
            # playlist would be memory spent on nothing.
            if not track.playlist_name:
                with self._rg_lock:
                    self._rg_measurements[file_path] = loud
            logger.debug(
                f"  [RG]  {track.title}: {loud.integrated_lufs:.2f} LUFS -> "
                f"{rg.format_gain(loud.gain_db)}, peak {rg.format_peak(loud.peak)}"
            )
        except Exception as e:
            logger.warning(f"  [WARN]  ReplayGain analysis failed for {track.title}: {e}")

    @staticmethod
    def _rg_album_key(track: TrackMetadata) -> tuple:
        if track.album_id:
            return ("id", str(track.album_id))
        album_artist = (track.album_artists[0] if track.album_artists
                        else (track.artists[0] if track.artists else ""))
        return ("name", (track.album or "").strip().lower(), album_artist.strip().lower())

    def _apply_album_replaygain(self, results: list[DownloadResult]) -> None:
        """Write album gain/peak for every release this batch delivered in full.

        A release gets album tags only when EVERY track of it in this batch
        was downloaded and measured, and the batch covers the whole release
        (track count >= total_tracks when the catalogue states it, or an album
        request when it does not). Anything less - a skipped track already in
        the library, a failure, one track pulled from a 12-track album - is
        left with track gain only: an album gain computed from part of an
        album is a wrong number, and players fall back to track gain when the
        album tags are absent. Playlist rows are never grouped: a playlist is
        not a release.
        """
        if not self._replaygain_active():
            return
        with self._rg_lock:
            measured = dict(self._rg_measurements)
            self._rg_measurements.clear()
        if not measured:
            return
        try:
            from antra.utils import replaygain as rg

            groups: dict[tuple, list[DownloadResult]] = {}
            for r in results:
                if r.track is None or r.track.playlist_name:
                    continue
                groups.setdefault(self._rg_album_key(r.track), []).append(r)

            for rows in groups.values():
                title = rows[0].track.album or "(untitled release)"
                loud = [measured.get(r.file_path) if r.file_path else None for r in rows]
                incomplete = [
                    r for r, m in zip(rows, loud)
                    if r.status != DownloadStatus.COMPLETED or m is None
                ]
                if incomplete:
                    if any(r.status == DownloadStatus.COMPLETED for r in rows):
                        logger.info(
                            f"  [RG]  Album gain not written for {title}: "
                            f"{len(rows) - len(incomplete)} of {len(rows)} tracks were "
                            "downloaded and measured in this run"
                        )
                    continue
                total = max((r.track.total_tracks or 0) for r in rows)
                whole = (len(rows) >= total) if total > 0 else any(
                    (r.track.request_kind or "") == "album" for r in rows
                )
                if not whole:
                    logger.info(
                        f"  [RG]  Album gain not written for {title}: this run covered "
                        f"{len(rows)} of {total or 'an unknown number of'} tracks"
                    )
                    continue
                album = rg.album_loudness(loud)
                if album is None:
                    continue
                gain = rg.gain_for(album[0])
                wrote = sum(
                    1 for r in rows
                    if rg.write_tags(r.file_path, album=(gain, album[1]), id3_v1=self.tagger._id3_v1)
                )
                logger.info(
                    f"  [RG]  {title}: album {album[0]:.2f} LUFS -> {rg.format_gain(gain)}, "
                    f"peak {rg.format_peak(album[1])} ({wrote}/{len(rows)} files)"
                )
        except Exception as e:
            logger.warning(f"  [WARN]  Album ReplayGain failed: {e}")

    # ── issue #75 — playlist folder maintenance ───────────────────────────

    def _handle_playlist_orphans(self, playlist_name, results, complete_playlist: bool) -> None:
        """Report (or, in auto mode, delete) files the playlist no longer holds.

        Runs only on a complete playlist. See download_playlist's docstring for
        why the partial case must never reach this.
        """
        if not complete_playlist:
            return
        mode = getattr(self.organizer, "playlist_prune_orphans", "ask")
        if mode == "off":
            return
        try:
            orphans = self.organizer.find_playlist_orphans(
                playlist_name, [r.file_path for r in results if r.file_path]
            )
        except Exception as e:
            logger.warning(f"  [PRUNE]  Could not check for leftover files: {e}")
            return
        if not orphans:
            return

        pruned = False
        if mode == "auto":
            removed = self.organizer.prune_playlist_orphans(orphans)
            logger.info(
                f"  [PRUNE]  Removed {len(removed)} file(s) no longer in '{playlist_name}'"
            )
            orphans, pruned = removed, True
        else:
            logger.info(
                f"  [PRUNE]  {len(orphans)} file(s) in '{playlist_name}' are no longer "
                "in the playlist"
            )
        if not orphans:
            return
        self._emit(
            EngineEventType.PLAYLIST_ORPHANS,
            message=playlist_name,
            paths=orphans,
            pruned=pruned,
        )

    def _renumber_playlist(self, playlist_name, results, complete_playlist: bool) -> None:
        """Rename playlist files to their current positions, in place in `results`.

        Rewriting `results` matters as much as the rename: the manifest, the
        summary and the post-download hooks all read `file_path` afterwards, and
        a path that was renamed a moment ago no longer exists.
        """
        if not complete_playlist:
            return
        entries = [(r.track, r.file_path) for r in results if r.file_path and r.track]
        if not entries:
            return
        try:
            renamed = self.organizer.renumber_playlist_folder(playlist_name, entries)
        except Exception as e:
            logger.warning(f"  [RENUM]  Could not renumber '{playlist_name}': {e}")
            return
        if not renamed:
            return
        lookup = {os.path.normcase(old): new for old, new in renamed.items()}
        for r in results:
            if not r.file_path:
                continue
            new = lookup.get(os.path.normcase(str(r.file_path)))
            if new:
                r.file_path = new

    def _common_release_folder(self, results) -> str:
        """The one release folder this batch wrote into, or "" if it spans several.

        {release_folder} is only meaningful for a batch when every delivered
        file landed in the same folder - an album. A playlist spanning 40
        albums has no single release folder, and naming one of the 40 would
        be worse than naming none, so it renders empty and the argv element
        holding it is dropped.
        """
        folders = set()
        for r in results:
            if not getattr(r, "file_path", None):
                continue
            try:
                folders.add(self.organizer.release_folder_for(r.file_path) or "")
            except Exception:
                return ""
            if len(folders) > 1:
                return ""
        return next(iter(folders), "") if len(folders) == 1 else ""

    def download_playlist(
        self, tracks: list[TrackMetadata], *, complete_playlist: bool = False
    ) -> list[DownloadResult]:
        """Download all tracks in a playlist in parallel, returning results in original order.

        `complete_playlist` says whether `tracks` is the WHOLE playlist. It gates
        the issue #75 folder maintenance, and it defaults to False because
        getting it wrong the other way is destructive: on a partial run (a PV-1
        selection, an auto-sync that fetched only new tracks, a single-track
        retry) every track the run did not cover looks like an orphan.
        """
        total = len(tracks)
        playlist_name = tracks[0].playlist_name if tracks and tracks[0].playlist_name else None
        self._emit(
            EngineEventType.PLAYLIST_STARTED,
            track_total=total,
            message=f"Starting playlist download for {total} track(s).",
        )

        # Tell the user UP FRONT when the platform their link came from cannot
        # be used for this format. The chain is fixed for the life of the job,
        # so this is knowable before the first track.
        self._warn_if_platform_pin_unavailable(tracks)

        # results[i] will hold the DownloadResult for tracks[i]
        results: list[Optional[DownloadResult]] = [None] * total

        def _worker(index: int, track: TrackMetadata) -> tuple[int, DownloadResult]:
            # Abort immediately if the output filesystem was lost by a previous worker.
            if self._output_lost.is_set():
                return index, DownloadResult(
                    track=track,
                    status=DownloadStatus.FAILED,
                    error_message=self._output_lost_message,
                )
            if self.controller:
                self.controller.wait_if_paused()
                if self.controller.is_cancelled():
                    return index, DownloadResult(
                        track=track,
                        status=DownloadStatus.CANCELLED,
                        error_message="Cancelled",
                    )
            logger.info(f"[{index + 1}/{total}] {track.artist_string} — {track.title}")
            self._emit(
                EngineEventType.TRACK_STARTED,
                track=track,
                track_index=index + 1,
                track_total=total,
            )
            return index, self.download_track(track, track_index=index + 1, track_total=total)

        workers = max(1, self.cfg.max_workers)
        # Resolve ahead of the downloaders so a worker's slot is spent moving
        # bytes rather than searching. try/finally, not a bare call after the
        # pool: the side threads must not outlive this call on any exit path.
        self._start_prefetch(tracks)
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_worker, i, track): i for i, track in enumerate(tracks)}
                for future in as_completed(futures):
                    if self.controller and self.controller.is_cancelled():
                        self._stop_prefetch()
                        pool.shutdown(wait=False, cancel_futures=True)
                        break
                    try:
                        idx, result = future.result()
                        results[idx] = result
                    except OSError as e:
                        idx = futures[future]
                        if _is_mount_lost_error(e):
                            self._signal_output_lost(e)
                        results[idx] = DownloadResult(
                            track=tracks[idx],
                            status=DownloadStatus.FAILED,
                            error_message=(
                                self._output_lost_message
                                if self._output_lost.is_set()
                                else str(e)
                            ),
                        )
                    except Exception as e:
                        idx = futures[future]
                        logger.warning(f"Worker for track {idx + 1} raised unexpectedly: {e}")
                        results[idx] = DownloadResult(
                            track=tracks[idx],
                            status=DownloadStatus.FAILED,
                            error_message=str(e),
                        )
        finally:
            self._stop_prefetch()

        # Fill any slots that were cancelled or never completed
        final: list[DownloadResult] = []
        for i, r in enumerate(results):
            if r is None:
                r = DownloadResult(
                    track=tracks[i],
                    status=DownloadStatus.CANCELLED,
                    error_message="Cancelled",
                )
            final.append(r)

        if self.controller and self.controller.is_cancelled():
            if playlist_name and final:
                self.organizer.write_playlist_manifest(
                    playlist_name,
                    [r.file_path for r in final if r.file_path],
                )
            self._emit(
                EngineEventType.PLAYLIST_CANCELLED,
                track_total=total,
                message="Playlist download cancelled.",
            )
            return final

        if playlist_name:
            # issue #75 — prune first, then renumber, then write the manifest.
            # The order is load-bearing: an orphan still holding "06 - X.mp3"
            # would block the rename that wants that name, and the manifest has
            # to be written from the FINAL paths or it points at files that no
            # longer exist under those names.
            self._handle_playlist_orphans(playlist_name, final, complete_playlist)
            self._renumber_playlist(playlist_name, final, complete_playlist)
            self.organizer.write_playlist_manifest(
                playlist_name,
                [r.file_path for r in final if r.file_path],
            )

        # v2.0.0 SF-2. Before the sidecars and the batch hook, so a beets
        # import or rescan fired by DMX-6 sees the album tags. Not on the
        # cancelled path: a half-finished album has no album loudness.
        self._apply_album_replaygain(final)

        # v2.0.0 PORT-2. Deliberately NOT on the cancelled path above: an .m3u
        # of a half-finished run is still a usable list of files, but a .cue or
        # .nfo describing 3 of 20 tracks claims to describe the release and does
        # not. The user re-runs and gets the real thing.
        self._write_sidecars(final)

        # v2.0.0 DMX-6, batch mode. Deliberately on the SAME path as the
        # sidecars and NOT on the cancelled return above: a rescan or a
        # beets import fired over a half-finished run describes a release
        # that does not exist yet. {count} counts only tracks that produced
        # a new file, so a run where everything was already in the library
        # still reports 0 rather than claiming work was done.
        _ok = sum(1 for r in final if r.status == DownloadStatus.COMPLETED)
        self.post_command.run_for_batch(
            downloaded=_ok,
            failed=sum(1 for r in final if r.status == DownloadStatus.FAILED),
            total=total,
            playlist_name=playlist_name or "",
            release_folder=self._common_release_folder(final),
        )

        self._emit(
            EngineEventType.PLAYLIST_COMPLETED,
            track_total=total,
            message=f"Processed {len(final)} track(s).",
        )

        # If mount loss was detected, raise so json_cli surfaces the error in
        # the playlist_summary and subsequent URLs are also skipped cleanly.
        if self._output_lost.is_set():
            raise OSError(self._output_lost_message)

        return final
