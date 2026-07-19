import asyncio
import logging
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .access import AccessStore, AccessStoreError
from .config import TelegramConfig
from .delivery import DeliveryKind, choose_delivery
from .jobs import (
    JobCoordinator,
    MusicRequestError,
    PendingQueueFull,
    youtube_music_input_kind,
)
from .library import LibraryIndex
from .media import MediaRegistry, MediaServer
from .models import PlaylistSession, TrackAsset
from .playlist_sessions import (
    PlaylistSessionError,
    PlaylistSessionStore,
    PlaylistTooLarge,
)
from .playlist_ui import parse_playlist_callback, render_playlist_page
from .splitter import AudioSplitError, split_audio_copy
from .telegram_storage import TelegramStorage
from .web_sessions import WebSessionStore


logger = logging.getLogger(__name__)


class TelegramMusicBot:
    def __init__(
        self,
        config: TelegramConfig,
        access_store: AccessStore,
        library: LibraryIndex,
        coordinator: JobCoordinator,
        registry: MediaRegistry,
        media_server: MediaServer,
        playlist_store: PlaylistSessionStore,
        web_session_store: WebSessionStore | None = None,
        telegram_storage: TelegramStorage | None = None,
    ):
        self.config = config
        self.access_store = access_store
        self.library = library
        self.coordinator = coordinator
        self.registry = registry
        self.media_server = media_server
        self.playlist_store = playlist_store
        self.web_session_store = web_session_store
        self.telegram_storage = telegram_storage
        self._playlist_action_lock = asyncio.Lock()
        self._playlist_actions: dict[str, set[int]] = {}
        self._storage_tasks: set[asyncio.Task] = set()

    async def _authorize(self, update: Update, *, require_admin: bool = False) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        if (
            user is not None
            and not user.is_bot
            and chat is not None
            and chat.type == ChatType.PRIVATE
        ):
            try:
                decision = await asyncio.to_thread(
                    self.access_store.authorize_or_claim,
                    user.id,
                )
            except AccessStoreError:
                logger.exception("Ownership check failed closed")
                decision = None
            if decision is not None and decision.allowed:
                if require_admin and decision.role != "admin":
                    await update.effective_message.reply_text("Эта команда доступна только администратору.")
                    return False
                if decision.claimed_admin and update.effective_message is not None:
                    await update.effective_message.reply_text(
                        "Готово: вы стали администратором этого бота."
                    )
                return True
        if update.effective_message is not None:
            await update.effective_message.reply_text("Доступ к этому приватному боту закрыт.")
        return False

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        invite_payload = context.args[0] if context.args else ""
        if (
            invite_payload.startswith("invite_")
            and user is not None
            and not user.is_bot
            and chat is not None
            and chat.type == ChatType.PRIVATE
        ):
            try:
                decision = await asyncio.to_thread(
                    self.access_store.redeem_invite,
                    user.id,
                    invite_payload.removeprefix("invite_"),
                )
            except AccessStoreError:
                logger.exception("Invitation redemption failed closed")
                decision = None
            if decision is None or not decision.allowed:
                await update.effective_message.reply_text(
                    "Приглашение недействительно, уже использовано или истекло."
                )
                return
            if decision.joined_by_invite:
                await update.effective_message.reply_text("Приглашение принято — доступ добавлен.")

        if not await self._authorize(update):
            return
        reply_markup = None
        if self._player_is_ready():
            player_url = await asyncio.to_thread(
                self._player_url,
                update.effective_user.id,
                None,
            )
            reply_markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("▶ Открыть Antra Player", url=player_url)]]
            )
        await update.effective_message.reply_text(
            "Напишите название песни и исполнителя или отправьте публичную ссылку на "
            "плейлист YouTube Music. Для плейлиста появятся кнопки загрузки каждого "
            "трека и всех треков сразу.",
            reply_markup=reply_markup,
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        await update.effective_message.reply_text(
            "Пример: Massive Attack Teardrop\n"
            "Можно также отправить публичную ссылку на плейлист YouTube Music и "
            "выбрать отдельный трек или «Скачать все».\n"
            "/status — проверить состояние бота\n"
            "/files — показать треки в локальной библиотеке\n"
            "/rescan — перечитать локальную музыкальную библиотеку\n"
            "/player — открыть браузерный музыкальный плеер\n"
            "/invite — создать одноразовую ссылку для нового участника\n"
            "/members — показать администратора и участников"
        )

    async def player(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        if not self._player_is_ready():
            await update.effective_message.reply_text(
                "Web-плеер ещё не опубликован."
            )
            return
        user = update.effective_user
        url = await asyncio.to_thread(
            self._player_url,
            user.id,
            None,
        )
        await update.effective_message.reply_text(
            "Ваша медиатека готова.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("▶ Открыть Antra Player", url=url)]]
            ),
        )

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        await update.effective_message.reply_text(
            "Бот работает.\n"
            f"Треков в локальной библиотеке: {self.library.size}.\n"
            f"Режим поиска: {self.config.resolve_mode}."
        )

    async def files(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        assets = self.library.list_assets(limit=20)
        if not assets:
            await update.effective_message.reply_text(
                "Локальная библиотека пока пуста. Напишите название песни — бот попробует найти её через Antra."
            )
            return
        lines = [f"Треки в библиотеке ({self.library.size}):"]
        lines.extend(f"• {asset.display_name}" for asset in assets)
        if self.library.size > len(assets):
            lines.append(f"…и ещё {self.library.size - len(assets)}")
        await update.effective_message.reply_text("\n".join(lines))

    async def unknown_command(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._authorize(update):
            return
        await update.effective_message.reply_text(
            "Неизвестная команда. Используйте /help или просто напишите название песни."
        )

    async def invite(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update, require_admin=True):
            return
        user = update.effective_user
        try:
            token = await asyncio.to_thread(
                self.access_store.create_invite,
                user.id,
                self.config.invite_ttl_seconds,
            )
            username = context.bot.username
            if not username:
                username = (await context.bot.get_me()).username
            link = f"https://t.me/{username}?start=invite_{token}"
        except (AccessStoreError, PermissionError):
            logger.exception("Invitation creation failed")
            await update.effective_message.reply_text("Не удалось создать приглашение.")
            return
        hours = max(1, self.config.invite_ttl_seconds // 3600)
        await update.effective_message.reply_text(
            f"Одноразовая ссылка-приглашение (действует {hours} ч.):\n{link}"
        )

    async def members(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update, require_admin=True):
            return
        try:
            rows = await asyncio.to_thread(
                self.access_store.list_members,
                update.effective_user.id,
            )
        except (AccessStoreError, PermissionError):
            logger.exception("Member listing failed")
            await update.effective_message.reply_text("Не удалось прочитать список участников.")
            return
        lines = ["Пользователи:"]
        lines.extend(f"{user_id} — {role}" for user_id, role in rows)
        await update.effective_message.reply_text("\n".join(lines))

    async def rescan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        count = await asyncio.to_thread(self.library.refresh)
        await asyncio.to_thread(self.registry.refresh)
        await update.effective_message.reply_text(f"Библиотека обновлена: {count} треков.")

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        message = update.effective_message
        query = " ".join((message.text or "").split())
        if not query:
            return
        input_kind = youtube_music_input_kind(query)
        max_chars = (
            self.config.max_playlist_url_chars
            if input_kind in {"track", "collection", "invalid"}
            else self.config.max_query_chars
        )
        if len(query) > max_chars:
            await message.reply_text(
                f"Запрос слишком длинный; максимум {max_chars} символов."
            )
            return
        if input_kind == "invalid":
            await message.reply_text("Некорректная ссылка YouTube Music.")
            return
        if input_kind == "collection":
            await self._show_playlist(update, query)
            return

        status_text = (
            "Обрабатываю прямую ссылку YouTube Music…"
            if input_kind == "track"
            else f"Ищу: {query}"
        )
        status = await message.reply_text(status_text)
        await message.chat.send_action(ChatAction.TYPING)
        try:
            asset = await self.coordinator.resolve(query)
        except PendingQueueFull:
            await status.edit_text("Очередь занята. Попробуйте чуть позже.")
            return
        except MusicRequestError as exc:
            await status.edit_text(str(exc))
            return
        except Exception as exc:
            logger.exception("Music request failed")
            await status.edit_text("Не удалось обработать запрос. Подробности записаны в лог.")
            return

        if asset is None:
            await status.edit_text("Трек не найден в доступной библиотеке.")
            return

        await status.edit_text(f"Найдено: {asset.display_name}")
        await self._deliver(message, asset)

    async def _show_playlist(self, update: Update, url: str) -> None:
        message = update.effective_message
        status = await message.reply_text("Читаю плейлист YouTube Music…")
        await message.chat.send_action(ChatAction.TYPING)
        try:
            preview = await self.coordinator.preview_playlist(url)
            session = await asyncio.to_thread(
                self.playlist_store.create,
                update.effective_user.id,
                update.effective_chat.id,
                preview,
            )
            text, markup = render_playlist_page(
                session,
                page=0,
                page_size=self.config.playlist_page_size,
            )
            await status.edit_text(text, reply_markup=markup)
            bound = await asyncio.to_thread(
                self.playlist_store.bind_message,
                session.token,
                session.owner_user_id,
                session.chat_id,
                status.message_id,
            )
            if not bound:
                await status.edit_text("Не удалось активировать кнопки плейлиста.")
        except PendingQueueFull:
            await status.edit_text("Очередь занята. Попробуйте чуть позже.")
        except MusicRequestError as exc:
            await status.edit_text(str(exc))
        except PlaylistTooLarge as exc:
            await status.edit_text(str(exc))
        except PlaylistSessionError:
            logger.exception("Playlist session creation failed")
            await status.edit_text("Не удалось сохранить кнопки плейлиста.")
        except Exception:
            logger.exception("Playlist preview failed")
            await status.edit_text("Не удалось прочитать этот плейлист.")

    async def handle_playlist_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        parsed = parse_playlist_callback(query.data if query is not None else "")
        if query is None or parsed is None:
            if query is not None:
                await query.answer("Кнопка недействительна.", show_alert=True)
            return
        if not await self._authorize(update):
            await query.answer("Доступ закрыт.", show_alert=True)
            return
        user = update.effective_user
        chat = update.effective_chat
        message = query.message
        if user is None or chat is None or message is None:
            await query.answer("Кнопка недействительна.", show_alert=True)
            return
        try:
            session = await asyncio.to_thread(
                self.playlist_store.get,
                parsed.token,
                user.id,
                chat.id,
                message.message_id,
            )
        except PlaylistSessionError:
            logger.exception("Playlist callback state lookup failed")
            await query.answer("Список временно недоступен.", show_alert=True)
            return
        if session is None:
            await query.answer("Кнопка недействительна или устарела.", show_alert=True)
            return

        if parsed.action == "p":
            page_count = max(
                1,
                (len(session.tracks) + self.config.playlist_page_size - 1)
                // self.config.playlist_page_size,
            )
            if parsed.value is None or parsed.value >= page_count:
                await query.answer("Страница недействительна.", show_alert=True)
                return
            text, markup = render_playlist_page(
                session,
                page=parsed.value,
                page_size=self.config.playlist_page_size,
            )
            await query.answer()
            await query.edit_message_text(text, reply_markup=markup)
            return

        if parsed.action == "t":
            if parsed.value is None or parsed.value >= len(session.tracks):
                await query.answer("Трек недействителен.", show_alert=True)
                return
            claimed = await self._claim_playlist_action(session.token, parsed.value)
            if not claimed:
                await query.answer("Этот трек уже скачивается.", show_alert=True)
                return
            await query.answer("Добавлено в очередь")
            context.application.create_task(
                self._download_playlist_track(message, session, parsed.value),
                update=update,
                name=f"playlist-track-{session.token}-{parsed.value}",
            )
            return

        claimed = await self._claim_playlist_action(session.token, -1)
        if not claimed:
            await query.answer("Этот плейлист уже обрабатывается.", show_alert=True)
            return
        await query.answer("Все треки добавлены в очередь")
        context.application.create_task(
            self._download_playlist_all(message, session),
            update=update,
            name=f"playlist-all-{session.token}",
        )

    async def _claim_playlist_action(self, token: str, index: int) -> bool:
        async with self._playlist_action_lock:
            active = self._playlist_actions.setdefault(token, set())
            if index == -1:
                if active:
                    return False
            elif -1 in active or index in active:
                return False
            active.add(index)
            return True

    async def _release_playlist_action(self, token: str, index: int) -> None:
        async with self._playlist_action_lock:
            active = self._playlist_actions.get(token)
            if active is None:
                return
            active.discard(index)
            if not active:
                self._playlist_actions.pop(token, None)

    async def _download_playlist_track(
        self,
        message,
        session: PlaylistSession,
        index: int,
    ) -> None:
        track = session.tracks[index]
        status = await message.reply_text(
            f"Скачиваю {index + 1}/{len(session.tracks)}: {track.artist_string} — {track.title}"
        )
        try:
            asset = await self.coordinator.resolve_playlist_track(
                session.token,
                index,
                track,
            )
            if asset is None:
                await status.edit_text(f"Не удалось скачать: {track.artist_string} — {track.title}")
                return
            await status.edit_text(f"Готово: {asset.display_name}")
            await self._deliver(message, asset)
        except PendingQueueFull:
            await status.edit_text("Очередь занята. Попробуйте чуть позже.")
        except Exception:
            logger.exception("Playlist track download failed")
            await status.edit_text("Не удалось скачать выбранный трек.")
        finally:
            await self._release_playlist_action(session.token, index)

    async def _download_playlist_all(self, message, session: PlaylistSession) -> None:
        total = len(session.tracks)
        ready = 0
        failed = 0
        status = await message.reply_text(f"Скачиваю плейлист: 0/{total}")
        try:
            for index, track in enumerate(session.tracks):
                await status.edit_text(
                    f"Скачиваю плейлист: {index}/{total}\n"
                    f"Сейчас: {track.artist_string} — {track.title}"
                )
                try:
                    asset = await self.coordinator.resolve_playlist_track(
                        session.token,
                        index,
                        track,
                    )
                    if asset is None:
                        failed += 1
                        continue
                    ready += 1
                    await self._deliver(message, asset)
                except PendingQueueFull:
                    failed += 1
                except Exception:
                    failed += 1
                    logger.exception("Playlist item %s failed", index)
                await status.edit_text(
                    f"Обработано {index + 1}/{total} • готово {ready} • ошибок {failed}"
                )
            await status.edit_text(
                f"Плейлист обработан: готово {ready}/{total} • ошибок {failed}."
            )
        finally:
            await self._release_playlist_action(session.token, -1)

    async def _deliver(self, message, asset: TrackAsset) -> None:
        if self.config.delivery_mode == "player" and self._player_is_ready():
            await self._send_player(message, asset)
            return
        decision = choose_delivery(
            asset.path,
            self.config.max_upload_bytes,
            self.config.delivery_mode,
        )
        if decision.kind == DeliveryKind.VLC:
            if self.config.public_base_url:
                await self._send_vlc(message, asset)
            elif self.config.split_large_audio:
                await self._send_split_audio(message, asset)
            else:
                await self._send_vlc(message, asset)
            return

        try:
            await message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
            with asset.path.open("rb") as audio_file:
                if decision.kind == DeliveryKind.AUDIO:
                    await message.reply_audio(
                        audio=audio_file,
                        filename=asset.path.name,
                        title=asset.title,
                        performer=asset.artist or None,
                        duration=int(asset.duration_seconds) if asset.duration_seconds else None,
                        read_timeout=180,
                        write_timeout=180,
                    )
                else:
                    await message.reply_document(
                        document=audio_file,
                        filename=asset.path.name,
                        caption=asset.display_name,
                        read_timeout=180,
                        write_timeout=180,
                    )
        except TelegramError:
            logger.exception("Telegram upload failed; falling back to VLC")
            await self._send_vlc(message, asset)

    async def _send_split_audio(self, message, asset: TrackAsset) -> None:
        if not asset.duration_seconds:
            await message.reply_text(
                "Файл слишком большой, а его длительность не удалось определить для деления."
            )
            return
        notice = await message.reply_text(
            "Запись большая — делю её на части без перекодирования…"
        )
        try:
            with tempfile.TemporaryDirectory(prefix="antra-telegram-split-") as temp_dir:
                parts = await asyncio.to_thread(
                    split_audio_copy,
                    asset.path,
                    Path(temp_dir),
                    duration_seconds=asset.duration_seconds,
                    max_part_bytes=self.config.max_upload_bytes,
                )
                await notice.edit_text(
                    f"Отправляю запись частями: {len(parts)}."
                )
                for index, part in enumerate(parts, start=1):
                    await message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
                    caption = (
                        f"{asset.display_name}\n"
                        f"Часть {index}/{len(parts)}"
                    )
                    with part.open("rb") as audio_file:
                        await message.reply_document(
                            document=audio_file,
                            filename=(
                                f"{asset.path.stem} — часть {index:02d}"
                                f"{part.suffix}"
                            ),
                            caption=caption,
                            read_timeout=300,
                            write_timeout=300,
                        )
        except (AudioSplitError, OSError, TelegramError):
            logger.exception("Large audio split/upload failed")
            await notice.edit_text(
                "Не удалось разделить или отправить большой файл. Подробности записаны в лог."
            )

    def _player_is_ready(self) -> bool:
        return bool(
            self.config.player_url
            and self.config.public_base_url
            and self.web_session_store is not None
        )

    def _player_url(self, user_id: int, media_id: str | None) -> str:
        if not self._player_is_ready() or self.web_session_store is None:
            raise RuntimeError("web player is not configured")
        token = self.web_session_store.issue(user_id)
        fragment_values = {
            "token": token,
            "api": self.config.public_base_url,
        }
        if media_id:
            fragment_values["track"] = media_id
        return f"{self.config.player_url}/#{urlencode(fragment_values)}"

    async def _send_player(self, message, asset: TrackAsset) -> None:
        user = message.from_user
        if user is None:
            await message.reply_text("Не удалось определить пользователя плеера.")
            return
        media_id = self.registry.register(asset)
        url = await asyncio.to_thread(self._player_url, user.id, media_id)
        await message.reply_text(
            f"Сохранено в медиатеку:\n{asset.display_name}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("▶ Слушать в Antra", url=url)]]
            ),
        )
        if self.telegram_storage is not None:
            storage_chat_id = (
                self.config.storage_chat_id
                or await asyncio.to_thread(self.access_store.admin_id)
                or message.chat_id
            )
            task = asyncio.create_task(
                self._archive_in_telegram(
                    message.get_bot(),
                    asset,
                    media_id,
                    storage_chat_id,
                ),
                name=f"telegram-storage-{media_id}",
            )
            self._storage_tasks.add(task)
            task.add_done_callback(self._storage_tasks.discard)

    async def _archive_in_telegram(
        self,
        bot,
        asset: TrackAsset,
        media_id: str,
        chat_id: int,
    ) -> None:
        assert self.telegram_storage is not None
        try:
            await self.telegram_storage.archive(
                bot,
                asset,
                track_id=media_id,
                chat_id=chat_id,
            )
        except Exception:
            logger.exception(
                "Telegram storage archive failed for media %s",
                media_id,
            )

    async def _send_vlc(self, message, asset: TrackAsset) -> None:
        try:
            link = self.media_server.playlist_url(asset)
        except RuntimeError:
            await message.reply_text(
                "Файл слишком большой для Telegram, а публичный VLC URL ещё не настроен."
            )
            return
        await message.reply_text(
            f"VLC playlist для {asset.display_name}:\n{link}\n"
            f"Ссылка действует {self.config.link_ttl_seconds // 3600} ч."
        )
