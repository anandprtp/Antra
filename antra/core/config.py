"""
Configuration management via .env / environment variables.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    # Load with override=True to ensure manual edits to .env
    # are picked up if load_config is called multiple times.
    load_dotenv(override=True)
except ImportError:
    pass


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SPOTIFY_AUTH_PATH = str(REPO_ROOT / ".antra_auth.json")

try:
    from platformdirs import user_data_dir
    _data_dir = user_data_dir("Antra", "Antra")
except Exception:
    _data_dir = str(REPO_ROOT)
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

    # DAB Music download adapter (optional — community Qobuz proxy based).
    # Disabled by default because these public endpoints are no longer used
    # for downloads. The adapter remains available as an explicit opt-in.
    dab_enabled: bool = False

    # Qobuz Proxy (optional — no credentials needed, free FLAC via community
    # Qobuz proxy endpoints: dab.yeet.su/api/stream, dabmusic.xyz, qobuz.squid.wtf)
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
    retry_delay: float = 2.0
    fetch_lyrics: bool = True
    enrich_album_data: bool = True
    source_preference: str = "auto"
    output_format: str = "flac"

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

    # Library deduplication mode:
    #   "smart_dedup"  — skip a track if the same ISRC/ID exists anywhere in the library (default)
    #   "full_albums"  — only skip if the file exists in the exact target folder; allows the same
    #                    track to exist in multiple album folders (e.g. studio album + Best Of)
    library_mode: str = "smart_dedup"

    # Folder structure layout:
    #   "standard" — Artist / Album (Year) / files  (default, Navidrome/Jellyfin/Plex compatible)
    #   "flat"     — Album (Year) / files  (no artist wrapper, simpler manual organisation)
    folder_structure: str = "standard"

    # Filename format for downloaded tracks:
    #   "default"      — NN - Title  (track-number prefix, current behaviour)
    #   "title_only"   — Title
    #   "artist_title" — Artist - Title
    #   "title_artist" — Title - Artist
    filename_format: str = "default"

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
        dab_enabled=os.getenv("DAB_ENABLED", "false").lower() == "true",
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
        retry_delay=float(os.getenv("RETRY_DELAY", "2.0")),
        fetch_lyrics=os.getenv("FETCH_LYRICS", "true").lower() == "true",
        enrich_album_data=os.getenv("ENRICH_ALBUM_DATA", "true").lower() == "true",
        source_preference=os.getenv("SOURCE_PREFERENCE", "auto"),
        output_format=os.getenv("OUTPUT_FORMAT", "flac"),
        soulseek_base_url=os.getenv("SLSKD_BASE_URL", ""),
        soulseek_api_key=os.getenv("SLSKD_API_KEY", ""),
        soulseek_username=os.getenv("SOULSEEK_USERNAME", ""),
        soulseek_password=os.getenv("SOULSEEK_PASSWORD", ""),
        soulseek_auto_bootstrap=os.getenv("SLSKD_AUTO_BOOTSTRAP", "true").lower() == "true",
        soulseek_seed_after_download=os.getenv("SOULSEEK_SEED_AFTER_DOWNLOAD", "false").lower() == "true",
        sources_enabled=os.getenv("SOURCES_ENABLED", ""),
        prefer_explicit=os.getenv("PREFER_EXPLICIT", "true").lower() == "true",
        library_mode=os.getenv("LIBRARY_MODE", "smart_dedup"),
        folder_structure=os.getenv("FOLDER_STRUCTURE", "standard"),
        filename_format=os.getenv("FILENAME_FORMAT", "default"),
        amazon_direct_creds_json=os.getenv("AMAZON_DIRECT_CREDS_JSON", ""),
        amazon_wvd_path=os.getenv("AMAZON_WVD_PATH", ""),
        apple_authorization_token=os.getenv("APPLE_AUTHORIZATION_TOKEN", ""),
        apple_music_user_token=os.getenv("APPLE_MUSIC_USER_TOKEN", ""),
        apple_storefront=os.getenv("APPLE_STOREFRONT", "us"),
        apple_wvd_path=os.getenv("APPLE_WVD_PATH", ""),
    )
    return cfg
