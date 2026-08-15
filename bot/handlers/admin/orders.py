"""Админка: заказы, возвраты, замены."""

from __future__ import annotations

import html

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import DeliveryType, OrderStatus
from bot.handlers.admin.common import guard
from bot.keyboards import admin as admin_kb
from bot.repo import audit as audit_repo
from bot.repo import orders as orders_repo
from bot.repo import users as users_repo
from bot.services import delivery as delivery_service
from bot.services import dispatch as dispatch_service
from bot.services import refunds as refunds_service
from bot.services.access import Actor
from bot.services.texts import text_service
from bot.states.admin import OrderSG
from bot.utils.money import format_kop
from bot.utils.render import show

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
        f"📦 Товар: {html.escape(order.product_title)} × {order.qty}",
        f"💵 До скидки: {format_kop(order.subtotal_kop)}",
    ]
    if order.discount_kop:
        lines.append(
            f"🎟 Скидка ({html.escape(order.promo_code or '')}): −{format_kop(order.discount_kop)}"
        )
    lines += [
        f"💰 Итого: <b>{format_kop(order.total_kop)}</b>",
        f"🚚 Выдача: {DeliveryType.TITLES.get(order.delivery_type, order.delivery_type)}",
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
    can_replace = (
        order.status == OrderStatus.DELIVERED
        and order.product_id is not None
        and order.delivery_type in DeliveryType.NEEDS_STOCK
    )
    needs_manual = order.status == OrderStatus.AWAITING

    if needs_manual:
        lines.append("")
        lines.append("🙋 <b>Заказ ждёт ручной выдачи.</b> Нажмите «Выдать вручную».")

    await show(
        event,
        "\n".join(lines),
        admin_kb.order_card(order, can_refund, can_replace, needs_manual),
    )


@router.callback_query(F.data.startswith("a:order_items:"))
async def order_items(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    order_id = int(call.data.split(":")[2])
    items = await delivery_service.items_of(session, order_id)
    text = (
        f"📄 <b>Что выдано по заказу #{order_id}</b>\n\n"
        + delivery_service.format_items(items)
    )
    await show(call, text, admin_kb.confirm("noop", f"a:order:{order_id}", yes_text="…"))


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

    result = await refunds_service.refund_to_balance(session, order_id, actor.user_id, comment)
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


@router.callback_query(F.data.startswith("a:order_replace:"))
async def ask_replace(call: CallbackQuery, actor: Actor, state: FSMContext, **_: object) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    order_id = int(call.data.split(":")[2])
    await state.set_state(OrderSG.replace_reason)
    await state.update_data(order_id=order_id)
    await show(
        call,
        f"🔁 <b>Замена по заказу #{order_id}</b>\n\n"
        "Покупателю выдадут другие позиции со склада, а старые уйдут в брак.\n\n"
        "Напишите причину замены:",
        admin_kb.confirm("noop", f"a:order:{order_id}", yes_text="…"),
    )


@router.message(OrderSG.replace_reason)
async def do_replace(
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
    reason = (message.text or "").strip() or "без причины"

    result = await refunds_service.replace_items(session, order_id, actor.user_id, reason)
    await state.clear()
    if not result.ok:
        await message.answer(f"❌ Замена не сделана: {result.detail}")
        return

    await audit_repo.record(
        session, actor.user_id, "order.replace", "order", order_id, {"reason": reason}
    )
    await message.answer(f"✅ По заказу #{order_id} выдана замена.")

    order = await orders_repo.get(session, order_id)
    if order is not None:
        await _tell_buyer(
            bot,
            order.user_id,
            f"🔁 <b>Замена по заказу #{order_id}</b>\n\n"
            f"{delivery_service.format_contents(result.contents or [])}",
        )


# --- блокировка -------------------------------------------------------------


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


@router.callback_query(F.data.startswith("a:order_manual:"))
async def ask_manual(
    call: CallbackQuery, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    order_id = int(call.data.split(":")[2])
    order = await orders_repo.get(session, order_id)
    if order is None:
        await call.answer("Заказ не найден", show_alert=True)
        return
    if order.status != OrderStatus.AWAITING:
        await call.answer("Этот заказ не ждёт ручной выдачи", show_alert=True)
        return

    buyer = await users_repo.get_user(session, order.user_id)
    contact = f"@{buyer.username}" if buyer and buyer.username else "без username"

    await state.set_state(OrderSG.manual_payload)
    await state.update_data(order_id=order_id)
    await show(
        call,
        f"🙋 <b>Ручная выдача по заказу #{order_id}</b>\n\n"
        f"📦 {html.escape(order.product_title)} × {order.qty}\n"
        f"👤 {html.escape(contact)} (<code>{order.user_id}</code>)\n\n"
        "Пришлите то, что нужно отправить покупателю: текст, файл, картинку "
        "или видео. Бот перешлёт это ему и закроет заказ.\n\n"
        "Отправленное сохранится в истории заказа — потом будет видно, что "
        "именно выдали.",
        admin_kb.confirm("noop", f"a:order:{order_id}", yes_text="…"),
    )


@router.message(OrderSG.manual_payload)
async def do_manual(
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
    order = await orders_repo.get_for_update(session, order_id)
    if order is None:
        await message.answer("Заказ не найден.")
        await state.clear()
        return

    item = dispatch_service.extract_file(message)
    if item is None:
        text = (message.html_text or "").strip() if message.text else ""
        if not text:
            await message.answer("Пришлите текст или файл — пустое отправлять нечего.")
            return
        item = delivery_service.DeliveredItem(content=text)

    result = await delivery_service.complete_manual(session, order, actor.user_id, item)
    if not result.ok:
        await message.answer("Не вышло закрыть заказ — проверьте его статус.")
        await state.clear()
        return
    if result.already_delivered:
        await message.answer("Заказ уже был выдан раньше — ничего не отправлял.")
        await state.clear()
        return

    body = await text_service.get(
        session,
        "delivery_manual_done",
        order_id=order.id,
        title=html.escape(order.product_title),
        items=delivery_service.format_items(result.items),
    )
    delivered = await _tell_buyer(bot, order.user_id, body)
    await dispatch_service.send_items(bot, order.user_id, result.items)

    await audit_repo.record(
        session, actor.user_id, "order.manual_delivery", "order", order_id,
        {"is_file": item.is_file},
    )
    await state.clear()

    note = "" if delivered else "\n\n⚠️ Покупателю доставить не удалось — возможно, он заблокировал бота."
    await message.answer(f"✅ Заказ #{order_id} выдан и закрыт.{note}")
