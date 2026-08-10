"""Сессия базы, пользователь и роль — один раз на обновление.

Одна сессия на всё обновление даёт две вещи: хендлер и сервисы работают в одной
транзакции (иначе резерв склада и создание заказа окажутся в разных, и половина
операции переживёт сбой второй половины), и пользователь читается один раз, а
не в каждом обращении.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.repo import users as users_repo
from bot.services.access import Actor, load_actor

log = logging.getLogger(__name__)


class ContextMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        session = self._factory()
        try:
            data["session"] = session
            data["session_factory"] = self._factory

            if tg_user is not None and not tg_user.is_bot:
                user, created = await users_repo.upsert_user(
                    session,
                    tg_id=tg_user.id,
                    username=tg_user.username,
                    first_name=tg_user.first_name,
                    last_name=tg_user.last_name,
                )
                data["user"] = user
                data["is_new_user"] = created
                data["actor"] = await load_actor(session, tg_user.id)
            else:
                data["user"] = None
                data["is_new_user"] = False
                data["actor"] = Actor(user_id=0, role=None)

            result = await handler(event, data)
            await session.commit()
            return result
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
