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

    # YAMS (yams.tf — Qobuz/Deezer backend, requires auth token)
    yams_enabled: bool = True
    yams_auth_token: str = ""

    # DAB Music (optional — no credentials needed, free FLAC via dab.yeet.su)
    dab_enabled: bool = True

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
    apple_mirrors: list[str] = field(default_factory=lambda: [
        "https://apple.squid.wtf",
        "https://appl.afkarxyz.qzz.io",
        "https://apple.rnb.su",
        "https://apple.vov.li",
    ])
    # Developer token for Apple Music Catalog API (used for playlist fetching).
    # Songs and albums work without this. Get one from developer.apple.com
    # OR leave blank — Antra will auto-extract one from the Apple Music web player.
    apple_developer_token: str = ""

    # Amazon Music (optional — no credentials needed, free FLAC via community proxy)
    amazon_enabled: bool = True
    amazon_mirrors: list[str] = field(default_factory=lambda: [
        "https://amzn.afkarxyz.qzz.io",
        "https://amzn.vov.li",
        "https://amzn.rnb.su",
        "https://amazon.squid.wtf"
    ])
    amazon_region: str = "US"  # US, UK, DE, FR, JP, CA, IT, ES, IN
    amazon_auth_method: str = "proxy"  # proxy, cookies
    amazon_cookies_path: str = ""
    amazon_insecure_mirrors: bool = True

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
        qobuz_email=os.getenv("QOBUZ_EMAIL", ""),
        qobuz_password=os.getenv("QOBUZ_PASSWORD", ""),
        qobuz_app_id=os.getenv("QOBUZ_APP_ID", "285473059"),
        qobuz_app_secret=os.getenv("QOBUZ_APP_SECRET", ""),
        qobuz_user_auth_token=os.getenv("QOBUZ_USER_AUTH_TOKEN", ""),
        deezer_arl_token=os.getenv("DEEZER_ARL_TOKEN", ""),
        tidal_email=os.getenv("TIDAL_EMAIL", ""),
        tidal_password=os.getenv("TIDAL_PASSWORD", ""),
        yams_enabled=os.getenv("YAMS_ENABLED", "true").lower() == "true",
        yams_auth_token=os.getenv("YAMS_AUTH_TOKEN", ""),
        dab_enabled=os.getenv("DAB_ENABLED", "true").lower() == "true",
        qobuz_proxy_enabled=os.getenv("QOBUZ_PROXY_ENABLED", "true").lower() == "true",
        jiosaavn_enabled=os.getenv("JIOSAAVN_ENABLED", "true").lower() == "true",
        jiosaavn_quality=os.getenv("JIOSAAVN_QUALITY", "320"),
        odesli_api_key=os.getenv("ODESLI_API_KEY", ""),
        soundcloud_client_id=os.getenv("SOUNDCLOUD_CLIENT_ID", ""),
        musixmatch_api_key=os.getenv("MUSIXMATCH_API_KEY", ""),
        genius_api_key=os.getenv("GENIUS_API_KEY", ""),
        output_dir=os.getenv("OUTPUT_DIR", "./Music"),
        spotfetch_mirrors=os.getenv(
            "SPOTFETCH_MIRRORS",
            "https://sp.afkarxyz.qzz.io/api,https://sp.vov.li/api,https://sp.rnb.su/api,https://spotify.squid.wtf/api",
        ).split(","),
        apple_enabled=os.getenv("APPLE_ENABLED", "true").lower() == "true",
        apple_mirrors=os.getenv("APPLE_MIRRORS", "https://apple.squid.wtf,https://appl.afkarxyz.qzz.io,https://apple.rnb.su,https://apple.vov.li").split(","),
        apple_developer_token=os.getenv("APPLE_DEVELOPER_TOKEN", ""),
        amazon_enabled=os.getenv("AMAZON_ENABLED", "true").lower() == "true",
        amazon_mirrors=os.getenv("AMAZON_MIRRORS", "https://amzn.afkarxyz.qzz.io,https://amzn.vov.li,https://amzn.rnb.su,https://amazon.squid.wtf").split(","),
        amazon_region=os.getenv("AMAZON_REGION", "US"),
        amazon_auth_method=os.getenv("AMAZON_AUTH_METHOD", "proxy"),
        amazon_cookies_path=os.getenv("AMAZON_COOKIES_PATH", ""),
        amazon_insecure_mirrors=os.getenv("AMAZON_INSECURE_MIRRORS", "true").lower() == "true",
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
    )
    return cfg
