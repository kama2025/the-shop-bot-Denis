"""Вход в админ-панель и её главное меню."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import admin as admin_kb
from bot.repo import audit as audit_repo
from bot.services import stats as stats_service
from bot.services.access import Actor
from bot.utils.fsm import soft_reset
from bot.utils.money import format_kop
from bot.utils.render import show

router = Router(name="admin.menu")


def _header(actor: Actor) -> str:
    return f"🛠 <b>Админ-панель</b>\n<code>{actor.user_id}</code>"


@router.message(Command("admin"))
async def cmd_admin(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not actor.is_admin:
        # Молча игнорируем: подтверждать существование команды посторонним ни к чему.
        return
    await soft_reset(state)
    snapshot = await stats_service.collect(session)
    text = (
        f"{_header(actor)}\n\n"
        f"👥 Пользователей: {snapshot.users_total} (+{snapshot.users_today} сегодня)\n"
        f"💰 Выручка: {format_kop(snapshot.revenue_kop)}\n"
        f"📦 Свободных позиций: {snapshot.stock_available}"
    )
    await show(message, text, admin_kb.menu())


@router.callback_query(F.data == "a:menu")
async def open_menu(
    call: CallbackQuery, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not actor.is_admin:
        await call.answer("🚫 Недостаточно прав", show_alert=True)
        return
    await soft_reset(state)
    await call.answer()
    snapshot = await stats_service.collect(session)
    text = (
        f"{_header(actor)}\n\n"
        f"👥 Пользователей: {snapshot.users_total} (+{snapshot.users_today} сегодня)\n"
        f"💰 Выручка: {format_kop(snapshot.revenue_kop)}\n"
        f"📦 Свободных позиций: {snapshot.stock_available}"
    )
    await show(call, text, admin_kb.menu())


@router.callback_query(F.data == "a:audit")
async def open_audit(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    from bot.handlers.admin.common import guard

    if not await guard(call, actor):
        return
    await call.answer()
    entries = await audit_repo.recent(session, limit=25)
    if not entries:
        text = "📜 Журнал пуст."
    else:
        lines = ["📜 <b>Последние действия администраторов</b>", ""]
        for entry in entries:
            lines.append(
                f"{entry.created_at.strftime('%d.%m %H:%M')} · "
                f"<code>{entry.admin_id}</code> · {entry.action}"
                + (f" · {entry.entity}#{entry.entity_id}" if entry.entity else "")
            )
        text = "\n".join(lines)
    await show(call, text, admin_kb.confirm("a:audit", "a:menu", yes_text="🔄 Обновить"))
