"""Уведомления администраторам.

Отдельный модуль, потому что уведомлять нужно из разных мест (оплата, нехватка
склада, сломанный канал), а правило «кому и молча ли» должно быть одно.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.repo import users as users_repo

log = logging.getLogger(__name__)


async def notify_admins(
    bot: Bot,
    session: AsyncSession,
    text: str,
    exclude: int | None = None,
    reply_markup=None,
) -> int:
    """Шлёт текст всем администраторам. Возвращает число доставленных.

    Ошибка доставки одному не должна мешать остальным и тем более ронять
    операцию, из которой уведомление вызвано: уведомление — побочный эффект,
    а не часть сделки.
    """
    admins = await users_repo.list_admins(session)
    delivered = 0
    for admin in admins:
        if exclude is not None and admin.user_id == exclude:
            continue
        try:
            await bot.send_message(admin.user_id, text, reply_markup=reply_markup)
            delivered += 1
        except TelegramAPIError as exc:
            log.warning("Не доставлено админу %s: %s", admin.user_id, exc)
        await asyncio.sleep(0.05)
    return delivered
