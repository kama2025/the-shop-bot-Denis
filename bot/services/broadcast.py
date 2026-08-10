"""Рассылка.

Три вещи, без которых рассылка на несколько тысяч человек ломается.

1. **Скорость.** Telegram ограничивает бота примерно 30 сообщениями в секунду.
   Простой цикл `for user in users: send()` упирается в лимит, получает 429 и
   часть сообщений теряет. Здесь пауза между отправками и обработка `retry_after`.
2. **Состояние в базе, а не в памяти.** Каждый получатель — строка. После
   перезапуска рассылка продолжается с места остановки и не шлёт дубли.
3. **403 — это не ошибка, а факт.** Пользователь заблокировал бота; его нужно
   пометить и больше не беспокоить, иначе он вечно будет портить статистику.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import Broadcast, BroadcastStatus, TargetStatus
from bot.db.session import session_scope
from bot.repo import broadcast as broadcast_repo
from bot.repo import users as users_repo

log = logging.getLogger(__name__)

CONTENT_TEXT = "text"
CONTENT_PHOTO = "photo"
CONTENT_VIDEO = "video"


def build_markup(buttons: list | None) -> InlineKeyboardMarkup | None:
    """Собирает inline-кнопки рассылки.

    Формат хранения: `[{"text": "...", "url": "..."}]`. Кнопка без адреса
    пропускается — Telegram откажет во всей отправке из-за одной кривой строки.
    """
    if not buttons:
        return None
    rows = []
    for item in buttons:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        url = str(item.get("url", "")).strip()
        if not text or not url:
            continue
        rows.append([InlineKeyboardButton(text=text, url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def parse_buttons(raw: str) -> tuple[list[dict], list[str]]:
    """Разбирает кнопки из текста вида `Текст | https://...`, по одной в строке."""
    buttons: list[dict] = []
    errors: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" not in line:
            errors.append(f"Нет разделителя «|»: {line}")
            continue
        text, url = (part.strip() for part in line.split("|", 1))
        if not text or not url:
            errors.append(f"Пустой текст или ссылка: {line}")
            continue
        if not url.startswith(("http://", "https://", "tg://")):
            errors.append(f"Ссылка должна начинаться с http(s):// — {url}")
            continue
        buttons.append({"text": text, "url": url})
    return buttons, errors


async def send_one(bot: Bot, chat_id: int, broadcast: Broadcast) -> None:
    """Отправляет одно сообщение. Исключения пробрасывает наверх."""
    markup = build_markup(broadcast.buttons)
    if broadcast.content_type == CONTENT_PHOTO and broadcast.file_id:
        await bot.send_photo(
            chat_id, photo=broadcast.file_id, caption=broadcast.text or None, reply_markup=markup
        )
    elif broadcast.content_type == CONTENT_VIDEO and broadcast.file_id:
        await bot.send_video(
            chat_id, video=broadcast.file_id, caption=broadcast.text or None, reply_markup=markup
        )
    else:
        await bot.send_message(chat_id, broadcast.text or "", reply_markup=markup)


async def prepare(
    session: AsyncSession,
    admin_id: int,
    content_type: str,
    text: str | None,
    file_id: str | None,
    buttons: list[dict] | None,
) -> Broadcast:
    broadcast = await broadcast_repo.create(
        session, admin_id, content_type, text, file_id, buttons
    )
    recipients = await users_repo.all_reachable_ids(session)
    await broadcast_repo.enqueue_targets(session, broadcast.id, recipients)
    await session.refresh(broadcast)
    return broadcast


async def run(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    broadcast_id: int,
    rate_per_second: int,
    on_progress=None,
) -> None:
    """Выполняет рассылку. Запускается фоновой задачей."""
    delay = 1.0 / max(1, rate_per_second)
    processed = 0

    async with session_scope(session_factory) as session:
        await broadcast_repo.set_status(session, broadcast_id, BroadcastStatus.RUNNING)

    while True:
        async with session_scope(session_factory) as session:
            broadcast = await broadcast_repo.get(session, broadcast_id)
            if broadcast is None:
                return
            if broadcast.status != BroadcastStatus.RUNNING:
                log.info("Рассылка %s остановлена", broadcast_id)
                return
            targets = await broadcast_repo.next_pending(session, broadcast_id, limit=50)
            # Снимаем данные до выхода из сессии: отправка идёт вне транзакции,
            # держать её открытой на время сетевых вызовов нельзя.
            payload = (
                broadcast.content_type,
                broadcast.text,
                broadcast.file_id,
                list(broadcast.buttons or []),
            )
            batch = [(target.id, target.user_id) for target in targets]

        if not batch:
            break

        snapshot = Broadcast(
            content_type=payload[0], text=payload[1], file_id=payload[2], buttons=payload[3]
        )

        results: list[tuple[int, str, str | None, int]] = []
        for target_id, user_id in batch:
            status, error = await _send_with_retry(bot, user_id, snapshot)
            results.append((target_id, status, error, user_id))
            processed += 1
            await asyncio.sleep(delay)

        async with session_scope(session_factory) as session:
            for target_id, status, error, user_id in results:
                await broadcast_repo.mark_target(session, target_id, status, error)
                if status == TargetStatus.BLOCKED:
                    await users_repo.mark_bot_blocked(session, user_id, True)
            await broadcast_repo.refresh_counters(session, broadcast_id)

        if on_progress is not None:
            await on_progress(processed)

    async with session_scope(session_factory) as session:
        await broadcast_repo.refresh_counters(session, broadcast_id)
        await broadcast_repo.set_status(session, broadcast_id, BroadcastStatus.DONE)
    log.info("Рассылка %s завершена, обработано %s", broadcast_id, processed)


async def _send_with_retry(
    bot: Bot, user_id: int, broadcast: Broadcast, attempts: int = 3
) -> tuple[str, str | None]:
    for attempt in range(1, attempts + 1):
        try:
            await send_one(bot, user_id, broadcast)
            return TargetStatus.SENT, None
        except TelegramRetryAfter as exc:
            # Telegram сам сказал, сколько ждать. Своя догадка здесь только
            # усугубит ограничение.
            wait = float(getattr(exc, "retry_after", 1)) + 0.5
            log.warning("429 от Telegram, ждём %.1f с", wait)
            await asyncio.sleep(wait)
        except TelegramForbiddenError as exc:
            return TargetStatus.BLOCKED, str(exc)[:255]
        except Exception as exc:  # noqa: BLE001 — одна неудача не должна ронять рассылку
            if attempt == attempts:
                return TargetStatus.FAILED, str(exc)[:255]
            await asyncio.sleep(1.0)
    return TargetStatus.FAILED, "Не удалось отправить после повторов"


def format_report(broadcast: Broadcast) -> str:
    return "\n".join(
        [
            f"📣 <b>Рассылка #{broadcast.id}</b>",
            f"• Всего получателей: {broadcast.total}",
            f"• Доставлено: {broadcast.sent}",
            f"• Заблокировали бота: {broadcast.blocked}",
            f"• Ошибок: {broadcast.failed}",
            f"• Статус: {broadcast.status}",
        ]
    )
