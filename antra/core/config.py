"""
Configuration management via .env / environment variables.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    # Load .env as a fallback only. Runtime environment variables from the
    # desktop app / tests / user shell must win over repo defaults so generated
    # keys and temporary overrides are not clobbered at import time.
    load_dotenv(override=False)
except ImportError:
    pass


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SPOTIFY_AUTH_PATH = str(REPO_ROOT / ".antra_auth.json")

from antra.utils.runtime import get_app_data_dir
# Never falls back to an ephemeral _MEIPASS path in a frozen build -- see
# get_app_data_dir()'s docstring.
_data_dir = str(get_app_data_dir())
DEFAULT_SPOTIFY_CACHE_PATH = str(Path(_data_dir) / ".spotify_cache")

# Hardcoded Antra Spotify App client ID (PKCE flow — no secret needed)
_ANTRA_SPOTIFY_CLIENT_ID = "9d6a33e76f6340f98893ac845220e264"



def _split_urls(value: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for raw in value.replace("\n", ",").replace(";", ",").split(","):
        cleaned = raw.strip().rstrip("/")
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        urls.append(cleaned)
    return urls


@dataclass
class Config:
    # Spotify (required)
    spotify_client_id: str = _ANTRA_SPOTIFY_CLIENT_ID
    spotify_client_secret: str = ""
    spotify_market: str = ""
    spotify_redirect_uri: str = "http://127.0.0.1:8888/callback"
    spotify_auth_path: str = DEFAULT_SPOTIFY_AUTH_PATH
    spotify_cache_path: str = DEFAULT_SPOTIFY_CACHE_PATH
    spotify_sp_dc: str = ""
    spotify_access_token: str = ""

    # Qobuz (optional, preferred for FLAC)
    qobuz_enabled: bool = False
    qobuz_email: str = ""
    qobuz_password: str = ""
    qobuz_app_id: str = "285473059"
    qobuz_app_secret: str = ""
    qobuz_user_auth_token: str = ""

    # Deezer (optional, hi-fi FLAC)
    deezer_arl_token: str = ""
    deezer_bf_secret: str = "g4el58wc0zvf9na1"

    # Tidal (optional)
    tidal_email: str = ""
    tidal_password: str = ""
    tidal_enabled: bool = False
    tidal_auth_mode: str = "session_json"
    tidal_session_json: str = ""
    tidal_access_token: str = ""
    tidal_refresh_token: str = ""
    tidal_session_id: str = ""
    tidal_token_type: str = "Bearer"
    tidal_country_code: str = ""

    # YAMS (yams.tf — Qobuz/Deezer backend, requires auth token)
    yams_enabled: bool = True
    yams_auth_token: str = ""

    # Qobuz Proxy (optional — no credentials needed, free FLAC via community
    # Qobuz proxy endpoints: qobuz.squid.wtf, etc.)
    qobuz_proxy_enabled: bool = True

    # JioSaavn (optional — no credentials needed, India-focused AAC 320kbps)
    jiosaavn_enabled: bool = True
    jiosaavn_quality: str = "320"  # 12 | 48 | 96 | 160 | 320

    # Odesli / song.link (optional — raises rate limit for ISRC→platform ID lookups)
    odesli_api_key: str = ""

    # Lyrics
    musixmatch_api_key: str = ""
    genius_api_key: str = ""

    # SoundCloud (optional — no credentials needed; provide client_id to skip auto-detection)
    soundcloud_client_id: str = ""

    # SpotFetch (no credentials needed — Spotify metadata proxy for no-auth fallback)
    spotfetch_mirrors: list[str] = field(default_factory=lambda: [
        "https://sp.afkarxyz.qzz.io/api",
        "https://sp.vov.li/api",
        "https://sp.rnb.su/api",
        "https://spotify.squid.wtf/api",
    ])

    # Apple Music (optional — no credentials needed, lossless ALAC via community proxy)
    apple_enabled: bool = True
    apple_mirrors: list[str] = field(default_factory=list)
    # Developer token for Apple Music Catalog API (used for playlist fetching).
    # Songs and albums work without this. Get one from developer.apple.com
    # OR leave blank — Antra will auto-extract one from the Apple Music web player.
    apple_developer_token: str = ""

    # Amazon Music download adapter (optional — community proxy based).
    # Disabled by default because the shared proxy pool is no longer relied on
    # for downloads. Amazon Music links/metadata can still be handled elsewhere.
    amazon_enabled: bool = False
    amazon_mirrors: list[str] = field(default_factory=list)
    amazon_region: str = "US"  # US, UK, DE, FR, JP, CA, IT, ES, IN
    amazon_auth_method: str = "proxy"  # proxy, cookies
    amazon_cookies_path: str = ""
    amazon_insecure_mirrors: bool = True

    # HiFi download adapter (optional — community hifi-api / Tidal proxy pool).
    # Disabled by default because these public mirrors are no longer used for
    # downloads. The adapter remains available as an explicit opt-in.
    hifi_enabled: bool = False

    # Output
    output_dir: str = "./Music"

    # Download behaviour
    max_retries: int = 3
    retry_delay: float = 1.0
    fetch_lyrics: bool = True
    enrich_album_data: bool = True
    source_preference: str = "auto"
    output_format: str = "flac"
    save_cover_art_sidecar: bool = False

    # v2.0.0 PORT-2 - sidecar files. All three default OFF: a new file silently
    # appearing in every existing user's library is a behaviour change nobody
    # asked for. No .m3u toggle - the organizer already writes one for
    # playlists; see antra/utils/sidecars.py for why that is deliberate.
    sidecar_cue: bool = False
    sidecar_nfo: bool = False
    sidecar_json: bool = False

    # v2.0.0 FR-2 - cover art settings block.
    # local_artwork_size defaults to 0 (original) rather than a fixed 1400 so
    # the byte-for-byte sidecar fast path is reachable out of the box; before
    # FR-2 every sidecar was a JPEG q95 re-encode measured 0.8-16.9% LARGER
    # than the original it replaced.
    cover_image_template: str = "cover"
    local_artwork_size: int = 0
    local_artwork_format: str = "jpg"
    jpeg_image_quality: int = 90
    # 1000 matches the hardcoded cap the v1.1.6 BUG-F fix introduced, so the
    # embedded path is byte-unchanged for anyone who never opens the setting.
    embedded_artwork_size: int = 1000
    embedded_artwork_png: bool = False
    save_artist_image: bool = False
    artist_image_template: str = "folder"

    # Soulseek / slskd (optional)
    soulseek_base_url: str = ""
    soulseek_api_key: str = ""
    soulseek_username: str = ""
    soulseek_password: str = ""
    soulseek_auto_bootstrap: bool = True
    # After moving a completed download to the library, create a hardlink back
    # in slskd's downloads dir so slskd can seed it to other Soulseek users.
    soulseek_seed_after_download: bool = False

    # Comma-separated list of enabled adapter groups: "hifi", "soulseek".
    # Empty = all enabled. Controlled via the Sources toggle in Settings.
    sources_enabled: str = ""

    # Prefer explicit (non-censored) track versions.  When True, the resolver
    # penalises results whose title contains "radio edit", "clean version", etc.
    # (or whose adapter confirms is_explicit=False) and keeps searching for the
    # explicit version rather than immediately accepting the clean one.
    prefer_explicit: bool = True
    # Opt-in safety mode for niche catalogs. When enabled, Antra becomes more
    # willing to fail a track than accept a lower-confidence match.
    strict_matching: bool = False
    # v1.1.8 FEAT-3 — never silently accept a lower-quality substitute. When a
    # lossless output format is requested and only a lossy file can be delivered,
    # fail the track into the Failed panel instead of keeping it.
    strict_format: bool = True
    # v1.1.8 FEAT-2 — never re-encode one lossy format into another (generation
    # loss). Same-format lossy output is unaffected (already a stream copy/remux).
    prevent_lossy_transcode: bool = True
    # v1.1.8 FEAT-4 — stamp which Antra version/source produced a file so a
    # library owner can re-fetch folders written by a version with a known bug.
    write_antra_tags: bool = True
    # v2.0.0 DMX-1 — per-tag write toggles. Stored as the DISABLED ids, so an
    # empty tuple (the default, and what any pre-DMX-1 config.json produces)
    # means every tag is written exactly as before.
    disabled_tags: tuple[str, ...] = ()
    # How the `date` tag is rendered: iso | year | dmy | mdy. ISO is the
    # default and must stay it — v1.1.3 established Windows Media Player needs
    # a full ISO date string.
    date_format: str = "iso"
    # Write the legacy ID3v1 tag alongside ID3v2 on MP3. On by default because
    # that is what the tagger has always done; some old hardware needs it and
    # some library scanners prefer it absent.
    write_id3v1: bool = True

    # Library deduplication mode:
    #   "smart_dedup"  — skip a track if the same ISRC/ID exists anywhere in the library (default)
    #   "full_albums"  — only skip if the file exists in the exact target folder; allows the same
    #                    track to exist in multiple album folders (e.g. studio album + Best Of)
    library_mode: str = "smart_dedup"

    # Legacy folder structure layout retained for backward compatibility.
    # New installs should use the more specific per-content settings below.
    folder_structure: str = "standard"

    # Album folder layout:
    #   "standard" — Albums / Artist / Album (Year) / files
    #   "flat"     — Albums / Album (Year) / files
    album_folder_structure: str = "standard"

    # Playlist folder layout:
    #   "standard" — Playlists / Playlist Name / files
    #   "flat"     — <root> / Playlist Name / files
    playlist_folder_structure: str = "standard"
    # Playlist storage mode (issue #78):
    #   "playlist_folder" - tracks live under the playlist name (default, unchanged)
    #   "library"         - tracks are filed by the configured library folder /
    #                       filename templates, exactly like an album download;
    #                       the playlist survives only as the .m3u manifest.
    playlist_storage_mode: str = "playlist_folder"
    # issue #75 — rename a playlist's files when their position in the playlist
    # moved. Spotify's Liked Songs prepends, so without this every number in the
    # folder goes stale the moment one song is added.
    playlist_renumber: bool = True
    # issue #75 — files left in a playlist folder that the playlist no longer
    # contains: "ask" (report only, default) | "auto" (delete) | "off".
    playlist_prune_orphans: str = "ask"

    # Single-track layout:
    #   "album_numbered" — store under the album folder as 101 - Title (default)
    #   "album"          — store under the album folder without forced numbering
    #   "file"           — store as a standalone file in the library root
    single_track_structure: str = "album_numbered"

    # Filename format for downloaded tracks:
    #   "default"      — NN - Title  (track-number prefix, current behaviour)
    #   "title_only"   — Title
    #   "artist_title" — Artist - Title
    #   "title_artist" — Title - Artist
    filename_format: str = "default"
    single_track_filename_template: str = ""
    album_zip_name_template: str = ""
    album_track_filename_template: str = ""
    folder_structure_template: str = ""
    # v1.1.8 FEAT-11 — user explicitly cleared the folder template: save straight
    # into the library root with no album folder. Deliberately a separate flag
    # rather than overloading an empty template, because "" already means "unset,
    # use the default" for CLI and web callers and flipping that would silently
    # restructure their libraries.
    flat_library_root: bool = False
    multi_disc_handling: str = "prefix"
    track_number_padding: int = 2
    illegal_character_replacement: str = ""
    whitespace_handling: str = "preserve"
    filename_conflict_behavior: str = "skip"

    # -- v2.0.0 DMX-6 -- post-download command hook -----------------------
    # OFF by default. post_command_confirmed is a second, independent gate:
    # the hook runs a program with values taken from remote catalogue
    # metadata, so it must be acknowledged once in the UI before anything
    # executes. Enforced in PostCommandRunner, not only in the UI, so a
    # config.json hand-edited to enabled=true stays inert without it.
    post_command_enabled: bool = False
    post_command_confirmed: bool = False
    post_command: str = ""
    post_command_mode: str = "track"
    post_command_timeout: int = 30
    # v2.0.0 SF-2 - ReplayGain 2.0 tags. OFF by default: one full decode per track.
    replaygain_enabled: bool = False
    # Audio fingerprint verification: check the downloaded audio against the
    # track's free 30s store preview and fail the track when it is a different
    # recording. ON by default: it costs ~1.4s and one small preview fetch per
    # track, and it is the only check that looks at the audio itself.
    verify_audio_enabled: bool = True
    verify_audio_threshold: float = 0.35

    # ── v2.0.0 DMX-2 — output-only text transforms ──────────────────────────
    # These are applied by antra_shared.filename_prefs.transform_text at the
    # TAG and FILENAME sites only. Nothing here mutates TrackMetadata, so the
    # resolver, strip_collab() and the identity gate keep seeing the
    # catalogue's original strings — applying them upstream would bring back
    # BUG-1's wrong-audio-correct-metadata failures.
    #
    # Every default is a no-op EXCEPT remove_duplicate_artists (default on),
    # which only changes output for a track whose artist list actually repeats
    # a name.
    title_casing: str = "nothing"
    artist_casing: str = "nothing"
    remove_album_version: bool = False
    remove_duplicate_artists: bool = True
    featured_artists_mode: str = "nothing"
    multi_artist_separator: str = "default"
    album_various_artists: bool = True

    # Direct Amazon Music credentials (JSON blob from amazon_creds.json).
    # When set, Antra calls the Amazon DMLS API directly with the user's own
    # paid account — no proxy server required.  Fields: cookie, authorization,
    # csrf_token, csrf_rnd, csrf_ts, customer_id, device_id, session_id, wvd_path.
    amazon_direct_creds_json: str = ""
    # Widevine device path used by Amazon direct-account login flows.
    # Stored separately so the app can refresh browser-session tokens without
    # forcing the user to keep editing a large credentials blob manually.
    amazon_wvd_path: str = ""

    # Direct Apple Music credentials for lossless ALAC downloads.
    # authorization    — Bearer JWT from music.apple.com (static web player token)
    # music_user_token — per-user Music-User-Token (~30 day expiry)
    # storefront       — country code, e.g. "us", "gb"
    # wvd_path         — path to android_l3.wvd Widevine device file
    apple_authorization_token: str = ""
    apple_music_user_token: str = ""
    apple_storefront: str = "us"
    apple_wvd_path: str = ""

    # ── Self-hosted mirror servers (API-Mirrors on laptop) ────────────────────
    # Set these to your Cloudflare Tunnel URLs (or http://localhost:PORT for local).
    # Leave blank to disable the mirror adapter.
    #
    # Tidal mirror  — 24-bit HiRes FLAC, priority 1 (highest)
    # Requires per-session SOCKS5 proxies in tidal_api/sessions/proxies.json
    # because Tidal API rejects Indian IPs.
    tidal_mirror_url: str = ""

    # Qobuz mirror  — 24-bit FLAC, priority 1
    # WARNING: Qobuz bans automated API access since Oct 2025. Use at own risk.
    qobuz_mirror_url: str = ""

    # Deezer mirror — 16-bit FLAC, priority 3 (fallback after 24-bit sources)
    # No IP restriction — works from India without proxy.
    deezer_mirror_url: str = ""

    # API key for mirror servers.
    # Normally delivered automatically via the manifest — users don't set this directly.
    # Override with ANTRA_API_KEY if you want to bypass the manifest for testing.
    antra_api_key: str = ""

    # Auto-sync / scheduled downloads
    auto_sync_enabled: bool = False
    # Hour (0–23) and minute (0–59) in local time when auto-sync fires.
    auto_sync_hour: int = 6
    auto_sync_minute: int = 0
    # Bitmask of days: bit 0 = Monday … bit 6 = Sunday. 127 = every day.
    auto_sync_days: int = 127
    # List of tracked playlist dicts: {url, last_track_ids: [...], last_sync: ISO}
    # Stored directly in config.json — too complex for env var encoding.
    tracked_playlists: list = field(default_factory=list)

    # Persistent provider priority (SF-1). When enabled, the resolver remembers
    # which adapters recently delivered (or failed) and reorders them within each
    # quality tier across sessions. Empty db_path → default user-data location.
    provider_stats_enabled: bool = True
    provider_stats_db_path: str = ""

    # Persistent library identity index (v2.0.0 SF-4). Caches per-file dedup
    # keys so the library scan does not re-parse every file on every run. Pure
    # cache — disabling it only makes startup slower, never different.
    library_index_enabled: bool = True
    library_index_db_path: str = ""


def _int_env(name: str, default: int) -> int:
    """int() an env var, falling back to the default on blank/garbage.

    A raw int(os.getenv(...)) here would raise at config load and take every
    download with it if the GUI ever wrote an empty string.
    """
    try:
        return int(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        return default


def _verify_threshold() -> float:
    """Fingerprint mismatch limit, clamped to a sane band.

    Measured separation is genuine <= 0.20 and different-recording >= 0.40, so a
    value outside 0.20-0.45 would either fail good downloads or never fire. An
    unparseable value falls back to the default rather than raising.
    """
    try:
        value = float(os.getenv("VERIFY_AUDIO_THRESHOLD", "0.35") or 0.35)
    except ValueError:
        return 0.35
    return min(max(value, 0.20), 0.45)


def load_config() -> Config:
    """Load configuration from environment variables."""
    cfg = Config(
        spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID", _ANTRA_SPOTIFY_CLIENT_ID),
        spotify_client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", ""),
        spotify_market=os.getenv("SPOTIFY_MARKET", ""),
        spotify_redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
        spotify_auth_path=os.getenv("SPOTIFY_AUTH_PATH", DEFAULT_SPOTIFY_AUTH_PATH),
        spotify_cache_path=os.getenv("SPOTIFY_CACHE_PATH", DEFAULT_SPOTIFY_CACHE_PATH),
        spotify_sp_dc=os.getenv("SPOTIFY_SP_DC", ""),
        spotify_access_token=os.getenv("SPOTIFY_ACCESS_TOKEN", ""),
        qobuz_enabled=os.getenv("QOBUZ_ENABLED", "false").lower() == "true",
        qobuz_email=os.getenv("QOBUZ_EMAIL", ""),
        qobuz_password=os.getenv("QOBUZ_PASSWORD", ""),
        qobuz_app_id=os.getenv("QOBUZ_APP_ID", "285473059"),
        qobuz_app_secret=os.getenv("QOBUZ_APP_SECRET", ""),
        qobuz_user_auth_token=os.getenv("QOBUZ_USER_AUTH_TOKEN", ""),
        deezer_arl_token=os.getenv("DEEZER_ARL_TOKEN", ""),
        deezer_bf_secret=os.getenv("DEEZER_BF_SECRET", "g4el58wc0zvf9na1"),
        tidal_email=os.getenv("TIDAL_EMAIL", ""),
        tidal_password=os.getenv("TIDAL_PASSWORD", ""),
        tidal_enabled=os.getenv("TIDAL_ENABLED", "false").lower() == "true",
        tidal_auth_mode=os.getenv("TIDAL_AUTH_MODE", "session_json"),
        tidal_session_json=os.getenv("TIDAL_SESSION_JSON", ""),
        tidal_access_token=os.getenv("TIDAL_ACCESS_TOKEN", ""),
        tidal_refresh_token=os.getenv("TIDAL_REFRESH_TOKEN", ""),
        tidal_session_id=os.getenv("TIDAL_SESSION_ID", ""),
        tidal_token_type=os.getenv("TIDAL_TOKEN_TYPE", "Bearer"),
        tidal_country_code=os.getenv("TIDAL_COUNTRY_CODE", ""),
        yams_enabled=os.getenv("YAMS_ENABLED", "true").lower() == "true",
        yams_auth_token=os.getenv("YAMS_AUTH_TOKEN", ""),
        qobuz_proxy_enabled=os.getenv("QOBUZ_PROXY_ENABLED", "true").lower() == "true",
        jiosaavn_enabled=os.getenv("JIOSAAVN_ENABLED", "true").lower() == "true",
        jiosaavn_quality=os.getenv("JIOSAAVN_QUALITY", "320"),
        odesli_api_key=os.getenv("ODESLI_API_KEY", ""),
        soundcloud_client_id=os.getenv("SOUNDCLOUD_CLIENT_ID", ""),
        musixmatch_api_key=os.getenv("MUSIXMATCH_API_KEY", ""),
        genius_api_key=os.getenv("GENIUS_API_KEY", ""),
        output_dir=os.getenv("OUTPUT_DIR", "./Music"),
        spotfetch_mirrors=_split_urls(os.getenv(
            "SPOTFETCH_MIRRORS",
            "https://sp.afkarxyz.qzz.io/api,https://sp.vov.li/api,https://sp.rnb.su/api,https://spotify.squid.wtf/api",
        )),
        apple_enabled=os.getenv("APPLE_ENABLED", "true").lower() == "true",
        apple_mirrors=_split_urls(os.getenv("APPLE_MIRRORS", "")),
        apple_developer_token=os.getenv("APPLE_DEVELOPER_TOKEN", ""),
        amazon_enabled=os.getenv("AMAZON_ENABLED", "false").lower() == "true",
        amazon_mirrors=_split_urls(os.getenv("AMAZON_MIRRORS", "")),
        amazon_region=os.getenv("AMAZON_REGION", "US"),
        amazon_auth_method=os.getenv("AMAZON_AUTH_METHOD", "proxy"),
        amazon_cookies_path=os.getenv("AMAZON_COOKIES_PATH", ""),
        amazon_insecure_mirrors=os.getenv("AMAZON_INSECURE_MIRRORS", "true").lower() == "true",
        hifi_enabled=os.getenv("HIFI_ENABLED", "false").lower() == "true",
        max_retries=int(os.getenv("MAX_RETRIES", "3")),
        retry_delay=float(os.getenv("RETRY_DELAY", "1.0")),
        fetch_lyrics=os.getenv("FETCH_LYRICS", "true").lower() == "true",
        enrich_album_data=os.getenv("ENRICH_ALBUM_DATA", "true").lower() == "true",
        source_preference=os.getenv("SOURCE_PREFERENCES", os.getenv("SOURCE_PREFERENCE", "auto")),
        output_format=os.getenv("OUTPUT_FORMAT", "flac"),
        save_cover_art_sidecar=os.getenv("SAVE_COVER_ART_SIDECAR", "false").lower() == "true",
        sidecar_cue=os.getenv("SIDECAR_CUE", "false").lower() == "true",
        sidecar_nfo=os.getenv("SIDECAR_NFO", "false").lower() == "true",
        sidecar_json=os.getenv("SIDECAR_JSON", "false").lower() == "true",
        cover_image_template=os.getenv("COVER_IMAGE_TEMPLATE", "cover"),
        local_artwork_size=_int_env("LOCAL_ARTWORK_SIZE", 0),
        local_artwork_format=os.getenv("LOCAL_ARTWORK_FORMAT", "jpg"),
        jpeg_image_quality=_int_env("JPEG_IMAGE_QUALITY", 90),
        embedded_artwork_size=_int_env("EMBEDDED_ARTWORK_SIZE", 1000),
        embedded_artwork_png=os.getenv("EMBEDDED_ARTWORK_PNG", "false").lower() == "true",
        save_artist_image=os.getenv("SAVE_ARTIST_IMAGE", "false").lower() == "true",
        artist_image_template=os.getenv("ARTIST_IMAGE_TEMPLATE", "folder"),
        soulseek_base_url=os.getenv("SLSKD_BASE_URL", ""),
        soulseek_api_key=os.getenv("SLSKD_API_KEY", ""),
        soulseek_username=os.getenv("SOULSEEK_USERNAME", ""),
        soulseek_password=os.getenv("SOULSEEK_PASSWORD", ""),
        soulseek_auto_bootstrap=os.getenv("SLSKD_AUTO_BOOTSTRAP", "true").lower() == "true",
        soulseek_seed_after_download=os.getenv("SOULSEEK_SEED_AFTER_DOWNLOAD", "false").lower() == "true",
        sources_enabled=os.getenv("SOURCES_ENABLED", ""),
        prefer_explicit=os.getenv("PREFER_EXPLICIT", "true").lower() == "true",
        strict_matching=os.getenv("STRICT_MATCHING", "false").lower() == "true",
        strict_format=os.getenv("STRICT_FORMAT", "true").lower() == "true",
        prevent_lossy_transcode=os.getenv("PREVENT_LOSSY_TRANSCODE", "true").lower() == "true",
        write_antra_tags=os.getenv("WRITE_ANTRA_TAGS", "true").lower() == "true",
        disabled_tags=tuple(
            part.strip().lower()
            for part in os.getenv("DISABLED_TAGS", "").split(",")
            if part.strip()
        ),
        date_format=os.getenv("DATE_FORMAT", "iso").strip().lower() or "iso",
        write_id3v1=os.getenv("WRITE_ID3V1", "true").lower() == "true",
        library_mode=os.getenv("LIBRARY_MODE", "smart_dedup"),
        folder_structure=os.getenv("FOLDER_STRUCTURE", "standard"),
        album_folder_structure=os.getenv("ALBUM_FOLDER_STRUCTURE", os.getenv("FOLDER_STRUCTURE", "standard")),
        playlist_folder_structure=os.getenv("PLAYLIST_FOLDER_STRUCTURE", os.getenv("FOLDER_STRUCTURE", "standard")),
        playlist_storage_mode=os.getenv("PLAYLIST_STORAGE_MODE", "playlist_folder"),
        playlist_renumber=os.getenv("PLAYLIST_RENUMBER", "true").lower() == "true",
        playlist_prune_orphans=os.getenv("PLAYLIST_PRUNE_ORPHANS", "ask"),
        single_track_structure=os.getenv("SINGLE_TRACK_STRUCTURE", "album_numbered"),
        filename_format=os.getenv("FILENAME_FORMAT", "default"),
        single_track_filename_template=os.getenv("SINGLE_TRACK_FILENAME_TEMPLATE", ""),
        album_zip_name_template=os.getenv("ALBUM_ZIP_NAME_TEMPLATE", ""),
        album_track_filename_template=os.getenv("ALBUM_TRACK_FILENAME_TEMPLATE", ""),
        folder_structure_template=os.getenv("FOLDER_STRUCTURE_TEMPLATE", ""),
        flat_library_root=os.getenv("FLAT_LIBRARY_ROOT", "false").lower() == "true",
        multi_disc_handling=os.getenv("MULTI_DISC_HANDLING", "prefix"),
        track_number_padding=int(os.getenv("TRACK_NUMBER_PADDING", "2")),
        illegal_character_replacement=os.getenv("ILLEGAL_CHARACTER_REPLACEMENT", ""),
        whitespace_handling=os.getenv("WHITESPACE_HANDLING", "preserve"),
        filename_conflict_behavior=os.getenv("FILENAME_CONFLICT_BEHAVIOR", "skip"),
        post_command_enabled=os.getenv("POST_COMMAND_ENABLED", "false").lower() == "true",
        post_command_confirmed=os.getenv("POST_COMMAND_CONFIRMED", "false").lower() == "true",
        post_command=os.getenv("POST_COMMAND", ""),
        post_command_mode=os.getenv("POST_COMMAND_MODE", "track"),
        post_command_timeout=int(os.getenv("POST_COMMAND_TIMEOUT", "30") or 30),
        replaygain_enabled=os.getenv("REPLAYGAIN_ENABLED", "false").lower() == "true",
        verify_audio_enabled=os.getenv("VERIFY_AUDIO_ENABLED", "true").lower() == "true",
        verify_audio_threshold=_verify_threshold(),
        # v2.0.0 DMX-2 — clamping lives in migrate_legacy_templates, so an
        # unrecognised value read from here degrades to the no-op rather than
        # reaching a regex or a casing branch.
        title_casing=os.getenv("TITLE_CASING", "nothing").strip().lower() or "nothing",
        artist_casing=os.getenv("ARTIST_CASING", "nothing").strip().lower() or "nothing",
        remove_album_version=os.getenv("REMOVE_ALBUM_VERSION", "false").lower() == "true",
        remove_duplicate_artists=os.getenv("REMOVE_DUPLICATE_ARTISTS", "true").lower() == "true",
        featured_artists_mode=os.getenv("FEATURED_ARTISTS_MODE", "nothing").strip().lower() or "nothing",
        multi_artist_separator=os.getenv("MULTI_ARTIST_SEPARATOR", "default").strip().lower() or "default",
        album_various_artists=os.getenv("ALBUM_VARIOUS_ARTISTS", "true").lower() == "true",
        amazon_direct_creds_json=os.getenv("AMAZON_DIRECT_CREDS_JSON", ""),
        amazon_wvd_path=os.getenv("AMAZON_WVD_PATH", ""),
        apple_authorization_token=os.getenv("APPLE_AUTHORIZATION_TOKEN", ""),
        apple_music_user_token=os.getenv("APPLE_MUSIC_USER_TOKEN", ""),
        apple_storefront=os.getenv("APPLE_STOREFRONT", "us"),
        apple_wvd_path=os.getenv("APPLE_WVD_PATH", ""),
        tidal_mirror_url=os.getenv("TIDAL_MIRROR_URL", ""),
        qobuz_mirror_url=os.getenv("QOBUZ_MIRROR_URL", ""),
        deezer_mirror_url=os.getenv("DEEZER_MIRROR_URL", ""),
        antra_api_key=os.getenv("ANTRA_API_KEY", ""),
        auto_sync_enabled=os.getenv("AUTO_SYNC_ENABLED", "false").lower() == "true",
        auto_sync_hour=int(os.getenv("AUTO_SYNC_HOUR", "6")),
        auto_sync_minute=int(os.getenv("AUTO_SYNC_MINUTE", "0")),
        auto_sync_days=int(os.getenv("AUTO_SYNC_DAYS", "127")),
        # tracked_playlists cannot come from env vars — read from config.json directly.
        # It is populated by _run_auto_sync() and the Go binding reads it from the
        # JSON config file before spawning the Python process.
        tracked_playlists=[],
        provider_stats_enabled=os.getenv("PROVIDER_STATS_ENABLED", "true").lower() == "true",
        provider_stats_db_path=os.getenv("PROVIDER_STATS_DB_PATH", ""),
        library_index_enabled=os.getenv("LIBRARY_INDEX_ENABLED", "true").lower() == "true",
        library_index_db_path=os.getenv("LIBRARY_INDEX_DB_PATH", ""),
    )
    return cfg
