import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(ValueError):
    """Raised when the private bot is configured insecurely or incompletely."""


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _allowed_user_ids(raw: str) -> frozenset[int]:
    values: set[int] = set()
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            values.add(int(item))
        except ValueError as exc:
            raise ConfigError("ANTRA_TELEGRAM_ALLOWED_USER_IDS must contain numeric Telegram IDs") from exc
    return frozenset(values)


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    allowed_user_ids: frozenset[int]
    library_dir: Path
    claim_first_user: bool = False
    access_db_path: Path = Path(".antra_telegram_access.sqlite3")
    invite_ttl_seconds: int = 86_400
    resolve_mode: str = "library"
    delivery_mode: str = "auto"
    download_format: str = "mp3"
    fast_mode: bool = True
    split_large_audio: bool = True
    max_upload_bytes: int = 49_000_000
    max_query_chars: int = 200
    max_playlist_url_chars: int = 2048
    playlist_db_path: Path = Path(".antra_telegram_playlists.sqlite3")
    playlist_session_ttl_seconds: int = 86_400
    max_playlist_tracks: int = 100
    playlist_page_size: int = 10
    max_concurrent_jobs: int = 1
    max_pending_jobs: int = 20
    public_base_url: str = ""
    link_secret: bytes = b""
    link_ttl_seconds: int = 86_400
    bind_host: str = "127.0.0.1"
    bind_port: int = 8090

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        token = os.getenv("ANTRA_TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError("ANTRA_TELEGRAM_BOT_TOKEN is required")

        allowed = _allowed_user_ids(os.getenv("ANTRA_TELEGRAM_ALLOWED_USER_IDS", ""))
        claim_first_user = os.getenv(
            "ANTRA_TELEGRAM_CLAIM_FIRST_USER", "false"
        ).strip().lower() == "true"
        if not allowed and not claim_first_user:
            raise ConfigError(
                "set ANTRA_TELEGRAM_ALLOWED_USER_IDS or explicitly enable ANTRA_TELEGRAM_CLAIM_FIRST_USER"
            )

        resolve_mode = os.getenv("ANTRA_TELEGRAM_RESOLVE_MODE", "library").strip().lower()
        if resolve_mode not in {"library", "download"}:
            raise ConfigError("ANTRA_TELEGRAM_RESOLVE_MODE must be 'library' or 'download'")

        delivery_mode = os.getenv("ANTRA_TELEGRAM_DELIVERY_MODE", "auto").strip().lower()
        if delivery_mode not in {"auto", "audio", "vlc"}:
            raise ConfigError("ANTRA_TELEGRAM_DELIVERY_MODE must be auto, audio, or vlc")

        public_base_url = os.getenv("ANTRA_TELEGRAM_PUBLIC_BASE_URL", "").strip().rstrip("/")
        secret_text = os.getenv("ANTRA_TELEGRAM_LINK_SECRET", "").strip()
        if public_base_url:
            parsed_url = urlparse(public_base_url)
            if (
                parsed_url.scheme != "https"
                or not parsed_url.netloc
                or parsed_url.username
                or parsed_url.password
                or parsed_url.query
                or parsed_url.fragment
            ):
                raise ConfigError(
                    "ANTRA_TELEGRAM_PUBLIC_BASE_URL must be an HTTPS origin without credentials, query, or fragment"
                )
            if secret_text == "replace-with-at-least-32-random-characters" or len(secret_text) < 32:
                raise ConfigError(
                    "ANTRA_TELEGRAM_LINK_SECRET must be a unique secret of at least 32 characters when VLC links are enabled"
                )

        library_dir = Path(
            os.getenv(
                "ANTRA_TELEGRAM_LIBRARY_DIR",
                os.getenv("OUTPUT_DIR", "./Music"),
            )
        ).expanduser().resolve()
        access_db_path = Path(
            os.getenv(
                "ANTRA_TELEGRAM_ACCESS_DB",
                ".antra_telegram_access.sqlite3",
            )
        ).expanduser().resolve()
        playlist_page_size = _positive_int("ANTRA_TELEGRAM_PLAYLIST_PAGE_SIZE", 10)
        if playlist_page_size > 20:
            raise ConfigError("ANTRA_TELEGRAM_PLAYLIST_PAGE_SIZE must not exceed 20")
        max_playlist_tracks = _positive_int("ANTRA_TELEGRAM_MAX_PLAYLIST_TRACKS", 100)
        if max_playlist_tracks > 500:
            raise ConfigError("ANTRA_TELEGRAM_MAX_PLAYLIST_TRACKS must not exceed 500")

        return cls(
            bot_token=token,
            allowed_user_ids=allowed,
            library_dir=library_dir,
            claim_first_user=claim_first_user,
            access_db_path=access_db_path,
            invite_ttl_seconds=_positive_int("ANTRA_TELEGRAM_INVITE_TTL_SECONDS", 86_400),
            resolve_mode=resolve_mode,
            delivery_mode=delivery_mode,
            download_format=os.getenv("ANTRA_TELEGRAM_DOWNLOAD_FORMAT", "mp3").strip().lower(),
            fast_mode=_boolean("ANTRA_TELEGRAM_FAST_MODE", True),
            split_large_audio=_boolean("ANTRA_TELEGRAM_SPLIT_LARGE_AUDIO", True),
            max_upload_bytes=_positive_int("ANTRA_TELEGRAM_MAX_UPLOAD_BYTES", 49_000_000),
            max_query_chars=_positive_int("ANTRA_TELEGRAM_MAX_QUERY_CHARS", 200),
            max_playlist_url_chars=_positive_int("ANTRA_TELEGRAM_MAX_PLAYLIST_URL_CHARS", 2048),
            playlist_db_path=Path(
                os.getenv(
                    "ANTRA_TELEGRAM_PLAYLIST_DB",
                    str(access_db_path.with_name("playlist_sessions.sqlite3")),
                )
            ).expanduser().resolve(),
            playlist_session_ttl_seconds=_positive_int(
                "ANTRA_TELEGRAM_PLAYLIST_SESSION_TTL_SECONDS",
                86_400,
            ),
            max_playlist_tracks=max_playlist_tracks,
            playlist_page_size=playlist_page_size,
            max_concurrent_jobs=_positive_int("ANTRA_TELEGRAM_MAX_CONCURRENT_JOBS", 1),
            max_pending_jobs=_positive_int("ANTRA_TELEGRAM_MAX_PENDING_JOBS", 20),
            public_base_url=public_base_url,
            link_secret=(secret_text or secrets.token_urlsafe(32)).encode("utf-8"),
            link_ttl_seconds=_positive_int("ANTRA_TELEGRAM_LINK_TTL_SECONDS", 86_400),
            bind_host=os.getenv("ANTRA_TELEGRAM_BIND_HOST", "127.0.0.1").strip(),
            bind_port=_positive_int("ANTRA_TELEGRAM_BIND_PORT", 8090),
        )
