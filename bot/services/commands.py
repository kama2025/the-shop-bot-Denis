"""Меню команд бота.

Telegram показывает список команд по кнопке «Меню» рядом с полем ввода.
Список задаётся отдельно для каждой **области видимости**, и это единственный
способ спрятать `/admin` от посторонних: сам обработчик команды посторонних и
так не пускает, но видеть её в меню им незачем.

Области, которые используются:

* `BotCommandScopeDefault` — все покупатели, без `/admin`;
* `BotCommandScopeChat` — личный чат каждого администратора, с `/admin`.

Список пересобирается при запуске и при каждом изменении состава
администраторов. Снятому администратору область явно удаляется — иначе
`/admin` останется у него в меню до перезапуска Telegram.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from sqlalchemy.ext.asyncio import AsyncSession

from bot.repo import users as users_repo

log = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand(command="start", description="Открыть магазин"),
    BotCommand(command="terms", description="Условия"),
    BotCommand(command="paysupport", description="Вопросы по оплате"),
    BotCommand(command="id", description="Мой Telegram ID"),
]

ADMIN_COMMANDS = [
    BotCommand(command="start", description="Открыть магазин"),
    BotCommand(command="admin", description="Админ-панель"),
    BotCommand(command="terms", description="Условия"),
    BotCommand(command="paysupport", description="Вопросы по оплате"),
    BotCommand(command="id", description="Мой Telegram ID"),
]


async def apply_default(bot: Bot) -> None:
    """Список для всех: без `/admin`."""
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())


async def grant(bot: Bot, user_id: int) -> bool:
    """Показывает `/admin` в личном чате администратора."""
    try:
        await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=user_id))
        return True
    except TelegramAPIError as exc:
        # Человек ещё не открывал бота — чата нет, задать область нечему.
        # Не ошибка: команды появятся при следующей синхронизации после /start.
        log.info("Не задали админское меню для %s: %s", user_id, exc)
        return False


async def revoke(bot: Bot, user_id: int) -> bool:
    """Убирает `/admin` у снятого администратора."""
    try:
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=user_id))
        return True
    except TelegramAPIError as exc:
        log.info("Не сняли админское меню у %s: %s", user_id, exc)
        return False


async def sync(bot: Bot, session: AsyncSession) -> int:
    """Пересобирает меню целиком. Возвращает число администраторов."""
    await apply_default(bot)
    admins = await users_repo.list_admins(session)
    for admin in admins:
        await grant(bot, admin.user_id)
        await asyncio.sleep(0.05)
    return len(admins)
