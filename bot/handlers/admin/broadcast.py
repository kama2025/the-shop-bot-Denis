"""Админка: рассылки."""

from __future__ import annotations

import asyncio
import html

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import BroadcastStatus
from bot.handlers.admin.common import guard
from bot.keyboards import admin as admin_kb
from bot.repo import audit as audit_repo
from bot.repo import broadcast as broadcast_repo
from bot.services import broadcast as broadcast_service
from bot.services.access import Actor
from bot.states.admin import BroadcastSG
from bot.utils.render import show

router = Router(name="admin.broadcast")

SECTION = "broadcast"


@router.callback_query(F.data == "a:bc")
async def menu(call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object) -> None:
    if not await guard(call, actor, SECTION, "list"):
        return
    await call.answer()
    recent = await broadcast_repo.list_recent(session)
    await show(call, "📣 <b>Рассылки</b>", admin_kb.broadcast_menu(recent))


@router.callback_query(F.data == "a:bc_new")
async def ask_content(call: CallbackQuery, actor: Actor, state: FSMContext, **_: object) -> None:
    if not await guard(call, actor, SECTION, "create"):
        return
    await call.answer()
    await state.set_state(BroadcastSG.content)
    await show(
        call,
        "📣 <b>Новая рассылка</b>\n\n"
        "Пришлите то, что нужно разослать: текст, фото с подписью или видео с подписью.\n"
        "Форматирование сохранится.",
        admin_kb.confirm("noop", "a:bc", yes_text="…"),
    )


@router.message(BroadcastSG.content)
async def got_content(
    message: Message, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor, SECTION, "create"):
        await state.clear()
        return

    if message.photo:
        content_type, file_id = broadcast_service.CONTENT_PHOTO, message.photo[-1].file_id
        text = message.html_text if message.caption else None
    elif message.video:
        content_type, file_id = broadcast_service.CONTENT_VIDEO, message.video.file_id
        text = message.html_text if message.caption else None
    elif message.text:
        content_type, file_id, text = broadcast_service.CONTENT_TEXT, None, message.html_text
    else:
        await message.answer("Пока поддерживаются текст, фото и видео. Пришлите что-то из этого.")
        return

    await state.update_data(content_type=content_type, file_id=file_id, text=text)
    await state.set_state(BroadcastSG.buttons)
    await message.answer(
        "Добавить кнопки под сообщением?\n\n"
        "Пришлите по одной на строку в формате <code>Текст | https://ссылка</code>\n"
        "Или отправьте «-», чтобы обойтись без кнопок."
    )


@router.message(BroadcastSG.buttons)
async def got_buttons(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor, SECTION, "create"):
        await state.clear()
        return

    raw = (message.text or "").strip()
    buttons: list[dict] = []
    if raw != "-":
        buttons, errors = broadcast_service.parse_buttons(raw)
        if errors:
            await message.answer("Не разобрал строки:\n• " + "\n• ".join(errors[:5]))
            return

    data = await state.get_data()
    broadcast = await broadcast_service.prepare(
        session,
        admin_id=actor.user_id,
        content_type=data["content_type"],
        text=data.get("text"),
        file_id=data.get("file_id"),
        buttons=buttons or None,
    )
    await state.update_data(broadcast_id=broadcast.id)
    await state.set_state(BroadcastSG.confirm)

    # Предпросмотр показываем ровно тем же способом, каким пойдёт рассылка —
    # иначе «в предпросмотре было нормально» перестаёт что-либо значить.
    await message.answer("👀 <b>Предпросмотр:</b>")
    await broadcast_service.send_one(message.bot, message.chat.id, broadcast)
    await message.answer(
        f"Отправить рассылку <b>{broadcast.total}</b> пользователям?",
        reply_markup=admin_kb.broadcast_confirm(broadcast.id, broadcast.total),
    )


@router.callback_query(F.data.startswith("a:bc_send:"))
async def send(
    call: CallbackQuery,
    session: AsyncSession,
    actor: Actor,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    **_: object,
) -> None:
    if not await guard(call, actor, SECTION, "act"):
        return
    broadcast_id = int(call.data.split(":")[2])
    broadcast = await broadcast_repo.get(session, broadcast_id)
    if broadcast is None:
        await call.answer("Рассылка не найдена", show_alert=True)
        return
    if broadcast.status != BroadcastStatus.DRAFT:
        await call.answer("Эта рассылка уже запускалась", show_alert=True)
        return

    await audit_repo.record(
        session, actor.user_id, "broadcast.start", "broadcast", broadcast_id,
        {"total": broadcast.total},
    )
    await session.commit()
    await state.clear()
    await call.answer("Запускаю")

    # Фоновой задачей: рассылка на тысячи получателей идёт минутами, а хендлер
    # обязан ответить Telegram за секунды.
    asyncio.create_task(
        broadcast_service.run(bot, session_factory, broadcast_id, settings.broadcast_rate)
    )
    await show(
        call,
        f"📣 Рассылка #{broadcast_id} запущена на {broadcast.total} получателей.\n"
        "Прогресс обновляется по кнопке.",
        admin_kb.broadcast_running(broadcast_id),
    )


@router.callback_query(F.data.startswith("a:bc_view:"))
async def view(call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object) -> None:
    if not await guard(call, actor, SECTION, "view"):
        return
    await call.answer()
    broadcast_id = int(call.data.split(":")[2])
    broadcast = await broadcast_repo.refresh_counters(session, broadcast_id)
    if broadcast is None:
        await call.answer("Рассылка не найдена", show_alert=True)
        return
    await show(
        call,
        broadcast_service.format_report(broadcast),
        admin_kb.broadcast_running(broadcast_id),
    )


@router.callback_query(F.data.startswith("a:bc_stop:"))
async def stop(call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object) -> None:
    if not await guard(call, actor, SECTION, "act"):
        return
    broadcast_id = int(call.data.split(":")[2])
    await broadcast_repo.set_status(session, broadcast_id, BroadcastStatus.PAUSED)
    await audit_repo.record(session, actor.user_id, "broadcast.stop", "broadcast", broadcast_id)
    await call.answer("Останавливаю — текущая пачка досылается")
    call.data = f"a:bc_view:{broadcast_id}"
    await view(call, session, actor)


@router.callback_query(F.data.startswith("a:bc_cancel:"))
async def cancel(
    call: CallbackQuery, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(call, actor, SECTION, "act"):
        return
    broadcast_id = int(call.data.split(":")[2])
    await broadcast_repo.set_status(session, broadcast_id, BroadcastStatus.CANCELED)
    await state.clear()
    await call.answer("Рассылка отменена")
    await menu(call, session, actor)
