import logging
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .access import AccessStore
from .bot import TelegramMusicBot
from .config import ConfigError, TelegramConfig
from .jobs import JobCoordinator, MusicResolver
from .library import LibraryIndex
from .media import MediaRegistry, MediaServer
from .models import TrackAsset
from .playlist_sessions import PlaylistSessionStore
from .security import LinkSigner
from .storage_db import StorageCatalog
from .telegram_storage import TelegramStorage
from .web_sessions import WebSessionStore


def _register_stored_tracks(
    registry: MediaRegistry,
    catalog: StorageCatalog,
    library_dir: Path,
) -> None:
    for stored in catalog.ready_tracks():
        local_asset = registry.get(stored.track_id)
        path = (
            local_asset.path
            if local_asset is not None
            else (
                library_dir
                / ".antra-telegram-cache"
                / stored.track_id
                / Path(stored.filename).name
            )
        )
        registry.register_stored(
            stored.track_id,
            TrackAsset(
                path,
                stored.title,
                stored.artist,
                stored.album,
                stored.duration_seconds,
                source=local_asset.source if local_asset is not None else "telegram",
            ),
        )


def build_application(config: TelegramConfig) -> Application:
    library = LibraryIndex(config.library_dir)
    library.refresh()

    signer = LinkSigner(config.link_secret)
    registry = MediaRegistry(config.library_dir, config.link_secret)
    registry.refresh()
    telegram_storage = None
    if config.storage_enabled:
        storage_catalog = StorageCatalog(config.storage_db_path)
        telegram_storage = TelegramStorage(
            storage_catalog,
            part_bytes=config.storage_part_bytes,
        )
        _register_stored_tracks(
            registry,
            storage_catalog,
            config.library_dir,
        )
    web_session_store = WebSessionStore(
        config.web_sessions_db_path,
        default_ttl_seconds=config.web_session_ttl_seconds,
    )
    access_store = AccessStore(
        config.access_db_path,
        static_allowed_user_ids=config.allowed_user_ids,
        allow_first_claim=config.claim_first_user,
    )
    media_server = MediaServer(
        registry=registry,
        signer=signer,
        public_base_url=config.public_base_url,
        bind_host=config.bind_host,
        bind_port=config.bind_port,
        link_ttl_seconds=config.link_ttl_seconds,
        web_session_store=web_session_store,
        cors_allowed_origins=(config.player_url,) if config.player_url else (),
        web_link_ttl_seconds=min(config.web_session_ttl_seconds, 86_400),
        player_upstream_url=config.player_upstream_url,
        player_base_url=config.player_url,
        access_store=access_store,
        telegram_storage=telegram_storage,
    )
    resolver = MusicResolver(config, library)
    coordinator = JobCoordinator(
        resolver,
        max_concurrent=config.max_concurrent_jobs,
        max_pending=config.max_pending_jobs,
    )
    playlist_store = PlaylistSessionStore(
        config.playlist_db_path,
        ttl_seconds=config.playlist_session_ttl_seconds,
        max_tracks=config.max_playlist_tracks,
    )
    bot = TelegramMusicBot(
        config,
        access_store,
        library,
        coordinator,
        registry,
        media_server,
        playlist_store,
        web_session_store=web_session_store,
        telegram_storage=telegram_storage,
    )

    async def post_init(application: Application) -> None:
        await media_server.start()
        await bot.start_background_tasks()

    async def stop_services(application: Application) -> None:
        # post_stop runs before python-telegram-bot closes its HTTP client.
        # Stop new restore requests first, then let uploads finish/cancel while
        # the Bot API is still usable. The same callback is an idempotent
        # post_shutdown fallback for non-standard lifecycle callers.
        await media_server.stop()
        await bot.shutdown()

    async def handle_error(
        update: object,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        logging.getLogger(__name__).exception(
            "Unhandled Telegram update error",
            exc_info=context.error,
        )

    application = (
        Application.builder()
        .token(config.bot_token)
        .concurrent_updates(max(2, min(config.max_pending_jobs, 8)))
        .post_init(post_init)
        .post_stop(stop_services)
        .post_shutdown(stop_services)
        .build()
    )
    media_server.configure_storage_bot(lambda: application.bot)
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("help", bot.help))
    application.add_handler(CommandHandler("status", bot.status))
    application.add_handler(CommandHandler("files", bot.files))
    application.add_handler(CommandHandler("invite", bot.invite))
    application.add_handler(CommandHandler("members", bot.members))
    application.add_handler(CommandHandler("rescan", bot.rescan))
    application.add_handler(CommandHandler("archive", bot.archive_library))
    application.add_handler(CommandHandler("player", bot.player))
    application.add_handler(
        CallbackQueryHandler(bot.handle_playlist_callback, pattern=r"^pl:")
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_text))
    application.add_handler(MessageHandler(filters.COMMAND, bot.unknown_command))
    application.add_error_handler(handle_error)
    return application


def main() -> None:
    load_dotenv(".env.telegram", override=False)
    load_dotenv(override=False)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        config = TelegramConfig.from_env()
    except ConfigError as exc:
        raise SystemExit(f"Telegram bot configuration error: {exc}") from exc
    build_application(config).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
