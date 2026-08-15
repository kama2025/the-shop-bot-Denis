"""Общее для админ-панели: проверка прав и мелкие помощники.

Проверка вызывается **в каждом хендлере явно**, а не одним middleware на роутер.
Ролей больше нет, и соблазн заменить это middleware'ом велик — но callback-запрос
может отправить кто угодно, а хендлеры добавляются постоянно. Явный вызов
видно в diff'е; забытый middleware на новом роутере — нет.
"""

from __future__ import annotations

import logging

from aiogram.types import CallbackQuery, Message

from bot.services.access import Actor, allows

log = logging.getLogger(__name__)

DENIED = "🚫 Недостаточно прав"


async def guard(event: Message | CallbackQuery, actor: Actor) -> bool:
    """Возвращает True, если действие разрешено. Иначе сам отвечает отказом."""
    if allows(actor.is_admin):
        return True

    log.warning(
        "Отказ в доступе",
        extra={"user_id": actor.user_id, "is_admin": actor.is_admin},
    )
    if isinstance(event, CallbackQuery):
        await event.answer(DENIED, show_alert=True)
    else:
        await event.answer(DENIED)
    return False


def parse_ids(data: str, count: int) -> list[int]:
    """Достаёт числовые части из callback_data вида `a:prod_move:12:-1`."""
    parts = data.split(":")
    return [int(part) for part in parts[-count:]]
