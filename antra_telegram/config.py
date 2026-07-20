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


def _optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _load_or_create_link_secret(
    configured: str,
    secret_path: Path,
) -> bytes:
    if configured:
        if (
            configured == "replace-with-at-least-32-random-characters"
            or len(configured) < 32
        ):
            raise ConfigError(
                "ANTRA_TELEGRAM_LINK_SECRET must contain at least 32 "
                "non-placeholder characters"
            )
        return configured.encode("utf-8")

    path = secret_path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    generated = secrets.token_urlsafe(48)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ConfigError(
            f"could not create private link secret at {path}"
        ) from exc
    else:
        try:
            os.write(fd, f"{generated}\n".encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    read_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        read_flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, read_flags)
        try:
            secret = os.read(fd, 4096).decode("utf-8").strip()
        finally:
            os.close(fd)
        os.chmod(path, 0o600)
    except (OSError, UnicodeError) as exc:
        raise ConfigError(
            f"could not read private link secret at {path}"
        ) from exc
    if len(secret) < 32:
        raise ConfigError(
            f"private link secret at {path} is invalid or truncated"
        )
    return secret.encode("utf-8")


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
    player_url: str = ""
    player_upstream_url: str = ""
    web_sessions_db_path: Path = Path(".antra_telegram_web.sqlite3")
    web_session_ttl_seconds: int = 2_592_000
    storage_enabled: bool = False
    storage_chat_id: int | None = None
    storage_db_path: Path = Path(".antra_telegram_storage.sqlite3")
    storage_part_bytes: int = 18_000_000
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
        if delivery_mode not in {"auto", "audio", "vlc", "player"}:
            raise ConfigError(
                "ANTRA_TELEGRAM_DELIVERY_MODE must be auto, audio, vlc, or player"
            )

        access_db_path = Path(
            os.getenv(
                "ANTRA_TELEGRAM_ACCESS_DB",
                ".antra_telegram_access.sqlite3",
            )
        ).expanduser().resolve()
        public_base_url = os.getenv("ANTRA_TELEGRAM_PUBLIC_BASE_URL", "").strip().rstrip("/")
        secret_text = os.getenv("ANTRA_TELEGRAM_LINK_SECRET", "").strip()
        secret_path = Path(
            os.getenv(
                "ANTRA_TELEGRAM_LINK_SECRET_FILE",
                str(access_db_path.with_name("link_secret")),
            )
        )
        if public_base_url:
            parsed_url = urlparse(public_base_url)
            if (
                parsed_url.scheme != "https"
                or not parsed_url.netloc
                or parsed_url.username
                or parsed_url.password
                or parsed_url.path not in {"", "/"}
                or parsed_url.query
                or parsed_url.fragment
            ):
                raise ConfigError(
                    "ANTRA_TELEGRAM_PUBLIC_BASE_URL must be an HTTPS origin without credentials, query, or fragment"
                )
        player_url = os.getenv("ANTRA_TELEGRAM_PLAYER_URL", "").strip().rstrip("/")
        if player_url:
            parsed_player_url = urlparse(player_url)
            if (
                parsed_player_url.scheme != "https"
                or not parsed_player_url.netloc
                or parsed_player_url.username
                or parsed_player_url.password
                or parsed_player_url.path not in {"", "/"}
                or parsed_player_url.query
                or parsed_player_url.fragment
            ):
                raise ConfigError(
                    "ANTRA_TELEGRAM_PLAYER_URL must be an HTTPS origin without credentials, query, or fragment"
                )
            if not public_base_url:
                raise ConfigError(
                    "ANTRA_TELEGRAM_PUBLIC_BASE_URL is required when the web player is enabled"
                )
        player_upstream_url = os.getenv(
            "ANTRA_TELEGRAM_PLAYER_UPSTREAM_URL",
            "",
        ).strip().rstrip("/")
        if player_upstream_url:
            parsed_player_upstream = urlparse(player_upstream_url)
            if (
                parsed_player_upstream.scheme not in {"http", "https"}
                or not parsed_player_upstream.netloc
                or parsed_player_upstream.username
                or parsed_player_upstream.password
                or parsed_player_upstream.path not in {"", "/"}
                or parsed_player_upstream.query
                or parsed_player_upstream.fragment
            ):
                raise ConfigError(
                    "ANTRA_TELEGRAM_PLAYER_UPSTREAM_URL must be an HTTP(S) origin without credentials, path, query, or fragment"
                )
            if not player_url:
                raise ConfigError(
                    "ANTRA_TELEGRAM_PLAYER_URL is required when the player upstream is enabled"
                )

        library_dir = Path(
            os.getenv(
                "ANTRA_TELEGRAM_LIBRARY_DIR",
                os.getenv("OUTPUT_DIR", "./Music"),
            )
        ).expanduser().resolve()
        playlist_page_size = _positive_int("ANTRA_TELEGRAM_PLAYLIST_PAGE_SIZE", 10)
        if playlist_page_size > 20:
            raise ConfigError("ANTRA_TELEGRAM_PLAYLIST_PAGE_SIZE must not exceed 20")
        max_playlist_tracks = _positive_int("ANTRA_TELEGRAM_MAX_PLAYLIST_TRACKS", 100)
        if max_playlist_tracks > 500:
            raise ConfigError("ANTRA_TELEGRAM_MAX_PLAYLIST_TRACKS must not exceed 500")
        storage_enabled = _boolean("ANTRA_TELEGRAM_STORAGE_ENABLED", False)
        storage_part_bytes = _positive_int(
            "ANTRA_TELEGRAM_STORAGE_PART_BYTES",
            18_000_000,
        )
        if storage_enabled and storage_part_bytes > 19_000_000:
            raise ConfigError(
                "ANTRA_TELEGRAM_STORAGE_PART_BYTES must not exceed 19000000 in cloud mode"
            )
        link_secret = _load_or_create_link_secret(
            secret_text,
            secret_path,
        )

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
            player_url=player_url,
            player_upstream_url=player_upstream_url,
            web_sessions_db_path=Path(
                os.getenv(
                    "ANTRA_TELEGRAM_WEB_SESSIONS_DB",
                    str(access_db_path.with_name("web_sessions.sqlite3")),
                )
            ).expanduser().resolve(),
            web_session_ttl_seconds=_positive_int(
                "ANTRA_TELEGRAM_WEB_SESSION_TTL_SECONDS",
                2_592_000,
            ),
            storage_enabled=storage_enabled,
            storage_chat_id=_optional_int("ANTRA_TELEGRAM_STORAGE_CHAT_ID"),
            storage_db_path=Path(
                os.getenv(
                    "ANTRA_TELEGRAM_STORAGE_DB",
                    str(access_db_path.with_name("telegram_storage.sqlite3")),
                )
            ).expanduser().resolve(),
            storage_part_bytes=storage_part_bytes,
            link_secret=link_secret,
            link_ttl_seconds=_positive_int("ANTRA_TELEGRAM_LINK_TTL_SECONDS", 86_400),
            bind_host=os.getenv("ANTRA_TELEGRAM_BIND_HOST", "127.0.0.1").strip(),
            bind_port=_positive_int("ANTRA_TELEGRAM_BIND_PORT", 8090),
        )
