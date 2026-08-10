"""Отправка купленного покупателю.

Вынесено отдельно, потому что вызывается из трёх мест: кнопка «Проверить
оплату», фоновый поллер и ручная выдача администратором. Три копии этой логики
разъехались бы, и в одной из них файлы перестали бы доходить.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from bot.services.delivery import DeliveredItem

log = logging.getLogger(__name__)

KIND_DOCUMENT = "document"
KIND_PHOTO = "photo"
KIND_VIDEO = "video"


async def send_items(
    bot: Bot, chat_id: int, items: list[DeliveredItem], caption: str | None = None
) -> int:
    """Отправляет файлы из выданных позиций. Возвращает число отправленных.

    Текстовые позиции здесь не отправляются: они уже попали в сообщение о
    выдаче. Дублировать их отдельным сообщением значит заставить покупателя
    искать, какое из двух настоящее.
    """
    files = [item for item in items if item.is_file]
    sent = 0
    for index, item in enumerate(files):
        text = caption if index == 0 else None
        try:
            await _send_one(bot, chat_id, item, text)
            sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(getattr(exc, "retry_after", 1)) + 0.5)
            try:
                await _send_one(bot, chat_id, item, text)
                sent += 1
            except TelegramAPIError as retry_exc:
                log.error(
                    "Файл заказа не доставлен после ожидания",
                    extra={"chat_id": chat_id, "error": str(retry_exc)},
                )
        except TelegramAPIError as exc:
            # Молча терять файл нельзя: покупатель заплатил и остался без
            # товара, а в журнале должно быть видно, за что именно.
            log.error(
                "Файл заказа не доставлен",
                extra={"chat_id": chat_id, "file_id": item.file_id, "error": str(exc)},
            )
        await asyncio.sleep(0.1)
    return sent


async def _send_one(
    bot: Bot, chat_id: int, item: DeliveredItem, caption: str | None
) -> None:
    if item.file_kind == KIND_PHOTO:
        await bot.send_photo(chat_id, photo=item.file_id, caption=caption)
    elif item.file_kind == KIND_VIDEO:
        await bot.send_video(chat_id, video=item.file_id, caption=caption)
    else:
        await bot.send_document(chat_id, document=item.file_id, caption=caption)


def extract_file(message) -> DeliveredItem | None:
    """Достаёт файл из сообщения администратора.

    Возвращает None, если файла нет, — вызывающий сам решает, ошибка это или
    допустимый случай.
    """
    caption = (message.caption or "").strip()

    if message.document is not None:
        return DeliveredItem(
            content=caption,
            file_id=message.document.file_id,
            file_kind=KIND_DOCUMENT,
            file_name=message.document.file_name or "файл",
        )
    if message.photo:
        largest = message.photo[-1]
        return DeliveredItem(
            content=caption,
            file_id=largest.file_id,
            file_kind=KIND_PHOTO,
            file_name="изображение",
        )
    if message.video is not None:
        return DeliveredItem(
            content=caption,
            file_id=message.video.file_id,
            file_kind=KIND_VIDEO,
            file_name=message.video.file_name or "видео",
        )
    if message.animation is not None:
        return DeliveredItem(
            content=caption,
            file_id=message.animation.file_id,
            file_kind=KIND_DOCUMENT,
            file_name=message.animation.file_name or "анимация",
        )
    if message.audio is not None:
        return DeliveredItem(
            content=caption,
            file_id=message.audio.file_id,
            file_kind=KIND_DOCUMENT,
            file_name=message.audio.file_name or "аудио",
        )
    return None
