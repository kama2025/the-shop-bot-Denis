"""Простое ограничение частоты обращений.

Защищает не от злоумышленника, а от залипшей кнопки и от дребезга: три нажатия
«Купить» подряд не должны создавать три заказа и трижды резервировать склад.

Хранится в Redis, потому что после перезапуска бот не должен забывать, кто
только что нажимал.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from redis.asyncio import Redis

log = logging.getLogger(__name__)

_PREFIX = "throttle:"


class ThrottleMiddleware(BaseMiddleware):
    def __init__(self, redis: Redis | None, rate_ms: int = 400) -> None:
        self._redis = redis
        self._rate_ms = rate_ms

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if self._redis is None or not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)

        tg_user = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        key = f"{_PREFIX}{tg_user.id}"
        try:
            # SET NX PX — атомарно: «поставь, если ключа нет, и удали через N мс».
            acquired = await self._redis.set(key, "1", px=self._rate_ms, nx=True)
        except Exception as exc:  # noqa: BLE001 — сбой Redis не должен закрывать магазин
            log.warning("Redis недоступен при троттлинге: %s", exc)
            return await handler(event, data)

        if not acquired:
            if isinstance(event, CallbackQuery):
                await event.answer("Слишком быстро 🙂", show_alert=False)
            return None

        return await handler(event, data)
