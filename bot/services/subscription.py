"""Проверка подписки на каналы.

Три решения, каждое со своей причиной.

1. **Положительный результат кешируется, отрицательный — нет.** Иначе человек,
   только что подписавшийся, будет пять минут видеть отказ и решит, что бот
   сломан.
2. **Канал, который бот не может проверить, не блокирует магазин.** Если бота
   выкинули из администраторов канала, `getChatMember` начнёт отвечать ошибкой.
   Считать это «не подписан» — значит закрыть магазин для всех сразу. Ложный
   отказ ломает работу так же, как дыра, поэтому такой канал пропускается, а
   администраторы получают уведомление.
3. **Роль читается из базы, а не из кеша.** Администраторы освобождены от
   проверки, и снятый администратор теряет это освобождение сразу.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Channel
from bot.repo import content as content_repo

log = logging.getLogger(__name__)

MEMBER_STATUSES = frozenset({"creator", "administrator", "member", "restricted"})
_CACHE_PREFIX = "sub:ok:"


@dataclass
class SubscriptionResult:
    subscribed: bool
    missing: list[Channel] = field(default_factory=list)
    broken: list[tuple[Channel, str]] = field(default_factory=list)

    @property
    def has_broken(self) -> bool:
        return bool(self.broken)


class SubscriptionService:
    def __init__(self, redis: Redis | None, cache_seconds: int = 300) -> None:
        self._redis = redis
        self._cache_seconds = cache_seconds

    async def _cached_ok(self, user_id: int) -> bool:
        if self._redis is None:
            return False
        try:
            return bool(await self._redis.get(f"{_CACHE_PREFIX}{user_id}"))
        except Exception as exc:  # noqa: BLE001 — сбой кеша не должен ронять магазин
            log.warning("Redis недоступен при чтении кеша подписки: %s", exc)
            return False

    async def _remember_ok(self, user_id: int) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.setex(f"{_CACHE_PREFIX}{user_id}", self._cache_seconds, "1")
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis недоступен при записи кеша подписки: %s", exc)

    async def forget(self, user_id: int) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.delete(f"{_CACHE_PREFIX}{user_id}")
        except Exception:  # noqa: BLE001
            pass

    async def check(
        self, session: AsyncSession, bot: Bot, user_id: int, use_cache: bool = True
    ) -> SubscriptionResult:
        channels = await content_repo.list_channels(session, only_active=True)
        if not channels:
            return SubscriptionResult(subscribed=True)

        if use_cache and await self._cached_ok(user_id):
            return SubscriptionResult(subscribed=True)

        missing: list[Channel] = []
        broken: list[tuple[Channel, str]] = []

        for channel in channels:
            try:
                member = await bot.get_chat_member(chat_id=channel.chat_ref, user_id=user_id)
            except TelegramAPIError as exc:
                # Бот не админ, канал удалён, ссылка неверная — это наша
                # проблема настройки, а не вина покупателя.
                broken.append((channel, str(exc)))
                await content_repo.set_channel_error(session, channel.id, str(exc))
                log.warning(
                    "Канал недоступен для проверки подписки",
                    extra={"channel": channel.chat_ref, "error": str(exc)},
                )
                continue

            await content_repo.set_channel_error(session, channel.id, None)
            if getattr(member, "status", None) not in MEMBER_STATUSES:
                missing.append(channel)

        subscribed = not missing
        if subscribed:
            await self._remember_ok(user_id)
        return SubscriptionResult(subscribed=subscribed, missing=missing, broken=broken)
