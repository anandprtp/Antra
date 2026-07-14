import asyncio
import logging

from telegram import Update
from telegram.constants import ChatAction, ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .access import AccessStore, AccessStoreError
from .config import TelegramConfig
from .delivery import DeliveryKind, choose_delivery
from .jobs import JobCoordinator, MusicRequestError, PendingQueueFull
from .library import LibraryIndex
from .media import MediaRegistry, MediaServer
from .models import TrackAsset


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
    ):
        self.config = config
        self.access_store = access_store
        self.library = library
        self.coordinator = coordinator
        self.registry = registry
        self.media_server = media_server

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
        await update.effective_message.reply_text(
            "Напишите название песни и исполнителя. Я сначала проверю вашу локальную "
            "библиотеку, затем верну аудио или защищённую ссылку для VLC."
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        await update.effective_message.reply_text(
            "Пример: Massive Attack Teardrop\n"
            "/status — проверить состояние бота\n"
            "/files — показать треки в локальной библиотеке\n"
            "/rescan — перечитать локальную музыкальную библиотеку\n"
            "/invite — создать одноразовую ссылку для нового участника\n"
            "/members — показать администратора и участников"
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
        if len(query) > self.config.max_query_chars:
            await message.reply_text(
                f"Запрос слишком длинный; максимум {self.config.max_query_chars} символов."
            )
            return

        status = await message.reply_text(f"Ищу: {query}")
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

    async def _deliver(self, message, asset: TrackAsset) -> None:
        decision = choose_delivery(
            asset.path,
            self.config.max_upload_bytes,
            self.config.delivery_mode,
        )
        if decision.kind == DeliveryKind.VLC:
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
