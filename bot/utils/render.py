"""Показ экрана: новое сообщение или правка существующего.

Telegram не умеет превращать текстовое сообщение в сообщение с картинкой и
наоборот. Наивная правка падает с «there is no text in the message to edit», и
экран перестаёт открываться. Здесь этот случай обработан: когда тип меняется,
старое сообщение удаляется, а новое отправляется.

Ещё одно ограничение: подпись к картинке — 1024 символа против 4096 у текста.
Длинное описание товара с картинкой не помещается. Обрезать описание нельзя —
владелец писал его целиком, — поэтому картинка снимается, а текст остаётся.
"""

from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


async def show(
    event: Message | CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    photo: str | FSInputFile | None = None,
) -> Message | None:
    """Показывает экран пользователю.

    Возвращает отправленное сообщение (или None, если правили существующее).
    """
    if photo is not None and len(text) > CAPTION_LIMIT:
        photo = None
    text = text[:TEXT_LIMIT]

    if isinstance(event, CallbackQuery):
        message = event.message
        if message is None:
            return None
        return await _edit_or_resend(message, text, reply_markup, photo)

    return await _send(event, text, reply_markup, photo)


async def _send(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
    photo: str | FSInputFile | None,
) -> Message:
    if photo is not None:
        try:
            return await message.answer_photo(
                photo=photo, caption=text, reply_markup=reply_markup
            )
        except TelegramBadRequest as exc:
            # Например, протух file_id. Экран важнее картинки.
            log.warning("Не удалось отправить картинку, шлём текстом: %s", exc)
    return await message.answer(text, reply_markup=reply_markup)


async def _edit_or_resend(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
    photo: str | FSInputFile | None,
) -> Message | None:
    has_photo = bool(message.photo)
    wants_photo = photo is not None

    try:
        if wants_photo and has_photo:
            if isinstance(photo, str) and _same_photo(message, photo):
                await message.edit_caption(caption=text, reply_markup=reply_markup)
            else:
                await message.edit_media(
                    media=InputMediaPhoto(media=photo, caption=text),
                    reply_markup=reply_markup,
                )
            return None
        if not wants_photo and not has_photo:
            await message.edit_text(text, reply_markup=reply_markup)
            return None
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            # Пользователь нажал ту же кнопку ещё раз. Не ошибка.
            return None
        log.debug("Правка не прошла, пересоздаём сообщение: %s", exc)

    # Тип сообщения меняется — правкой не обойтись.
    try:
        await message.delete()
    except TelegramBadRequest:
        # Сообщение старше 48 часов удалить нельзя. Просто отправим новое.
        pass
    return await _send(message, text, reply_markup, photo)


def _same_photo(message: Message, file_id: str) -> bool:
    return any(size.file_id == file_id for size in (message.photo or []))


async def notify(event: Message | CallbackQuery, text: str, alert: bool = False) -> None:
    """Короткое уведомление, не меняющее экран."""
    if isinstance(event, CallbackQuery):
        await event.answer(text[:200], show_alert=alert)
        return
    await event.answer(text)
