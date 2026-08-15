"""Админка: заказы, возвраты, замены."""

from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import OrderStatus
from bot.handlers.admin.common import guard
from bot.keyboards import admin as admin_kb
from bot.repo import audit as audit_repo
from bot.repo import orders as orders_repo
from bot.repo import users as users_repo
from bot.services import delivery as delivery_service
from bot.services import notify as notify_service
from bot.services import refunds as refunds_service
from bot.services.access import Actor
from bot.services.texts import text_service
from bot.states.admin import OrderSG
from bot.utils.money import format_kop
from bot.utils.render import show

log = logging.getLogger(__name__)

router = Router(name="admin.orders")

PER_PAGE = 8


@router.callback_query(F.data.startswith("a:orders:"))
async def list_orders(
    call: CallbackQuery, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    page = int(call.data.split(":")[2])
    data = await state.get_data()
    status = data.get("orders_filter")

    total = await orders_repo.count_all(session, status)
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    items = await orders_repo.list_all(session, status, PER_PAGE, page * PER_PAGE)

    title = "📦 <b>Заказы</b>"
    if status:
        title += f" · фильтр: {OrderStatus.TITLES.get(status, status)}"
    text = f"{title} — всего {total}"
    if not items:
        text += "\n\nПока пусто."
    await show(call, text, admin_kb.orders(items, page, pages, status))


@router.callback_query(F.data == "a:orders_filter")
async def toggle_filter(
    call: CallbackQuery, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(call, actor):
        return
    data = await state.get_data()
    current = data.get("orders_filter")
    await state.update_data(orders_filter=None if current else OrderStatus.DELIVERED)
    call.data = "a:orders:0"
    await list_orders(call, session, actor, state)


@router.callback_query(F.data.startswith("a:order:"))
async def order_card(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    order_id = int(call.data.split(":")[2])
    await _render_order(call, session, order_id)


async def _render_order(event, session: AsyncSession, order_id: int) -> None:
    order = await orders_repo.get(session, order_id)
    if order is None:
        await show(event, "Заказ не найден.", admin_kb.confirm("noop", "a:orders:0", yes_text="…"))
        return

    buyer = await users_repo.get_user(session, order.user_id)
    lines = [
        f"🧾 <b>Заказ #{order.id}</b>",
        "",
        f"👤 Покупатель: {html.escape(buyer.display) if buyer else '—'} "
        f"(<code>{order.user_id}</code>)",
        f"📦 Товар: {html.escape(order.product_title)}",
        f"🎟 Токен: <code>{order.token or '—'}</code>",
        f"💵 До скидки: {format_kop(order.subtotal_kop)}",
    ]
    if order.discount_kop:
        lines.append(
            f"🎟 Скидка ({html.escape(order.promo_code or '')}): −{format_kop(order.discount_kop)}"
        )
    lines += [
        f"💰 Итого: <b>{format_kop(order.total_kop)}</b>",
        f"💳 Оплата: {order.payment_method or '—'}",
        f"🔗 Транзакция: <code>{order.provider_txn_id or '—'}</code>",
        f"📌 Статус: {OrderStatus.TITLES.get(order.status, order.status)}",
        f"📅 Создан: {order.created_at.strftime('%d.%m.%Y %H:%M')}",
    ]
    if order.paid_at:
        lines.append(f"✅ Оплачен: {order.paid_at.strftime('%d.%m.%Y %H:%M')}")
    if order.refunded_at:
        lines.append(f"↩️ Возврат: {order.refunded_at.strftime('%d.%m.%Y %H:%M')}")
    if order.admin_note:
        lines.append(f"📝 Пометка: {html.escape(order.admin_note)}")

    can_refund = refunds_service.order_can_be_refunded(order)
    can_confirm = order.status == OrderStatus.IN_WORK

    if order.status == OrderStatus.AWAITING_CREDENTIALS:
        lines += ["", "🔑 <b>Ждём от покупателя логин и пароль.</b>"]
    elif order.status == OrderStatus.IN_WORK:
        lines += [
            "",
            "🔑 <b>Доступ к аккаунту</b>",
            f"Логин: <code>{html.escape(order.account_login or '')}</code>",
            f"Пароль: <code>{html.escape(order.account_password or '')}</code>",
        ]
    if order.delivered_by:
        lines.append(f"🛠 Выполнил: <code>{order.delivered_by}</code>")

    await show(
        event,
        "\n".join(lines),
        admin_kb.order_card(
            order,
            can_refund,
            can_confirm,
            buyer.username if buyer and buyer.username else None,
        ),
    )


@router.callback_query(F.data.startswith("a:order_pays:"))
async def order_payments(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    order_id = int(call.data.split(":")[2])
    payments = await orders_repo.payments_of(session, order_id)
    if not payments:
        text = f"💳 По заказу #{order_id} обращений к провайдеру не было."
    else:
        lines = [f"💳 <b>Платёжный журнал заказа #{order_id}</b>", ""]
        for payment in payments:
            lines.append(
                f"{payment.created_at.strftime('%d.%m %H:%M:%S')} · {payment.provider} · "
                f"{payment.event} → <b>{payment.status}</b> · {format_kop(payment.amount_kop)}"
            )
        text = "\n".join(lines)
    await show(call, text, admin_kb.confirm("noop", f"a:order:{order_id}", yes_text="…"))


# --- поиск ------------------------------------------------------------------


@router.callback_query(F.data == "a:order_search")
async def ask_search(call: CallbackQuery, actor: Actor, state: FSMContext, **_: object) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    await state.set_state(OrderSG.search)
    await show(
        call,
        "🔎 Отправьте номер заказа или Telegram ID покупателя:",
        admin_kb.confirm("noop", "a:orders:0", yes_text="…"),
    )


@router.message(OrderSG.search)
async def do_search(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return
    await state.set_state(None)
    query = (message.text or "").strip()
    found = await orders_repo.search(session, query)
    if not found:
        await message.answer("Ничего не нашлось. Нужен номер заказа или Telegram ID.")
        return
    if len(found) == 1:
        await _render_order(message, session, found[0].id)
        return
    await message.answer(
        f"Найдено заказов: {len(found)}",
        reply_markup=admin_kb.orders(found[:PER_PAGE], 0, 1, None),
    )


# --- возврат ----------------------------------------------------------------


@router.callback_query(F.data.startswith("a:order_refund:"))
async def ask_refund(call: CallbackQuery, actor: Actor, state: FSMContext, **_: object) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    order_id = int(call.data.split(":")[2])
    await state.set_state(OrderSG.refund_comment)
    await state.update_data(order_id=order_id)
    await show(
        call,
        f"↩️ <b>Возврат по заказу #{order_id}</b>\n\n"
        "Деньги вернутся на внутренний баланс покупателя — вернуть их на карту "
        "через Platega нельзя, в её API такого метода нет.\n\n"
        "Напишите причину возврата (попадёт в журнал и в карточку заказа):",
        admin_kb.confirm("noop", f"a:order:{order_id}", yes_text="…"),
    )


@router.message(OrderSG.refund_comment)
async def do_refund(
    message: Message,
    session: AsyncSession,
    actor: Actor,
    state: FSMContext,
    bot: Bot,
    **_: object,
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return
    data = await state.get_data()
    order_id = int(data["order_id"])
    comment = (message.text or "").strip() or "без причины"

    result = await refunds_service.mark_refunded(session, order_id, actor.user_id, comment)
    await state.clear()
    if not result.ok:
        await message.answer(f"❌ Возврат не сделан: {result.detail}")
        return

    await audit_repo.record(
        session, actor.user_id, "order.refund", "order", order_id,
        {"amount_kop": result.amount_kop, "comment": comment},
    )
    await message.answer(
        f"✅ Возврат {format_kop(result.amount_kop)} по заказу #{order_id} зачислен на баланс."
    )

    order = await orders_repo.get(session, order_id)
    if order is not None:
        await _tell_buyer(
            bot,
            order.user_id,
            f"↩️ По заказу #{order_id} сделан возврат {format_kop(result.amount_kop)}.\n"
            f"Деньги на вашем балансе — можно сразу купить заново.\n"
            f"Причина: {html.escape(comment)}",
        )


# --- замена -----------------------------------------------------------------


@router.callback_query(F.data.startswith("a:order_block:"))
async def ask_block(call: CallbackQuery, actor: Actor, state: FSMContext, **_: object) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    order_id = int(call.data.split(":")[2])
    await state.set_state(OrderSG.block_reason)
    await state.update_data(order_id=order_id)
    await show(
        call,
        "🚫 Напишите причину блокировки покупателя:",
        admin_kb.confirm("noop", f"a:order:{order_id}", yes_text="…"),
    )


@router.message(OrderSG.block_reason)
async def do_block(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return
    data = await state.get_data()
    order = await orders_repo.get(session, int(data["order_id"]))
    reason = (message.text or "").strip() or "без причины"
    await state.clear()
    if order is None:
        await message.answer("Заказ не найден.")
        return

    await users_repo.set_blocked(session, order.user_id, True, reason)
    await audit_repo.record(
        session, actor.user_id, "user.block", "user", order.user_id, {"reason": reason}
    )
    await message.answer(f"🚫 Покупатель <code>{order.user_id}</code> заблокирован.")


async def _tell_buyer(bot: Bot, user_id: int, text: str) -> bool:
    """Сообщение покупателю. Недоставка не ломает операцию админа, но видна ему."""
    try:
        await bot.send_message(user_id, text)
        return True
    except TelegramAPIError:
        return False


# --- ручная выдача -----------------------------------------------------------


@router.callback_query(F.data.startswith("a:done:"))
async def confirm_done(
    call: CallbackQuery, session: AsyncSession, actor: Actor, bot: Bot, **_: object
) -> None:
    """Подтверждение выполнения.

    Кнопка живёт в двух местах — в карточке заказа и под уведомлением, которое
    ушло всем администраторам. Значит нажать её могут дважды и разные люди,
    поэтому заказ берётся под блокировку, а повторное нажатие отвечает
    «уже подтверждён» и покупателю ничего не шлёт.
    """
    if not await guard(call, actor):
        return
    order_id = int(call.data.split(":")[2])

    order = await orders_repo.get_for_update(session, order_id)
    if order is None:
        await call.answer("Заказ не найден", show_alert=True)
        return

    result = await delivery_service.confirm_done(session, order, actor.user_id)
    if result.repeated:
        await call.answer("Этот заказ уже подтверждён", show_alert=True)
        return
    if not result.ok:
        await call.answer(
            "Подтвердить можно только заказ в работе — по этому ещё нет реквизитов "
            "или он уже закрыт.",
            show_alert=True,
        )
        return

    await audit_repo.record(session, actor.user_id, "order.done", "order", order_id)
    await call.answer("Готово")

    text = await text_service.get(
        session,
        "order_done",
        order_id=order.id,
        token=order.token or "",
        title=html.escape(order.product_title),
    )
    try:
        await bot.send_message(order.user_id, text)
    except TelegramAPIError as exc:
        log.warning("Не сообщили покупателю о выполнении: %s", exc)
        await notify_service.notify_admins(
            bot,
            session,
            f"⚠️ Заказ #{order.id} выполнен, но покупателю сообщить не удалось "
            f"(<code>{order.user_id}</code>). Возможно, он заблокировал бота.",
        )

    await _render_order(call, session, order_id)
