import math
import re
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .models import PlaylistSession


_CALLBACK_RE = re.compile(
    r"^pl:(?P<token>[A-Za-z0-9_-]{16,32}):(?P<action>[tap])(?:[:](?P<value>[0-9]{1,4}))?$"
)


@dataclass(frozen=True)
class PlaylistCallback:
    token: str
    action: str
    value: int | None = None


def parse_playlist_callback(data: str) -> PlaylistCallback | None:
    match = _CALLBACK_RE.fullmatch(data or "")
    if match is None:
        return None
    action = match.group("action")
    raw_value = match.group("value")
    if action in {"t", "p"} and raw_value is None:
        return None
    if action == "a" and raw_value is not None:
        return None
    return PlaylistCallback(
        token=match.group("token"),
        action=action,
        value=int(raw_value) if raw_value is not None else None,
    )


def _clean(value: str) -> str:
    return " ".join((value or "").split())


def _track_name(session: PlaylistSession, index: int) -> str:
    track = session.tracks[index]
    artist = _clean(track.artist_string)
    title = _clean(track.title) or "Без названия"
    return f"{artist} — {title}" if artist else title


def _button_label(index: int, name: str, limit: int = 52) -> str:
    prefix = f"⬇️ {index + 1}. "
    room = max(1, limit - len(prefix))
    clipped = name if len(name) <= room else f"{name[: max(1, room - 1)]}…"
    return prefix + clipped


def render_playlist_page(
    session: PlaylistSession,
    page: int,
    page_size: int,
) -> tuple[str, InlineKeyboardMarkup]:
    total = len(session.tracks)
    page_count = max(1, math.ceil(total / page_size))
    page = min(max(page, 0), page_count - 1)
    start = page * page_size
    end = min(start + page_size, total)

    lines = [
        f"Плейлист: {(_clean(session.name) or 'YouTube Music')[:200]}",
        f"Треков: {total} • страница {page + 1}/{page_count}",
        "",
    ]
    keyboard: list[list[InlineKeyboardButton]] = []
    for index in range(start, end):
        name = _track_name(session, index)
        lines.append(f"{index + 1}. {name[:160]}")
        callback_data = f"pl:{session.token}:t:{index}"
        keyboard.append(
            [InlineKeyboardButton(_button_label(index, name), callback_data=callback_data)]
        )

    if page_count > 1:
        navigation: list[InlineKeyboardButton] = []
        if page > 0:
            navigation.append(
                InlineKeyboardButton("⬅️", callback_data=f"pl:{session.token}:p:{page - 1}")
            )
        if page + 1 < page_count:
            navigation.append(
                InlineKeyboardButton("➡️", callback_data=f"pl:{session.token}:p:{page + 1}")
            )
        keyboard.append(navigation)
    keyboard.append(
        [
            InlineKeyboardButton(
                f"⬇️ Скачать все ({total})",
                callback_data=f"pl:{session.token}:a",
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)
