"""Админка: пользователи, балансы, администраторы, статистика, выгрузка."""

from __future__ import annotations

import html
from datetime import timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.base import utcnow
from bot.db.models import AdminRole, BalanceTxnKind
from bot.handlers.admin.common import guard
from bot.keyboards import admin as admin_kb
from bot.repo import audit as audit_repo
from bot.repo import balance as balance_repo
from bot.repo import orders as orders_repo
from bot.repo import users as users_repo
from bot.services import export as export_service
from bot.services import stats as stats_service
from bot.services.access import Actor
from bot.states.admin import AdminSG, UserAdminSG
from bot.utils.money import PriceParseError, format_kop, parse_price_to_kop
from bot.utils.render import show

router = Router(name="admin.people")


# --- статистика -------------------------------------------------------------


@router.callback_query(F.data == "a:stats")
async def stats(call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object) -> None:
    if not await guard(call, actor, "stats", "view"):
        return
    await call.answer()
    snapshot = await stats_service.collect(session)
    text = stats_service.format_snapshot(snapshot)

    threshold = 3
    low = await stats_service.low_stock(session, threshold)
    if low:
        text += f"\n\n<b>Заканчивается (≤{threshold} шт.)</b>"
        for product, left in low[:10]:
            text += f"\n• {html.escape(product.title)} — {left} шт."

    await show(call, text, admin_kb.confirm("a:stats", "a:menu", yes_text="🔄 Обновить"))


# --- выгрузка ---------------------------------------------------------------


@router.callback_query(F.data == "a:export")
async def export_menu(call: CallbackQuery, actor: Actor, **_: object) -> None:
    if not await guard(call, actor, "export", "list"):
        return
    await call.answer()
    await show(call, "📤 <b>Выгрузка заказов в XLSX</b>", admin_kb.export_menu())


@router.callback_query(F.data.startswith("a:export_"))
async def do_export(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor, "export", "act"):
        return
    kind = call.data.split("_", 1)[1]
    await call.answer("Собираю файл…")

    now = utcnow()
    date_from = None
    if kind == "month":
        date_from = now - timedelta(days=30)
    elif kind == "today":
        date_from = now.replace(hour=0, minute=0, second=0, microsecond=0)

    content, count = await export_service.orders_workbook(session, date_from=date_from)
    await audit_repo.record(session, actor.user_id, "orders.export", "orders", None, {"rows": count})

    filename = f"orders-{kind}-{now.strftime('%Y%m%d-%H%M')}.xlsx"
    await call.message.answer_document(
        BufferedInputFile(content, filename=filename),
        caption=f"📤 Заказов в файле: {count}",
    )


# --- пользователи -----------------------------------------------------------


@router.callback_query(F.data == "a:users")
async def users_menu(call: CallbackQuery, actor: Actor, **_: object) -> None:
    if not await guard(call, actor, "users", "list"):
        return
    await call.answer()
    await show(call, "👥 <b>Пользователи</b>", admin_kb.users_menu())


@router.callback_query(F.data == "a:user_search")
async def ask_user(call: CallbackQuery, actor: Actor, state: FSMContext, **_: object) -> None:
    if not await guard(call, actor, "users", "list"):
        return
    await call.answer()
    await state.set_state(UserAdminSG.search)
    await show(
        call,
        "🔎 Отправьте Telegram ID или @username:",
        admin_kb.confirm("noop", "a:users", yes_text="…"),
    )


@router.message(UserAdminSG.search)
async def do_user_search(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor, "users", "list"):
        await state.clear()
        return
    await state.set_state(None)
    found = await users_repo.search(session, message.text or "")
    if not found:
        await message.answer("Никого не нашлось.")
        return
    if len(found) == 1:
        await _render_user(message, session, found[0].tg_id)
        return
    lines = ["Найдено несколько — уточните запрос:", ""]
    for user in found[:10]:
        lines.append(f"• <code>{user.tg_id}</code> — {html.escape(user.display)}")
    await message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("a:user:"))
async def user_card(call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object) -> None:
    if not await guard(call, actor, "users", "view"):
        return
    await call.answer()
    await _render_user(call, session, int(call.data.split(":")[2]))


async def _render_user(event, session: AsyncSession, user_id: int) -> None:
    user = await users_repo.get_user(session, user_id)
    if user is None:
        await show(event, "Пользователь не найден.", admin_kb.confirm("noop", "a:users", yes_text="…"))
        return
    summary = await users_repo.user_summary(session, user_id)
    ledger = await balance_repo.ledger_balance(session, user_id)

    lines = [
        f"👤 <b>{html.escape(user.display)}</b>",
        f"🆔 <code>{user.tg_id}</code>",
        f"📅 Первый запуск: {user.created_at.strftime('%d.%m.%Y %H:%M')}",
        f"👀 Последняя активность: {user.last_seen_at.strftime('%d.%m.%Y %H:%M')}",
        f"💼 Баланс: {format_kop(user.balance_kop)}",
        f"🛒 Покупок: {summary['orders']} на {format_kop(summary['spent_kop'])}",
    ]
    if ledger != user.balance_kop:
        lines.append(f"⚠️ Леджер расходится: {format_kop(ledger)}")
    if user.is_blocked:
        lines.append(f"🚫 Заблокирован: {html.escape(user.block_reason or '—')}")
    if user.has_blocked_bot:
        lines.append("🔕 Заблокировал бота")

    await show(event, "\n".join(lines), admin_kb.user_card(user.tg_id, user.is_blocked))


@router.callback_query(F.data.startswith("a:user_block:"))
async def toggle_block(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor, "users", "act"):
        return
    user_id = int(call.data.split(":")[2])
    user = await users_repo.get_user(session, user_id)
    if user is None:
        await call.answer("Пользователь не найден", show_alert=True)
        return
    new_state = not user.is_blocked
    await users_repo.set_blocked(session, user_id, new_state, "Решение администратора")
    await audit_repo.record(
        session, actor.user_id, "user.block" if new_state else "user.unblock", "user", user_id
    )
    await call.answer("🚫 Заблокирован" if new_state else "✅ Разблокирован")
    await _render_user(call, session, user_id)


@router.callback_query(F.data.startswith("a:user_orders:"))
async def user_orders(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor, "orders", "list"):
        return
    await call.answer()
    user_id = int(call.data.split(":")[2])
    items = await orders_repo.list_for_user(session, user_id, limit=10)
    if not items:
        await show(call, "Заказов нет.", admin_kb.confirm("noop", f"a:user:{user_id}", yes_text="…"))
        return
    await show(call, f"🧾 Заказы <code>{user_id}</code>", admin_kb.orders(items, 0, 1, None))


@router.callback_query(F.data.startswith("a:user_balance:"))
async def ask_balance(
    call: CallbackQuery, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(call, actor, "balance", "act"):
        return
    await call.answer()
    user_id = int(call.data.split(":")[2])
    await state.update_data(target_user=user_id)
    await state.set_state(UserAdminSG.balance_amount)
    await show(
        call,
        "💼 Отправьте сумму со знаком: <code>+500</code> — начислить, "
        "<code>-500</code> — списать.",
        admin_kb.confirm("noop", f"a:user:{user_id}", yes_text="…"),
    )


@router.message(UserAdminSG.balance_amount)
async def change_balance(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor, "balance", "act"):
        await state.clear()
        return
    raw = (message.text or "").strip()
    sign = -1 if raw.startswith("-") else 1
    try:
        amount = parse_price_to_kop(raw.lstrip("+-")) * sign
    except PriceParseError as exc:
        await message.answer(f"Не понял сумму: {exc}")
        return
    if amount == 0:
        await message.answer("Ноль ничего не меняет.")
        return

    data = await state.get_data()
    user_id = int(data["target_user"])
    try:
        txn = await balance_repo.move(
            session,
            user_id=user_id,
            amount_kop=amount,
            kind=BalanceTxnKind.MANUAL,
            admin_id=actor.user_id,
            comment="Правка администратором",
        )
    except balance_repo.InsufficientFunds as exc:
        await message.answer(f"❌ Нельзя: {exc}")
        return

    await audit_repo.record(
        session, actor.user_id, "balance.manual", "user", user_id, {"amount_kop": amount}
    )
    await state.clear()
    await message.answer(
        f"✅ Баланс <code>{user_id}</code> изменён на {format_kop(amount, with_sign=True)}.\n"
        f"Стало: {format_kop(txn.balance_after_kop)}"
    )


@router.callback_query(F.data == "a:balance_audit")
async def balance_audit(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor, "balance", "view"):
        return
    await call.answer("Сверяю…")
    mismatches = await balance_repo.find_mismatches(session)
    if not mismatches:
        text = "🧮 Балансы сошлись: кеш совпадает с леджером у всех пользователей."
    else:
        lines = ["⚠️ <b>Расхождения баланса</b>", ""]
        for tg_id, cached, ledger in mismatches[:20]:
            lines.append(
                f"<code>{tg_id}</code>: кеш {format_kop(cached)}, леджер {format_kop(ledger)}"
            )
        lines.append("")
        lines.append("Леджер — источник правды. Поправьте вручную кнопкой «Изменить баланс».")
        text = "\n".join(lines)
    await show(call, text, admin_kb.confirm("a:balance_audit", "a:users", yes_text="🔄 Ещё раз"))


# --- администраторы ---------------------------------------------------------


@router.callback_query(F.data == "a:admins")
async def list_admins(
    call: CallbackQuery, session: AsyncSession, actor: Actor, settings: Settings, **_: object
) -> None:
    if not await guard(call, actor, "admins", "list"):
        return
    await call.answer()
    items = await users_repo.list_admins(session)
    text = (
        "👑 <b>Администраторы</b>\n\n"
        "Владелец задаётся переменной <code>OWNER_IDS</code> и восстанавливается при "
        "каждом запуске — удалить его из панели нельзя. Это защита от ситуации, когда "
        "магазин остаётся без единого доступа."
    )
    await show(call, text, admin_kb.admins(items, settings.owner_ids))


@router.callback_query(F.data == "a:admin_add")
async def ask_admin(call: CallbackQuery, actor: Actor, state: FSMContext, **_: object) -> None:
    if not await guard(call, actor, "admins", "create"):
        return
    await call.answer()
    await state.set_state(AdminSG.add_id)
    await show(
        call,
        "Отправьте Telegram ID нового администратора.\n\n"
        "Свой ID человек может узнать командой /id в этом боте.",
        admin_kb.confirm("noop", "a:admins", yes_text="…"),
    )


@router.message(AdminSG.add_id)
async def add_admin(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor, "admins", "create"):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw.lstrip("-").isdigit():
        await message.answer("Нужен числовой Telegram ID.")
        return

    user_id = int(raw)
    if await users_repo.get_user(session, user_id) is None:
        await message.answer(
            "Этот человек ещё не запускал бота. Пусть отправит /start, потом добавим."
        )
        return

    await users_repo.add_admin(session, user_id, AdminRole.ADMIN, actor.user_id)
    await audit_repo.record(session, actor.user_id, "admin.add", "user", user_id)
    await state.clear()
    await message.answer(
        f"✅ <code>{user_id}</code> назначен администратором.\n"
        "Доступ: товары, категории, склад, заказы, рассылки, статистика, пользователи."
    )


@router.callback_query(F.data.startswith("a:admin_del:"))
async def remove_admin(
    call: CallbackQuery, session: AsyncSession, actor: Actor, settings: Settings, **_: object
) -> None:
    if not await guard(call, actor, "admins", "act"):
        return
    user_id = int(call.data.split(":")[2])
    if user_id in settings.owner_ids:
        await call.answer("Владельца из OWNER_IDS удалить нельзя", show_alert=True)
        return
    if user_id == actor.user_id:
        await call.answer("Себя снять нельзя — попросите другого владельца", show_alert=True)
        return

    await users_repo.remove_admin(session, user_id)
    await audit_repo.record(session, actor.user_id, "admin.remove", "user", user_id)
    await call.answer("Администратор снят")
    await list_admins(call, session, actor, settings)
