"""Картинка-шапка бота.

Хранится дважды: файлом на диске (источник правды, переживает смену бота) и
`file_id` в настройках (кеш, чтобы не загружать файл при каждом экране).
`file_id` привязан к конкретному боту и протухает при смене токена — тогда
достаточно удалить кеш, файл на месте.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiogram.types import FSInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.settings_store import settings_store

log = logging.getLogger(__name__)

KEY_FILE_ID = "header_image_file_id"
KEY_PATH = "header_image_path"


async def photo(session: AsyncSession) -> str | FSInputFile | None:
    file_id = await settings_store.get(session, KEY_FILE_ID)
    if file_id:
        return file_id

    path_value = await settings_store.get(session, KEY_PATH)
    if not path_value:
        return None
    path = Path(path_value)
    if not path.exists():
        log.warning("Файл шапки не найден: %s", path)
        return None
    return FSInputFile(str(path))


async def remember(session: AsyncSession, message: Message) -> None:
    """Запоминает `file_id` отправленной картинки, чтобы не грузить файл снова."""
    if not message.photo:
        return
    largest = message.photo[-1]
    current = await settings_store.get(session, KEY_FILE_ID)
    if current != largest.file_id:
        await settings_store.set(session, KEY_FILE_ID, largest.file_id)


async def set_from_message(session: AsyncSession, message: Message, media_dir: Path) -> str:
    """Сохраняет присланную админом картинку на диск и в кеш."""
    if not message.photo:
        raise ValueError("В сообщении нет картинки")
    largest = message.photo[-1]
    media_dir.mkdir(parents=True, exist_ok=True)
    target = media_dir / "header.jpg"
    await message.bot.download(largest, destination=target)
    await settings_store.set(session, KEY_PATH, str(target))
    await settings_store.set(session, KEY_FILE_ID, largest.file_id)
    return str(target)


async def clear_file_id(session: AsyncSession) -> None:
    await settings_store.set(session, KEY_FILE_ID, None)
