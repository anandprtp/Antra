"""
Reusable application service for CLI and future desktop frontends.
"""
import logging
import json
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

SOURCE_PREFERENCE_CHOICES = ("auto", "apple", "hifi", "amazon", "dab", "qobuz", "soulseek", "jiosaavn")
OUTPUT_FORMAT_CHOICES = ("source", "flac", "alac", "m4a", "aac", "mp3")
SPECIAL_SOURCE_PREFERENCE_CHOICES = ("priority-2", "priority-3", "priority-4")
SPECIAL_OUTPUT_FORMAT_CHOICES = ("lossless",)
LEGACY_SOURCE_PREFERENCE_ALIASES = {
    "tidal": "hifi",
    "anandtidal": "hifi",
}
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


def _parse_enabled_sources(value) -> set[str]:
    if not value:
        return set()
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        return set()
    return {str(item).strip().lower() for item in raw_items if str(item).strip()}


def _merge_amazon_direct_creds_json(raw_json: str, wvd_path: str) -> str:
    raw_json = (raw_json or "").strip()
    if not raw_json:
        return ""
    if not (wvd_path or "").strip():
        return raw_json
    try:
        payload = json.loads(raw_json)
    except Exception:
        return raw_json
    if not isinstance(payload, dict):
        return raw_json
    payload["wvd_path"] = (wvd_path or "").strip()
    try:
        return json.dumps(payload)
    except Exception:
        return raw_json


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
        "apple": "apple",
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
            preferred_order = ["soulseek", "apple", "hifi", "jiosaavn"]
            by_name = {adapter.name: adapter for adapter in adapters}
            return [by_name[name] for name in preferred_order if name in by_name]
        if normalized == "priority-2":
            allowed = {"hifi", "amazon", "apple", "dab", "soulseek", "jiosaavn"}
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
        """Build the active download chain for the app."""
        adapters: list = []
        enabled_sources = _parse_enabled_sources(getattr(cfg, "sources_enabled", ""))

        def source_group_enabled(name: str) -> bool:
            return not enabled_sources or name in enabled_sources

        manifest = None
        try:
            from antra.core.endpoint_manifest import load_endpoint_manifest

            manifest = load_endpoint_manifest()
        except Exception as e:
            logger.debug(f"[Sources] Endpoint manifest unavailable: {e}")

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

        if source_group_enabled("soulseek") and soulseek_base_url:
            try:
                from antra.sources.soulseek import SoulseekAdapter

                soulseek = SoulseekAdapter(
                    base_url=soulseek_base_url,
                    api_key=soulseek_api_key or None,
                    seed_after_download=getattr(cfg, "soulseek_seed_after_download", False),
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

        # Tidal Premium (session/token-backed preferred; email/password kept as legacy fallback)
        tidal_session_ready = bool(
            getattr(cfg, "tidal_enabled", False)
            and (
                (getattr(cfg, "tidal_auth_mode", "session_json") == "session_json" and (getattr(cfg, "tidal_session_json", "") or "").strip())
                or (
                    getattr(cfg, "tidal_auth_mode", "session_json") != "session_json"
                    and (getattr(cfg, "tidal_access_token", "") or "").strip()
                    and (getattr(cfg, "tidal_refresh_token", "") or "").strip()
                )
            )
        )
        if source_group_enabled("tidal") and (tidal_session_ready or (cfg.tidal_email and cfg.tidal_password)):
            try:
                from antra.sources.tidal import TidalAdapter

                adapter = TidalAdapter(
                    email=cfg.tidal_email,
                    password=cfg.tidal_password,
                    mirrors=[],
                    enabled=getattr(cfg, "tidal_enabled", False),
                    auth_mode=getattr(cfg, "tidal_auth_mode", "session_json"),
                    session_json=getattr(cfg, "tidal_session_json", ""),
                    access_token=getattr(cfg, "tidal_access_token", ""),
                    refresh_token=getattr(cfg, "tidal_refresh_token", ""),
                    session_id=getattr(cfg, "tidal_session_id", ""),
                    token_type=getattr(cfg, "tidal_token_type", "Bearer"),
                )
                if adapter.is_available():
                    adapters.append(adapter)
                    logger.info("[OK] Tidal adapter enabled")
            except Exception as e:
                logger.warning(f"Tidal adapter failed to initialize: {e}")

        # Qobuz Premium / Studio
        qobuz_ready = bool(
            getattr(cfg, "qobuz_enabled", False)
            and (
                (
                    (getattr(cfg, "qobuz_email", "") or "").strip()
                    and (getattr(cfg, "qobuz_password", "") or "").strip()
                )
                or (getattr(cfg, "qobuz_user_auth_token", "") or "").strip()
            )
        )
        if source_group_enabled("qobuz") and qobuz_ready:
            try:
                from antra.sources.qobuz import QobuzAdapter

                adapter = QobuzAdapter(
                    email=getattr(cfg, "qobuz_email", ""),
                    password=getattr(cfg, "qobuz_password", ""),
                    app_id=getattr(cfg, "qobuz_app_id", ""),
                    app_secret=getattr(cfg, "qobuz_app_secret", ""),
                    user_auth_token=getattr(cfg, "qobuz_user_auth_token", ""),
                )
                if adapter.is_available():
                    adapters.append(adapter)
                    logger.info("[OK] Qobuz adapter enabled")
            except Exception as e:
                logger.warning(f"Qobuz adapter failed to initialize: {e}")

        apple_direct_ready = bool(
            (getattr(cfg, "apple_authorization_token", "") or "").strip()
            and (getattr(cfg, "apple_music_user_token", "") or "").strip()
            and (getattr(cfg, "apple_wvd_path", "") or "").strip()
        )
        apple_mirrors = list(getattr(cfg, "apple_mirrors", None) or [])
        if not apple_mirrors and manifest is not None:
            apple_mirrors = list(getattr(manifest, "apple", []) or [])
        if source_group_enabled("apple") and getattr(cfg, "apple_enabled", False):
            try:
                from antra.sources.apple import AppleAdapter

                adapter = AppleAdapter(
                    mirrors=apple_mirrors,
                    api_key=getattr(cfg, "odesli_api_key", "") or None,
                    authorization_token=getattr(cfg, "apple_authorization_token", ""),
                    music_user_token=getattr(cfg, "apple_music_user_token", ""),
                    storefront=getattr(cfg, "apple_storefront", "us"),
                    wvd_path=getattr(cfg, "apple_wvd_path", ""),
                )
                if adapter.is_available():
                    adapters.append(adapter)
                    mode = "direct account" if apple_direct_ready else "mirror pool"
                    logger.info(f"[OK] Apple adapter enabled ({mode})")
            except Exception as e:
                logger.warning(f"Apple adapter failed to initialize: {e}")

        amazon_direct_creds_json = _merge_amazon_direct_creds_json(
            getattr(cfg, "amazon_direct_creds_json", ""),
            getattr(cfg, "amazon_wvd_path", ""),
        )
        amazon_direct_ready = bool(amazon_direct_creds_json.strip())
        amazon_mirrors = list(getattr(cfg, "amazon_mirrors", None) or [])
        if not amazon_mirrors and manifest is not None:
            amazon_mirrors = list(getattr(manifest, "amazon", []) or [])
        if source_group_enabled("amazon") and getattr(cfg, "amazon_enabled", False):
            try:
                from antra.sources.amazon import AmazonAdapter

                adapter = AmazonAdapter(
                    mirrors=amazon_mirrors,
                    api_key=getattr(cfg, "odesli_api_key", "") or None,
                    direct_creds_json=amazon_direct_creds_json,
                )
                if adapter.is_available():
                    adapters.append(adapter)
                    mode = "direct account" if amazon_direct_ready else "mirror pool"
                    logger.info(f"[OK] Amazon adapter enabled ({mode})")
            except Exception as e:
                logger.warning(f"Amazon adapter failed to initialize: {e}")

        by_name = {adapter.name: adapter for adapter in adapters}
        ordered = [by_name[name] for name in ("tidal", "apple", "amazon", "soulseek", "qobuz") if name in by_name]
        if ordered:
            logger.info(f"[Sources] Active download chain: {', '.join(adapter.name for adapter in ordered)}")
        else:
            logger.warning(
                "[Sources] No download adapters available. Enable Apple, Amazon, TIDAL, Qobuz, or Soulseek in Settings."
            )
        return ordered

    @staticmethod
    def _enrich_isrcs(tracks: list[TrackMetadata]) -> None:
        """Bulk-enrich ISRCs and release dates for Spotify-sourced tracks missing them.

        Uses the Spotify v1 /tracks endpoint with an anonymous TOTP token (same
        mechanism as the main Spotify client).  Only fires when at least one track
        has a spotify_id but no isrc — skipped entirely otherwise so there is zero
        overhead for fully-enriched track lists (e.g. tracks from authenticated
        Spotify or Apple Music catalog API).
        """
        missing = [t for t in tracks if t.spotify_id and not t.isrc]
        if not missing:
            return
        try:
            from antra.core.isrc_enricher import ISRCEnricher
            logger.info(
                f"[Service] Enriching ISRCs for {len(missing)}/{len(tracks)} tracks "
                "via Spotify API"
            )
            ISRCEnricher().enrich_tracks(tracks)
        except Exception as e:
            logger.warning(f"[Service] ISRC enrichment failed (non-fatal): {e}")

    @staticmethod
    def _stamp_disc_totals(tracks: list[TrackMetadata]) -> list[TrackMetadata]:
        """Compute total_discs per album and stamp it on each track.

        Groups tracks by album_id (or album+artist as fallback), normalizes any
        anomalous disc numbering into a clean 1..N sequence, then stamps the
        total disc count back onto every track in the group.

        Some upstream sources occasionally emit broken disc numbers like 29/39
        for a normal 2-disc release. Normalizing here keeps the default file
        naming stable across Spotify, Apple Music, Amazon, and any future source:
        disc 1 -> 101/102..., disc 2 -> 201/202..., etc.
        """
        from collections import defaultdict
        album_groups: dict[str, list[TrackMetadata]] = defaultdict(list)
        for track in tracks:
            key = track.album_id or f"{track.album}||{track.primary_artist}"
            album_groups[key].append(track)

        for group in album_groups.values():
            disc_numbers = [t.disc_number for t in group if t.disc_number is not None and t.disc_number > 0]
            if not disc_numbers:
                continue

            unique_discs = sorted(set(disc_numbers))
            expected = list(range(1, len(unique_discs) + 1))
            if unique_discs != expected:
                remap = {disc: index for index, disc in enumerate(unique_discs, start=1)}
                logger.debug(
                    "[Service] Normalizing disc numbers for album group %s: %s -> %s",
                    group[0].album if group else "unknown",
                    unique_discs,
                    expected,
                )
                for track in group:
                    if track.disc_number in remap:
                        track.disc_number = remap[track.disc_number]

            total = len(unique_discs)
            for track in group:
                track.total_discs = total

        return tracks

    def fetch_playlist_tracks(
        self,
        playlist: str,
        options: Optional[RuntimeOptions] = None,
        enrich_override: Optional[bool] = None,
    ) -> list[TrackMetadata]:
        cfg = self.build_runtime_config(options)
        self.validate_config(cfg)

        # Handle Apple Music URLs
        if "music.apple.com" in playlist:
            tracks = self._fetch_apple_tracks(playlist, cfg)

        # Handle SoundCloud URLs
        elif "soundcloud.com" in playlist:
            tracks = self._fetch_soundcloud_tracks(playlist, cfg)

        # Handle Amazon Music URLs
        elif "music.amazon." in playlist:
            tracks = self._fetch_amazon_music_tracks(playlist, cfg)
            enrich_album_data = getattr(cfg, "enrich_album_data", False) if enrich_override is None else enrich_override
            if enrich_album_data:
                try:
                    spotify = self._make_spotify_client(cfg)
                    logger.info("Enriching Amazon tracks with Spotify metadata...")
                    tracks = spotify.batch_enrich_album_data(tracks)
                except Exception as e:
                    logger.debug(f"[Service] Spotify hydration failed: {e}")

        else:
            # Try authenticated Spotify client first.
            # On auth failure → fall back to:
            #   1. SpotFetch proxy (returns ISRC + full metadata)
            #   2. Direct Spotify public-page scraping (no 3rd-party dependency)
            spotify = self._make_spotify_client(cfg)
            try:
                tracks = self._fetch_tracks_with_client(spotify, playlist, cfg, enrich_override=enrich_override)
            except SpotifyResourceError as e:
                logger.debug(
                    f"[Spotify] Resource error ({e}) — trying SpotFetch proxy"
                )
                tracks = self._fetch_spotfetch_tracks(playlist, cfg, spotify)
            except Exception as e:
                if _is_auth_error(e):
                    logger.debug(
                        "[Spotify] Auth not configured — trying SpotFetch proxy"
                    )
                    tracks = self._fetch_spotfetch_tracks(playlist, cfg, spotify)
                else:
                    raise

        return self._stamp_disc_totals(tracks)

    def enrich_tracks_for_download(
        self,
        tracks: list[TrackMetadata],
        playlist: str,
        options: Optional[RuntimeOptions] = None,
    ) -> list[TrackMetadata]:
        cfg = self.build_runtime_config(options)
        self.validate_config(cfg)
        if not getattr(cfg, "enrich_album_data", False) or not tracks:
            return self._stamp_disc_totals(tracks)

        try:
            if "music.amazon." in playlist:
                spotify = self._make_spotify_client(cfg)
                logger.info("Enriching Amazon tracks with Spotify metadata...")
                tracks = spotify.batch_enrich_album_data(tracks)
            elif not (
                "music.apple.com" in playlist
                or "soundcloud.com" in playlist
            ):
                spotify = self._make_spotify_client(cfg)
                logger.info("Enriching tracks with album metadata...")
                tracks = spotify.batch_enrich_album_data(tracks)
        except Exception as e:
            logger.debug(f"[Service] Deferred track enrichment failed: {e}")

        return self._stamp_disc_totals(tracks)

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

    def _fetch_spotfetch_tracks(
        self, url: str, cfg: Config, spotify: Optional[SpotifyClient] = None
    ) -> list[TrackMetadata]:
        import re as _re

        # ── Attempt 1: SpotFetch proxy (has ISRC + full metadata) ─────────────
        try:
            from antra.core.spotfetch_fetcher import SpotFetchFetcher
            mirrors = getattr(cfg, "spotfetch_mirrors", None) or None
            return SpotFetchFetcher(bases=mirrors).fetch(url)
        except ImportError:
            pass
        except ValueError:
            raise  # bad URL / 404 — no point trying further
        except Exception as e:
            logger.debug(f"[SpotFetch] All proxies failed ({e}) — falling back to public scraper")

        # ── Attempt 2: Direct Spotify partner API (TOTP token, no 3rd-party) ──
        if spotify is None:
            spotify = self._make_spotify_client(cfg)

        # Album — partner GraphQL API (most reliable, full track listing)
        m_album = _re.search(r"spotify\.com/(?:intl-[a-z]+/)?album/([A-Za-z0-9]+)", url)
        if m_album:
            album_id = m_album.group(1)
            tracks = spotify._fetch_album_via_partner_api(album_id)
            if tracks:
                logger.info("[Spotify] Used partner GraphQL API for album (no credentials)")
                return tracks
            # last-resort HTML scrape
            tracks = spotify._fetch_public_album_page(album_id)
            if tracks:
                logger.info("[Spotify] Used public album page scraper")
                return tracks

        # Track
        m_track = _re.search(r"spotify\.com/(?:intl-[a-z]+/)?track/([A-Za-z0-9]+)", url)
        if m_track:
            meta = spotify._fetch_public_track_page(m_track.group(1))
            if meta:
                logger.info("[Spotify] Used public track page scraper")
                return [meta]

        # Playlist — partner GraphQL API
        m_pl = _re.search(r"spotify\.com/(?:intl-[a-z]+/)?playlist/([A-Za-z0-9]+)", url)
        if m_pl:
            tracks = spotify._fetch_public_playlist_embed(m_pl.group(1))
            if tracks:
                logger.info("[Spotify] Used partner GraphQL API for playlist (no credentials)")
                return tracks

        raise RuntimeError(
            "Spotify metadata unavailable: all no-credentials methods failed. "
            "Configure Spotify credentials to get reliable metadata."
        )

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

    def search_artists(self, query: str, source: str = "spotify") -> list[dict]:
        """Search for artists by name. Returns scored results for the UI.

        source: "spotify" | "apple"
        Each result: {artist_id, name, artwork_url, genres, followers, match_score, profile_url, source}
        """
        if source == "apple":
            from antra.core.apple_fetcher import AppleFetcher
            return AppleFetcher().search_artists(query)

        # Spotify — try with credentials first, then anonymous token (handled inside
        # spotify.search_artists). If that returns nothing (e.g. rate-limited), fall
        # back to Apple Music / iTunes search so the user gets results. Apple Music
        # profile URLs are handled correctly by the discography flow.
        try:
            spotify = self._make_spotify_client(self._base_config)
            results = spotify.search_artists(query)
            if results:
                return results
        except Exception as e:
            logger.debug(f"[Service] Spotify artist search unavailable ({e})")

        logger.debug("[Service] Spotify search returned no results — falling back to Apple Music")
        try:
            from antra.core.apple_fetcher import AppleFetcher
            return AppleFetcher().search_artists(query)
        except Exception as e:
            logger.debug(f"[Service] Apple fallback artist search failed: {e}")
        return []

    def _make_spotify_client(self, cfg: Config) -> SpotifyClient:
        client = self._spotify_client_factory(
            cfg.spotify_client_id,
            cfg.spotify_client_secret,
            cfg.spotify_market,
            redirect_uri=cfg.spotify_redirect_uri,
            auth_storage_path=cfg.spotify_auth_path,
        )
        client._spotfetch_mirrors = getattr(cfg, "spotfetch_mirrors", None)
        return client

    def _fetch_tracks_with_client(
        self,
        spotify: SpotifyClient,
        playlist: str | SpotifyPlaylistSummary,
        cfg: Config,
        enrich_override: Optional[bool] = None,
    ) -> list[TrackMetadata]:
        if isinstance(playlist, SpotifyPlaylistSummary):
            tracks = spotify.get_library_selection_tracks(playlist)
        else:
            tracks = spotify.get_playlist_tracks(playlist)

        enrich_album_data = cfg.enrich_album_data if enrich_override is None else enrich_override
        if enrich_album_data:
            logger.info("Enriching tracks with album metadata...")
            tracks = spotify.batch_enrich_album_data(tracks)

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
        organizer: Optional[LibraryOrganizer] = None,
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
            prefer_explicit=getattr(cfg, "prefer_explicit", True),
        )
        if organizer is None:
            full_albums = getattr(cfg, "library_mode", "smart_dedup") == "full_albums"
            organizer = LibraryOrganizer(
                cfg.output_dir,
                full_albums=full_albums,
                folder_structure=getattr(cfg, "folder_structure", "standard"),
                filename_format=getattr(cfg, "filename_format", "default"),
            )

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
            max_workers=2,
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
        organizer: Optional[LibraryOrganizer] = None,
    ) -> list[DownloadResult]:
        cfg = self.build_runtime_config(options)
        engine = self.build_engine(cfg, event_callback=event_callback, controller=controller, organizer=organizer)
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
