"""Проверка доступа покупателя: блокировка, обслуживание, подписка.

Порядок проверок выбран так, чтобы более грубая причина отказа побеждала:
заблокированному пользователю бессмысленно предлагать подписаться.

Администраторы проходят гейт без проверок — иначе владелец, забывший подписаться
на собственный канал, не сможет починить настройки канала.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.keyboards import user as user_kb
from bot.services.settings_store import settings_store
from bot.services.subscription import SubscriptionService
from bot.services.texts import text_service

log = logging.getLogger(__name__)

# Что разрешено до прохождения гейта.
ALLOWED_COMMANDS = frozenset({"/start", "/terms", "/paysupport", "/support", "/admin"})
ALLOWED_CALLBACKS = ("u:sub_check", "u:menu", "noop")


class GateMiddleware(BaseMiddleware):
    def __init__(self, subscription: SubscriptionService) -> None:
        self._subscription = subscription

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)

        user = data.get("user")
        actor = data.get("actor")
        session = data["session"]
        if user is None:
            return await handler(event, data)

        # 1. Администраторы — мимо всех проверок.
        if actor is not None and actor.is_admin:
            return await handler(event, data)

        # 2. Блокировка магазином.
        if user.is_blocked:
            text = await text_service.get(session, "user_blocked")
            await _reply(event, text)
            return None

        # 3. Режим обслуживания.
        if await settings_store.get_bool(session, "maintenance", False):
            text = await text_service.get(session, "shop_closed")
            await _reply(event, text)
            return None

        # 4. Подписка на каналы.
        if _is_exempt(event):
            return await handler(event, data)

        bot = data["bot"]
        result = await self._subscription.check(session, bot, user.tg_id)
        if result.subscribed:
            return await handler(event, data)

        text = await text_service.get(session, "subscription_required")
        await _reply(event, text, reply_markup=user_kb.subscription(result.missing))
        return None


def _is_exempt(event: Message | CallbackQuery) -> bool:
    if isinstance(event, Message):
        raw = (event.text or event.caption or "").strip()
        command = raw.split()[0].split("@")[0] if raw else ""
        return command in ALLOWED_COMMANDS
    return bool(event.data and event.data.startswith(ALLOWED_CALLBACKS))


async def _reply(event: Message | CallbackQuery, text: str, reply_markup=None) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer()
        if event.message is not None:
            await event.message.answer(text, reply_markup=reply_markup)
        return
    await event.answer(text, reply_markup=reply_markup)
