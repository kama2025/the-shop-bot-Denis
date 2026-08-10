"""Ловушка для всего, что не разобрали остальные роутеры.

Подключается последней. Кнопка, после которой ничего не происходит, читается
как поломка, а устаревшие клавиатуры приходят постоянно: после перезапуска,
после правки меню, из старой переписки.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import user as user_kb
from bot.services.access import Actor
from bot.services.texts import text_service

log = logging.getLogger(__name__)

router = Router(name="fallback")


@router.callback_query()
async def stale_button(call: CallbackQuery, **_: object) -> None:
    log.info("Необработанная кнопка: %s", call.data)
    await call.answer("Кнопка устарела — откройте меню заново", show_alert=False)


@router.message()
async def unknown_message(
    message: Message, session: AsyncSession, actor: Actor, **_: object
) -> None:
    text = await text_service.get(session, "shop_menu")
    await message.answer(text, reply_markup=user_kb.main_menu(actor.is_admin))
