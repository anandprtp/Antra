"""
Reusable application service for CLI and future desktop frontends.
"""
import logging
from dataclasses import dataclass, replace
from typing import Callable, Optional

from antra.core.config import Config, load_config
from antra.core.control import DownloadController
from antra.core.engine import DownloadEngine, EngineConfig
from antra.core.events import EngineEvent
from antra.core.spotify import SpotifyResourceError
from antra.core.models import (
    BulkDownloadProgress,
    BulkDownloadReport,
    DownloadResult,
    PlaylistFailure,
    SpotifyLibrary,
    SpotifyPlaylistSummary,
    TrackMetadata,
)
from antra.core.resolver import SourceResolver
from antra.core.spotify import SpotifyClient
from antra.utils.lyrics import LyricsFetcher
from antra.utils.organizer import LibraryOrganizer

logger = logging.getLogger(__name__)

SOURCE_PREFERENCE_CHOICES = ("auto", "hifi", "amazon", "soulseek", "jiosaavn")
OUTPUT_FORMAT_CHOICES = ("source", "flac", "m4a", "aac", "mp3")
SPECIAL_SOURCE_PREFERENCE_CHOICES = ("priority-2", "priority-3", "priority-4")
SPECIAL_OUTPUT_FORMAT_CHOICES = ("lossless",)
LEGACY_SOURCE_PREFERENCE_ALIASES = {}
LEGACY_OUTPUT_FORMAT_ALIASES = {"flac-16": "flac", "flac-24": "flac"}


_AUTH_ERROR_KEYWORDS = (
    "not authenticated",
    "no credentials",
    "unauthorized",
    "auth",
    "token",
    "login",
    "credentials",
    "client_id",
    "client_secret",
    "401",
    "403",
)


def _is_auth_error(exc: Exception) -> bool:
    """Return True if the exception looks like a Spotify auth/credential failure."""
    msg = str(exc).lower()
    return any(kw in msg for kw in _AUTH_ERROR_KEYWORDS)


def _split_config_urls(value: str) -> list[str]:
    parts = []
    for raw in value.replace("\n", ",").replace(";", ",").split(","):
        cleaned = raw.strip()
        if cleaned:
            parts.append(cleaned)
    return parts


def normalize_source_preference(value: Optional[str]) -> str:
    normalized = LEGACY_SOURCE_PREFERENCE_ALIASES.get(value or "", value or "")
    if normalized in SOURCE_PREFERENCE_CHOICES or normalized in SPECIAL_SOURCE_PREFERENCE_CHOICES:
        return normalized
    return "auto"


def normalize_output_format(value: Optional[str]) -> str:
    normalized = LEGACY_OUTPUT_FORMAT_ALIASES.get(value or "", value or "")
    if normalized in OUTPUT_FORMAT_CHOICES or normalized in SPECIAL_OUTPUT_FORMAT_CHOICES:
        return normalized
    return "source"


def describe_source_preference(value: Optional[str]) -> str:
    normalized = normalize_source_preference(value)
    labels = {
        "auto": "auto",
        "priority-2": "hifi / dab -> soulseek -> jiosaavn",
        "priority-3": "jiosaavn",
        "priority-4": "jiosaavn",
    }
    return labels.get(normalized, normalized)


def describe_output_format(value: Optional[str]) -> str:
    normalized = normalize_output_format(value)
    labels = {
        "source": "source",
        "lossless": "flac / m4a",
    }
    return labels.get(normalized, normalized)


@dataclass
class RuntimeOptions:
    output_dir: Optional[str] = None
    fetch_lyrics: Optional[bool] = None
    enrich_album_data: Optional[bool] = None
    source_preference: Optional[str] = None
    output_format: Optional[str] = None


class AntraService:
    """Coordinates config, Spotify metadata, adapters, and downloads."""

    def __init__(
        self,
        config: Optional[Config] = None,
        spotify_client_factory: Optional[Callable[..., SpotifyClient]] = None,
    ):
        self._base_config = config or load_config()
        self._spotify_client_factory = spotify_client_factory or SpotifyClient

    def build_runtime_config(self, options: Optional[RuntimeOptions] = None) -> Config:
        cfg = replace(self._base_config)
        cfg.source_preference = normalize_source_preference(cfg.source_preference)
        cfg.output_format = normalize_output_format(cfg.output_format)
        if not options:
            return cfg

        if options.output_dir:
            cfg.output_dir = options.output_dir
        if options.fetch_lyrics is not None:
            cfg.fetch_lyrics = options.fetch_lyrics
        if options.enrich_album_data is not None:
            cfg.enrich_album_data = options.enrich_album_data
        if options.source_preference is not None:
            cfg.source_preference = normalize_source_preference(options.source_preference)
        if options.output_format is not None:
            cfg.output_format = normalize_output_format(options.output_format)
        return cfg

    @staticmethod
    def _filter_adapters_by_source_preference(adapters: list, source_preference: Optional[str]) -> list:
        normalized = normalize_source_preference(source_preference)
        if not normalized or normalized == "auto":
            return adapters
        if normalized == "soulseek":
            preferred_order = ["soulseek", "hifi", "jiosaavn"]
            by_name = {adapter.name: adapter for adapter in adapters}
            return [by_name[name] for name in preferred_order if name in by_name]
        if normalized == "priority-2":
            allowed = {"hifi", "amazon", "soulseek", "jiosaavn"}
            return [adapter for adapter in adapters if adapter.name in allowed]
        if normalized == "priority-3":
            allowed = {"jiosaavn"}
            return [adapter for adapter in adapters if adapter.name in allowed]
        if normalized == "priority-4":
            allowed = {"jiosaavn"}
            return [adapter for adapter in adapters if adapter.name in allowed]
        return [adapter for adapter in adapters if adapter.name == normalized]

    @staticmethod
    def validate_config(cfg: Config):
        # We no longer strictly require spotify_client_id/secret for basic usage
        # because the fallback public web scrapers handle anonymous usage.
        pass

    def build_adapters(self, cfg: Config) -> list:
        """Build and return all configured source adapters."""
        adapters: list = []

        # Amazon Music (free FLAC via community proxy — highest priority, no account needed)
        if cfg.amazon_enabled and cfg.amazon_mirrors:
            try:
                from antra.sources.amazon import AmazonAdapter
                amazon = AmazonAdapter(
                    mirrors=cfg.amazon_mirrors,
                    api_key=cfg.odesli_api_key or None,
                )
                if amazon.is_available():
                    adapters.append(amazon)
                    logger.info("[OK] Amazon adapter enabled (free FLAC via community proxy)")
            except Exception as e:
                logger.warning(f"Amazon adapter failed to initialize: {e}")

        # HiFi (free FLAC via community Tidal proxy — no account needed)
        try:
            from antra.sources.hifi import HifiAdapter
            hifi = HifiAdapter()
            if hifi.is_available():
                adapters.append(hifi)
                logger.info("[OK] HiFi adapter enabled (free FLAC via Tidal proxy)")
        except Exception as e:
            logger.warning(f"HiFi adapter failed to initialize: {e}")

        # Soulseek via slskd
        soulseek_base_url = (getattr(cfg, "soulseek_base_url", "") or "").strip()
        soulseek_api_key = getattr(cfg, "soulseek_api_key", "") or ""
        soulseek_username = (getattr(cfg, "soulseek_username", "") or "").strip()
        soulseek_password = (getattr(cfg, "soulseek_password", "") or "").strip()
        if not soulseek_base_url and getattr(cfg, "soulseek_auto_bootstrap", True):
            if not soulseek_username or not soulseek_password:
                logger.info(
                    "[Soulseek] Managed bootstrap skipped — add your Soulseek username and password in Settings to enable the Soulseek source."
                )
            else:
                try:
                    from antra.utils.slskd_manager import SlskdBootstrapManager

                    managed = SlskdBootstrapManager().ensure_running(
                        username=soulseek_username,
                        password=soulseek_password,
                    )
                    if managed:
                        soulseek_base_url = managed.get("base_url", soulseek_base_url)
                        if not soulseek_api_key:
                            soulseek_api_key = managed.get("api_key", "") or ""
                        logger.info("[OK] Managed slskd bootstrap is ready.")
                    else:
                        logger.warning("Managed slskd bootstrap did not start successfully.")
                except Exception as e:
                    logger.warning(f"Managed slskd bootstrap failed: {e}")

        if soulseek_base_url:
            try:
                from antra.sources.soulseek import SoulseekAdapter

                soulseek = SoulseekAdapter(
                    base_url=soulseek_base_url,
                    api_key=soulseek_api_key or None,
                )
                if soulseek.is_available():
                    adapters.append(soulseek)
                    logger.info("[OK] Soulseek adapter enabled (via slskd)")
                else:
                    logger.warning(
                        "Soulseek adapter not available — check slskd connection/API key."
                    )
            except Exception as e:
                logger.warning(f"Soulseek adapter failed to initialize: {e}")

        # Qobuz (user credentials — lossless FLAC)
        if cfg.qobuz_email and cfg.qobuz_password and "@example.com" not in cfg.qobuz_email:
            try:
                from antra.sources.qobuz import QobuzAdapter

                adapter = QobuzAdapter(
                    email=cfg.qobuz_email,
                    password=cfg.qobuz_password,
                    app_id=cfg.qobuz_app_id,
                    app_secret=cfg.qobuz_app_secret,
                )
                if adapter.is_available():
                    adapters.append(adapter)
                    logger.info("[OK] Qobuz adapter enabled")
            except Exception as e:
                logger.warning(f"Qobuz adapter failed to initialize: {e}")

        # Tidal
        if cfg.tidal_email and cfg.tidal_password:
            try:
                from antra.sources.tidal import TidalAdapter

                adapter = TidalAdapter(email=cfg.tidal_email, password=cfg.tidal_password)
                if adapter.is_available():
                    adapters.append(adapter)
                    logger.info("[OK] Tidal adapter enabled")
            except Exception as e:
                logger.warning(f"Tidal adapter failed to initialize: {e}")

        # JioSaavn (no credentials needed — good MP3 320kbps fallback)
        if cfg.jiosaavn_enabled:
            try:
                from antra.sources.jiosaavn import JioSaavnAdapter

                adapter = JioSaavnAdapter(quality=cfg.jiosaavn_quality)
                if adapter.is_available():
                    adapters.append(adapter)
                    logger.info(f"[OK] JioSaavn adapter enabled ({cfg.jiosaavn_quality}kbps)")
            except Exception as e:
                logger.warning(f"JioSaavn adapter failed to initialize: {e}")


        source_preference = getattr(cfg, "source_preference", None)
        filtered = self._filter_adapters_by_source_preference(adapters, source_preference)
        if source_preference and source_preference != "auto":
            logger.info(f"[OK] Source preference selected: {describe_source_preference(source_preference)}")
            if not filtered and adapters:
                logger.warning(
                    "Selected source preference has no available adapters right now; "
                    "falling back to auto source chain."
                )
                return adapters
        return filtered

    def fetch_playlist_tracks(
        self,
        playlist: str,
        options: Optional[RuntimeOptions] = None,
    ) -> list[TrackMetadata]:
        cfg = self.build_runtime_config(options)
        self.validate_config(cfg)

        # Handle Apple Music URLs
        if "music.apple.com" in playlist:
            return self._fetch_apple_tracks(playlist, cfg)

        # Handle SoundCloud URLs
        if "soundcloud.com" in playlist:
            return self._fetch_soundcloud_tracks(playlist, cfg)

        # Handle Amazon Music URLs
        if "music.amazon.com" in playlist:
            return self._fetch_amazon_music_tracks(playlist, cfg)

        # Try Spotify first; fall back to SpotFetch if credentials are missing
        # or if the anonymous public-page scraper fails (Spotify changed their
        # web app and no longer embeds __NEXT_DATA__ JSON).
        try:
            spotify = self._make_spotify_client(cfg)
            tracks = self._fetch_tracks_with_client(spotify, playlist, cfg)
            return tracks
        except SpotifyResourceError as e:
            logger.info(
                f"[SpotFetch] Spotify metadata unavailable ({e}) — falling back to SpotFetch proxy"
            )
            return self._fetch_spotfetch_tracks(playlist, cfg)
        except Exception as e:
            if _is_auth_error(e):
                logger.info(
                    "[SpotFetch] Spotify auth not configured — falling back to SpotFetch proxy"
                )
                return self._fetch_spotfetch_tracks(playlist, cfg)
            raise

    def _fetch_apple_tracks(self, url: str, cfg: Config) -> list[TrackMetadata]:
        try:
            from antra.core.apple_fetcher import AppleFetcher
        except ImportError:
            raise RuntimeError(
                "Apple Music playlist fetching is not available in this distribution."
            )
        developer_token = getattr(cfg, "apple_developer_token", "") or None
        fetcher = AppleFetcher(developer_token=developer_token)
        return fetcher.fetch(url)

    def _fetch_soundcloud_tracks(self, url: str, cfg: Config) -> list[TrackMetadata]:
        try:
            from antra.core.soundcloud_fetcher import SoundCloudFetcher
        except ImportError:
            raise RuntimeError(
                "SoundCloud playlist fetching is not available in this distribution."
            )
        client_id = getattr(cfg, "soundcloud_client_id", "") or None
        return SoundCloudFetcher(client_id=client_id).fetch(url)

    def _fetch_spotfetch_tracks(self, url: str, cfg: Config) -> list[TrackMetadata]:
        try:
            from antra.core.spotfetch_fetcher import SpotFetchFetcher
        except ImportError:
            raise RuntimeError(
                "Spotify playlist fetching via proxy is not available in this distribution."
            )
        return SpotFetchFetcher().fetch(url)

    def _fetch_amazon_music_tracks(self, url: str, cfg: Config) -> list[TrackMetadata]:
        try:
            from antra.core.amazon_music_fetcher import AmazonMusicFetcher
        except ImportError:
            raise RuntimeError(
                "Amazon Music playlist fetching is not available in this distribution."
            )
        return AmazonMusicFetcher(
            mirrors=cfg.amazon_mirrors,
            cookies_path=cfg.amazon_cookies_path,
        ).fetch(url)

    def _make_spotify_client(self, cfg: Config) -> SpotifyClient:
        return self._spotify_client_factory(
            cfg.spotify_client_id,
            cfg.spotify_client_secret,
            cfg.spotify_market,
            redirect_uri=cfg.spotify_redirect_uri,
            auth_storage_path=cfg.spotify_auth_path,
        )

    def _fetch_tracks_with_client(
        self,
        spotify: SpotifyClient,
        playlist: str | SpotifyPlaylistSummary,
        cfg: Config,
    ) -> list[TrackMetadata]:
        if isinstance(playlist, SpotifyPlaylistSummary):
            tracks = spotify.get_library_selection_tracks(playlist)
        else:
            tracks = spotify.get_playlist_tracks(playlist)

        if cfg.enrich_album_data:
            logger.info("Enriching tracks with album metadata...")
            for i, track in enumerate(tracks):
                tracks[i] = spotify.enrich_album_data(track)

        return tracks

    def login_spotify_user(self, options: Optional[RuntimeOptions] = None) -> bool:
        cfg = self.build_runtime_config(options)
        self.validate_config(cfg)
        spotify = self._make_spotify_client(cfg)
        return spotify.login_user()

    def logout_spotify_user(self, options: Optional[RuntimeOptions] = None):
        cfg = self.build_runtime_config(options)
        self.validate_config(cfg)
        spotify = self._make_spotify_client(cfg)
        spotify.logout_user()

    def has_spotify_user_login(self, options: Optional[RuntimeOptions] = None) -> bool:
        cfg = self.build_runtime_config(options)
        self.validate_config(cfg)
        spotify = self._make_spotify_client(cfg)
        return spotify.has_user_login()

    def get_user_library(
        self,
        options: Optional[RuntimeOptions] = None,
        include_liked_songs: bool = True,
        include_saved_albums: bool = True,
        include_followed_artists: bool = True,
    ) -> SpotifyLibrary:
        cfg = self.build_runtime_config(options)
        self.validate_config(cfg)
        spotify = self._make_spotify_client(cfg)
        return spotify.get_current_user_library(
            include_liked_songs=include_liked_songs,
            include_saved_albums=include_saved_albums,
            include_followed_artists=include_followed_artists,
        )

    @staticmethod
    def select_playlists(
        library: SpotifyLibrary,
        names_csv: Optional[str] = None,
        all_playlists: bool = False,
    ) -> list[SpotifyPlaylistSummary]:
        if all_playlists or not names_csv:
            return list(library.playlists) if all_playlists else []

        requested = [part.strip().lower() for part in names_csv.split(",") if part.strip()]
        selected: list[SpotifyPlaylistSummary] = []
        seen: set[str] = set()
        for requested_name in requested:
            for playlist in library.playlists:
                if playlist.name.lower() != requested_name:
                    continue
                if playlist.selection_key in seen:
                    continue
                selected.append(playlist)
                seen.add(playlist.selection_key)
        return selected

    def fetch_library_selections(
        self,
        selections: list[SpotifyPlaylistSummary],
        options: Optional[RuntimeOptions] = None,
        progress_callback: Optional[Callable[[BulkDownloadProgress], None]] = None,
    ) -> tuple[list[TrackMetadata], list[PlaylistFailure]]:
        cfg = self.build_runtime_config(options)
        self.validate_config(cfg)
        spotify = self._make_spotify_client(cfg)
        tracks: list[TrackMetadata] = []
        failures: list[PlaylistFailure] = []

        for index, selection in enumerate(selections, 1):
            if progress_callback:
                progress_callback(
                    BulkDownloadProgress(
                        playlist=selection,
                        playlist_index=index,
                        playlist_total=len(selections),
                        stage="fetching",
                        message=f"Fetching {selection.name}",
                    )
                )
            try:
                playlist_tracks = self._fetch_tracks_with_client(spotify, selection, cfg)
                tracks.extend(playlist_tracks)
                if progress_callback:
                    progress_callback(
                        BulkDownloadProgress(
                            playlist=selection,
                            playlist_index=index,
                            playlist_total=len(selections),
                            stage="fetched",
                            tracks_total=len(playlist_tracks),
                            message=f"Fetched {len(playlist_tracks)} tracks",
                        )
                    )
            except Exception as exc:
                failures.append(PlaylistFailure(selection, str(exc)))
                if progress_callback:
                    progress_callback(
                        BulkDownloadProgress(
                            playlist=selection,
                            playlist_index=index,
                            playlist_total=len(selections),
                            stage="fetch_failed",
                            message=str(exc),
                        )
                    )
        return tracks, failures

    def download_library_selections(
        self,
        selections: list[SpotifyPlaylistSummary],
        options: Optional[RuntimeOptions] = None,
        event_callback: Optional[Callable[[EngineEvent], None]] = None,
        controller: Optional[DownloadController] = None,
        progress_callback: Optional[Callable[[BulkDownloadProgress], None]] = None,
    ) -> BulkDownloadReport:
        cfg = self.build_runtime_config(options)
        self.validate_config(cfg)
        spotify = self._make_spotify_client(cfg)
        report = BulkDownloadReport()

        for index, selection in enumerate(selections, 1):
            if progress_callback:
                progress_callback(
                    BulkDownloadProgress(
                        playlist=selection,
                        playlist_index=index,
                        playlist_total=len(selections),
                        stage="fetching",
                        message=f"Fetching {selection.name}",
                    )
                )
            try:
                tracks = self._fetch_tracks_with_client(spotify, selection, cfg)
                if progress_callback:
                    progress_callback(
                        BulkDownloadProgress(
                            playlist=selection,
                            playlist_index=index,
                            playlist_total=len(selections),
                            stage="downloading",
                            tracks_total=len(tracks),
                            message=f"Downloading {selection.name}",
                        )
                    )
                results = self.download_tracks(
                    tracks,
                    options=options,
                    event_callback=event_callback,
                    controller=controller,
                )
                report.results.extend(results)
                if progress_callback:
                    completed = sum(1 for result in results if result.status.name == "COMPLETED")
                    progress_callback(
                        BulkDownloadProgress(
                            playlist=selection,
                            playlist_index=index,
                            playlist_total=len(selections),
                            stage="completed",
                            tracks_completed=completed,
                            tracks_total=len(results),
                            message=f"Finished {selection.name}",
                        )
                    )
            except Exception as exc:
                report.failures.append(PlaylistFailure(selection, str(exc)))
                if progress_callback:
                    progress_callback(
                        BulkDownloadProgress(
                            playlist=selection,
                            playlist_index=index,
                            playlist_total=len(selections),
                            stage="failed",
                            message=str(exc),
                        )
                    )
        return report

    def build_engine(
        self,
        cfg: Config,
        event_callback: Optional[Callable[[EngineEvent], None]] = None,
        controller: Optional[DownloadController] = None,
    ) -> DownloadEngine:
        adapters = self.build_adapters(cfg)
        if not adapters:
            raise RuntimeError("No source adapters available. Check your configuration.")

        normalized_source_preference = normalize_source_preference(cfg.source_preference)
        preserve_input_order = normalized_source_preference in {
            "auto",
            "soulseek",
            "priority-2",
            "priority-3",
            "priority-4",
        }
        resolver_adapters = adapters
        if normalized_source_preference == "auto":
            resolver_adapters = sorted(adapters, key=lambda adapter: adapter.priority)
        resolver = SourceResolver(
            resolver_adapters,
            preferred_output_format=cfg.output_format,
            preserve_input_order=preserve_input_order,
        )
        organizer = LibraryOrganizer(cfg.output_dir)

        lyrics_fetcher = None
        if cfg.fetch_lyrics:
            lyrics_fetcher = LyricsFetcher(
                musixmatch_api_key=cfg.musixmatch_api_key or None,
                genius_api_key=cfg.genius_api_key or None,
            )

        engine_cfg = EngineConfig(
            max_retries=cfg.max_retries,
            retry_delay=cfg.retry_delay,
            fetch_lyrics=cfg.fetch_lyrics,
            output_format=cfg.output_format,
        )

        return DownloadEngine(
            resolver=resolver,
            organizer=organizer,
            lyrics_fetcher=lyrics_fetcher,
            config=engine_cfg,
            event_callback=event_callback,
            controller=controller,
        )

    def download_tracks(
        self,
        tracks: list[TrackMetadata],
        options: Optional[RuntimeOptions] = None,
        event_callback: Optional[Callable[[EngineEvent], None]] = None,
        controller: Optional[DownloadController] = None,
    ) -> list[DownloadResult]:
        cfg = self.build_runtime_config(options)
        engine = self.build_engine(cfg, event_callback=event_callback, controller=controller)
        return engine.download_playlist(tracks)

    def download_playlist(
        self,
        playlist: str,
        options: Optional[RuntimeOptions] = None,
        event_callback: Optional[Callable[[EngineEvent], None]] = None,
        controller: Optional[DownloadController] = None,
    ) -> list[DownloadResult]:
        tracks = self.fetch_playlist_tracks(playlist, options=options)
        return self.download_tracks(
            tracks,
            options=options,
            event_callback=event_callback,
            controller=controller,
        )
