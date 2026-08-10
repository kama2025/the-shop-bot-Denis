"""Общее для админ-панели: проверка прав и мелкие помощники.

Проверка прав вызывается **в каждом хендлере явно**, а не одним middleware на
роутер. Причина в спеке: право проверяется на четырёх дверях отдельно — открыть
запись, показать список, выполнить действие, создать. Middleware знает только
«админ или нет» и пропустит обычного администратора в раздел, который должен
быть доступен лишь владельцу.
"""

from __future__ import annotations

import logging

from aiogram.types import CallbackQuery, Message

from bot.services.access import Actor, allows

log = logging.getLogger(__name__)

DENIED = "🚫 Недостаточно прав"


async def guard(
    event: Message | CallbackQuery, actor: Actor, section: str, door: str
) -> bool:
    """Возвращает True, если действие разрешено. Иначе сам отвечает отказом."""
    if allows(actor.role, section, door):
        return True

    log.warning(
        "Отказ в доступе",
        extra={
            "user_id": actor.user_id,
            "role": actor.role,
            "section": section,
            "door": door,
        },
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
