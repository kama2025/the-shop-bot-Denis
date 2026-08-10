"""Админка: тексты, настройки, каналы подписки."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.handlers.admin.common import guard
from bot.keyboards import admin as admin_kb
from bot.repo import audit as audit_repo
from bot.repo import content as content_repo
from bot.services import header as header_service
from bot.services.access import Actor
from bot.services.settings_store import DEFAULT_SETTINGS, settings_store
from bot.services.texts import text_service
from bot.states.admin import ChannelSG, SettingSG, TextSG
from bot.utils.pagination import paginate
from bot.utils.render import show

router = Router(name="admin.content")

PER_PAGE = 8


# --- тексты -----------------------------------------------------------------


@router.callback_query(F.data.startswith("a:texts:"))
async def list_texts(call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object) -> None:
    if not await guard(call, actor, "texts", "list"):
        return
    await call.answer()
    page = int(call.data.split(":")[2])
    items = await content_repo.all_texts(session)
    chunk = paginate(items, page, PER_PAGE)
    await show(
        call,
        f"📝 <b>Тексты бота</b> — всего {chunk.total}\n\nВыберите текст, чтобы изменить.",
        admin_kb.texts(chunk.items, chunk.page, chunk.pages),
    )


@router.callback_query(F.data.startswith("a:text:"))
async def text_card(call: CallbackQuery, session: AsyncSession, actor: Actor, state: FSMContext, **_: object) -> None:
    if not await guard(call, actor, "texts", "view"):
        return
    await call.answer()
    key = call.data.split(":", 2)[2]
    entry = await content_repo.get_text(session, key)
    if entry is None:
        await call.answer("Текст не найден", show_alert=True)
        return

    await state.update_data(text_key=key)
    await state.set_state(TextSG.value)
    body = (
        f"📝 <b>{html.escape(entry.title)}</b>\n"
        f"Ключ: <code>{entry.key}</code>\n"
    )
    if entry.hint:
        body += f"Подсказка: {html.escape(entry.hint)}\n"
    body += (
        "\n<b>Сейчас:</b>\n"
        f"<code>{html.escape(entry.value)}</code>\n\n"
        "Отправьте новый текст сообщением. Поддерживается HTML-разметка Telegram "
        "(<code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, <code>&lt;code&gt;</code>). "
        "Подстановки в фигурных скобках оставляйте как есть."
    )
    await show(call, body, admin_kb.confirm("noop", "a:texts:0", yes_text="…"))


@router.message(TextSG.value)
async def save_text(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor, "texts", "act"):
        await state.clear()
        return
    data = await state.get_data()
    key = data.get("text_key")
    value = message.html_text if message.text else ""
    if not key or not value.strip():
        await message.answer("Пустой текст сохранить нельзя.")
        return

    await content_repo.set_text(session, key, value, actor.user_id)
    text_service.invalidate()
    await audit_repo.record(session, actor.user_id, "text.update", "text", key)
    await state.clear()
    await message.answer(f"✅ Текст «{key}» обновлён.")


# --- настройки --------------------------------------------------------------


@router.callback_query(F.data.startswith("a:settings:"))
async def list_settings(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor, "settings", "list"):
        return
    await call.answer()
    page = int(call.data.split(":")[2])
    items = await content_repo.all_settings(session)
    chunk = paginate(items, page, PER_PAGE)
    await show(
        call,
        f"⚙️ <b>Настройки</b> — всего {chunk.total}",
        admin_kb.settings(chunk.items, chunk.page, chunk.pages),
    )


@router.callback_query(F.data.startswith("a:set:"))
async def setting_card(
    call: CallbackQuery, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(call, actor, "settings", "view"):
        return
    await call.answer()
    key = call.data.split(":", 2)[2]
    entry = await content_repo.get_setting(session, key)
    if entry is None:
        await call.answer("Настройка не найдена", show_alert=True)
        return

    await state.update_data(setting_key=key)
    await state.set_state(SettingSG.value)
    hint = {
        "bool": "Ответьте <code>1</code> — включить, <code>0</code> — выключить.",
        "int": "Отправьте целое число.",
    }.get(entry.value_type, "Отправьте новое значение.")

    await show(
        call,
        f"⚙️ <b>{html.escape(entry.title)}</b>\n"
        f"Ключ: <code>{entry.key}</code>\n"
        f"Сейчас: <code>{html.escape(entry.value or '—')}</code>\n\n"
        + (f"{html.escape(entry.hint)}\n\n" if entry.hint else "")
        + hint,
        admin_kb.confirm("noop", "a:settings:0", yes_text="…"),
    )


@router.message(SettingSG.value)
async def save_setting(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor, "settings", "act"):
        await state.clear()
        return
    data = await state.get_data()
    key = data.get("setting_key")
    if not key:
        await state.clear()
        return

    raw = (message.text or "").strip()
    spec = DEFAULT_SETTINGS.get(key, {})
    kind = spec.get("type", "str")

    if kind == "int":
        if not raw.lstrip("-").isdigit():
            await message.answer("Нужно целое число.")
            return
    elif kind == "bool":
        if raw.lower() not in ("0", "1", "true", "false", "да", "нет", "on", "off"):
            await message.answer("Нужно 1 или 0.")
            return
        raw = "1" if raw.lower() in ("1", "true", "да", "on") else "0"

    await settings_store.set(session, key, raw, actor.user_id)
    await audit_repo.record(session, actor.user_id, "setting.update", "setting", key, {"value": raw})
    await state.clear()
    await message.answer(f"✅ Настройка «{key}» = <code>{html.escape(raw)}</code>")


@router.callback_query(F.data == "a:set_header")
async def ask_header(call: CallbackQuery, actor: Actor, state: FSMContext, **_: object) -> None:
    if not await guard(call, actor, "settings", "act"):
        return
    await call.answer()
    await state.set_state(SettingSG.header_image)
    await show(
        call,
        "🖼 Пришлите картинку-шапку. Она будет показываться на экранах магазина.",
        admin_kb.confirm("noop", "a:settings:0", yes_text="…"),
    )


@router.message(SettingSG.header_image)
async def save_header(
    message: Message,
    session: AsyncSession,
    actor: Actor,
    state: FSMContext,
    settings: Settings,
    **_: object,
) -> None:
    if not await guard(message, actor, "settings", "act"):
        await state.clear()
        return
    if not message.photo:
        await message.answer("Нужна именно картинка.")
        return
    path = await header_service.set_from_message(session, message, settings.media_dir)
    await audit_repo.record(session, actor.user_id, "setting.header", "setting", "header_image")
    await state.clear()
    await message.answer(f"✅ Шапка обновлена.\nФайл: <code>{html.escape(path)}</code>")


# --- каналы -----------------------------------------------------------------


@router.callback_query(F.data == "a:channels")
async def list_channels(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor, "channels", "list"):
        return
    await call.answer()
    items = await content_repo.list_channels(session, only_active=False)
    lines = ["📡 <b>Каналы для проверки подписки</b>", ""]
    if not items:
        lines.append("Ни одного канала — проверка подписки выключена, магазин открыт всем.")
    else:
        for channel in items:
            lines.append(
                f"• <code>{html.escape(channel.chat_ref)}</code> — {html.escape(channel.title)}"
            )
            if channel.last_error:
                lines.append(f"  ⚠️ {html.escape(channel.last_error[:120])}")
        lines.append("")
        lines.append(
            "⚠️ Бот обязан быть администратором каждого канала — иначе проверка "
            "невозможна. Канал с ошибкой не блокирует магазин, но и подписку по нему "
            "не проверяет."
        )
    await show(call, "\n".join(lines), admin_kb.channels(items))


@router.callback_query(F.data == "a:ch_add")
async def ask_channel(call: CallbackQuery, actor: Actor, state: FSMContext, **_: object) -> None:
    if not await guard(call, actor, "channels", "create"):
        return
    await call.answer()
    await state.set_state(ChannelSG.chat_ref)
    await show(
        call,
        "Шаг 1 из 3. Отправьте <code>@username</code> канала или его числовой ID "
        "(для приватных, вида <code>-1001234567890</code>).\n\n"
        "Бот уже должен быть администратором этого канала.",
        admin_kb.confirm("noop", "a:channels", yes_text="…"),
    )


@router.message(ChannelSG.chat_ref)
async def got_chat_ref(
    message: Message, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor, "channels", "create"):
        await state.clear()
        return
    chat_ref = (message.text or "").strip()
    if not chat_ref:
        await message.answer("Пусто. Отправьте @username или числовой ID.")
        return

    # Проверяем доступность сразу: канал, добавленный «вслепую», обнаружится
    # только когда покупатель упрётся в неработающую проверку.
    try:
        chat = await message.bot.get_chat(chat_ref)
    except Exception as exc:  # noqa: BLE001
        await message.answer(
            f"Не могу открыть этот канал: {html.escape(str(exc)[:200])}\n\n"
            "Проверьте, что бот добавлен администратором, и пришлите ссылку ещё раз."
        )
        return

    await state.update_data(chat_ref=chat_ref, title=chat.title or chat_ref)
    await state.set_state(ChannelSG.invite_url)
    await message.answer(
        f"Канал найден: <b>{html.escape(chat.title or chat_ref)}</b>\n\n"
        "Шаг 2 из 3. Отправьте ссылку для кнопки «Подписаться» "
        "(<code>https://t.me/...</code>)."
    )


@router.message(ChannelSG.invite_url)
async def got_invite(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor, "channels", "create"):
        await state.clear()
        return
    url = (message.text or "").strip()
    if not url.startswith(("https://t.me/", "http://t.me/", "https://telegram.me/")):
        await message.answer("Ссылка должна начинаться с https://t.me/")
        return

    data = await state.get_data()
    channel = await content_repo.add_channel(session, data["chat_ref"], data["title"], url)
    await audit_repo.record(
        session, actor.user_id, "channel.add", "channel", channel.id, {"chat_ref": channel.chat_ref}
    )
    await state.clear()
    await message.answer(f"✅ Канал «{html.escape(channel.title)}» добавлен в проверку подписки.")


@router.callback_query(F.data.startswith("a:ch_toggle:"))
async def toggle_channel(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor, "channels", "act"):
        return
    channel_id = int(call.data.split(":")[2])
    channel = await content_repo.get_channel(session, channel_id)
    if channel is None:
        await call.answer("Канал удалён", show_alert=True)
        return
    channel.is_active = not channel.is_active
    await call.answer("🟢 Включён" if channel.is_active else "🔴 Выключен")
    await list_channels(call, session, actor)


@router.callback_query(F.data.startswith("a:ch_del:"))
async def delete_channel(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor, "channels", "act"):
        return
    channel_id = int(call.data.split(":")[2])
    await content_repo.remove_channel(session, channel_id)
    await audit_repo.record(session, actor.user_id, "channel.delete", "channel", channel_id)
    await call.answer("Канал удалён")
    await list_channels(call, session, actor)
