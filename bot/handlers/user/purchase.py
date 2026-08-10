"""Покупка: создание заказа, выбор оплаты, проверка, выдача."""

from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import OrderStatus, User
from bot.keyboards import user as user_kb
from bot.payments.base import ProviderError
from bot.payments.registry import PaymentRegistry
from bot.repo import catalog as catalog_repo
from bot.repo import orders as orders_repo
from bot.repo import users as users_repo
from bot.services import delivery as delivery_service
from bot.services import header as header_service
from bot.services import notify as notify_service
from bot.services import orders as orders_service
from bot.services import payments as payments_service
from bot.services import dispatch as dispatch_service
from bot.services import promo as promo_service
from bot.services.settings_store import settings_store
from bot.services.texts import text_service
from bot.utils.money import format_kop
from bot.utils.render import show

log = logging.getLogger(__name__)

router = Router(name="user.purchase")


@router.callback_query(F.data.startswith("u:buy:"))
async def create_order(
    call: CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    registry: PaymentRegistry,
    state: FSMContext,
    **_: object,
) -> None:
    _, _, product_id_raw, qty_raw = call.data.split(":", 3)
    product_id, qty = int(product_id_raw), int(qty_raw)

    product = await catalog_repo.get_product(session, product_id)
    if product is None or not product.is_active:
        await call.answer("Товар недоступен", show_alert=True)
        return

    data = await state.get_data()
    promo = None
    promo_code = data.get("promo_code")
    if promo_code:
        subtotal = product.price_kop * qty
        check = await promo_service.validate(
            session, promo_code, user.tg_id, product=product, subtotal_kop=subtotal
        )
        promo = check.promo if check.ok else None

    try:
        order = await orders_service.create_order(
            session,
            user_id=user.tg_id,
            product=product,
            qty=qty,
            promo=promo,
            reserve_minutes=settings.order_reserve_minutes,
            max_qty=settings.max_qty_per_order,
        )
    except orders_service.OutOfStock as exc:
        await call.answer()
        await show(
            call,
            await text_service.get(session, "stock_shortage", available=exc.available),
            user_kb.simple_back(f"u:cat:{product.category_id}:0"),
        )
        return
    except orders_service.OrderError as exc:
        await call.answer(str(exc), show_alert=True)
        return

    await call.answer()
    await _show_payment_choice(call, session, user, order, registry)


async def _show_payment_choice(
    call: CallbackQuery,
    session: AsyncSession,
    user: User,
    order,
    registry: PaymentRegistry,
) -> None:
    discount_line = ""
    if order.discount_kop:
        discount_line = await text_service.get(
            session,
            "order_discount_line",
            discount=format_kop(order.discount_kop),
            promo=html.escape(order.promo_code or "скидка"),
        )

    text = await text_service.get(
        session,
        "order_summary",
        order_id=order.id,
        title=html.escape(order.product_title),
        qty=order.qty,
        subtotal=format_kop(order.subtotal_kop),
        discount=discount_line,
        total=format_kop(order.total_kop),
        promo=html.escape(order.promo_code or ""),
    )

    methods = registry.methods()
    balance_enabled = await settings_store.get_bool(session, "balance_enabled", True)
    balance = user.balance_kop if balance_enabled else None

    if not methods and balance is None:
        await show(
            call,
            text + "\n\n⚠️ Способы оплаты пока не настроены. Напишите в поддержку.",
            user_kb.simple_back(),
        )
        return

    await show(
        call,
        text,
        user_kb.payment_methods(order, methods, balance),
        await header_service.photo(session),
    )


@router.callback_query(F.data.startswith("u:pay:"))
async def choose_method(
    call: CallbackQuery,
    session: AsyncSession,
    user: User,
    registry: PaymentRegistry,
    settings: Settings,
    bot: Bot,
    **_: object,
) -> None:
    parts = call.data.split(":", 3)
    order_id, method_code = int(parts[2]), parts[3]

    order = await orders_repo.get(session, order_id)
    if order is None or order.user_id != user.tg_id:
        await call.answer("Заказ не найден", show_alert=True)
        return
    if not orders_service.is_payable(order):
        await call.answer("Заказ уже закрыт", show_alert=True)
        return

    if method_code == "balance":
        await call.answer()
        result = await payments_service.pay_with_balance(session, order.id)
        await _present_result(call, session, bot, result, settings)
        return

    try:
        await payments_service.start_payment(
            session, registry, order, method_code, user.username
        )
    except ProviderError as exc:
        log.error("Не удалось выставить счёт: %s", exc, extra={"order_id": order.id})
        await call.answer(
            "Платёжная система не отвечает. Попробуйте другой способ или чуть позже.",
            show_alert=True,
        )
        return

    await call.answer()
    text = await text_service.get(
        session,
        "payment_created",
        order_id=order.id,
        total=format_kop(order.total_kop),
        minutes=settings.order_reserve_minutes,
    )
    await show(call, text, user_kb.payment_link(order), await header_service.photo(session))


@router.callback_query(F.data.startswith("u:check:"))
async def check_payment(
    call: CallbackQuery,
    session: AsyncSession,
    user: User,
    registry: PaymentRegistry,
    settings: Settings,
    bot: Bot,
    **_: object,
) -> None:
    order_id = int(call.data.split(":")[2])
    order = await orders_repo.get(session, order_id)
    if order is None or order.user_id != user.tg_id:
        await call.answer("Заказ не найден", show_alert=True)
        return

    await call.answer("Проверяем оплату…")
    result = await payments_service.confirm_order(session, registry, order_id, source="button")
    await _present_result(call, session, bot, result, settings)


@router.callback_query(F.data.startswith("u:cancel:"))
async def cancel_order(
    call: CallbackQuery, session: AsyncSession, user: User, **_: object
) -> None:
    order_id = int(call.data.split(":")[2])
    order = await orders_repo.get_for_update(session, order_id)
    if order is None or order.user_id != user.tg_id:
        await call.answer("Заказ не найден", show_alert=True)
        return

    if order.status in (OrderStatus.DELIVERED, OrderStatus.PAID):
        await call.answer("Заказ уже оплачен — отменить нельзя", show_alert=True)
        return

    await orders_service.cancel(session, order)
    await call.answer("Заказ отменён, товар вернулся в продажу")
    await show(
        call,
        f"🚫 Заказ #{order.id} отменён. Товар вернулся в продажу.",
        user_kb.simple_back(),
    )


async def _present_result(
    call: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    result: payments_service.ConfirmResult,
    settings: Settings,
) -> None:
    """Показывает покупателю исход проверки оплаты."""
    outcome = result.outcome
    order = result.order

    if outcome in (payments_service.Outcome.DELIVERED, payments_service.Outcome.ALREADY):
        items = result.items or await delivery_service.items_of(session, order.id)
        text = await text_service.get(
            session,
            "delivery",
            order_id=order.id,
            title=html.escape(order.product_title),
            qty=order.qty,
            items=delivery_service.format_items(items),
        )
        await show(call, text, user_kb.simple_back())
        # Файлы уходят отдельными сообщениями: в подпись к экрану их не вложить.
        await dispatch_service.send_items(bot, order.user_id, items)
        if outcome == payments_service.Outcome.DELIVERED:
            await _after_sale(bot, session, order)
        return

    if outcome == payments_service.Outcome.AWAITING:
        support = await settings_store.get(session, "support_contact") or "@support"
        text = await text_service.get(
            session,
            "delivery_manual",
            order_id=order.id,
            title=html.escape(order.product_title),
            qty=order.qty,
            support=support,
        )
        await show(call, text, user_kb.simple_back())
        await _notify_manual(bot, session, order)
        return

    if outcome == payments_service.Outcome.TOPPED_UP:
        await show(
            call,
            f"✅ Баланс пополнен на {format_kop(order.total_kop)}.",
            user_kb.simple_back(),
        )
        return

    if outcome == payments_service.Outcome.SHORTAGE:
        await show(call, await text_service.get(session, "delivery_shortage"), user_kb.simple_back())
        await notify_service.notify_admins(
            bot,
            session,
            f"⚠️ Заказ #{order.id} оплачен, но выдать нечего.\n"
            f"Покупатель: <code>{order.user_id}</code>\n"
            f"Товар: {html.escape(order.product_title)} × {order.qty}\n"
            f"Сумма: {format_kop(order.total_kop)}\n\n"
            f"Нужно пополнить склад и выдать вручную либо вернуть деньги.",
        )
        return

    if outcome == payments_service.Outcome.MISMATCH:
        await show(
            call,
            "⚠️ Платёж не сошёлся с заказом. Администратор уже уведомлён — "
            "напишите в поддержку, вопрос решим.",
            user_kb.simple_back(),
        )
        await notify_service.notify_admins(
            bot,
            session,
            f"🚨 Платёж не сошёлся с заказом #{order.id}: {result.detail}\n"
            f"Покупатель: <code>{order.user_id}</code>. Выдача остановлена.",
        )
        return

    if outcome == payments_service.Outcome.FAILED:
        text = result.detail or await text_service.get(session, "payment_canceled")
        await show(call, text, user_kb.simple_back())
        return

    if outcome == payments_service.Outcome.CLOSED:
        status = OrderStatus.TITLES.get(order.status, order.status) if order else ""
        await show(call, f"Заказ уже закрыт: {status}", user_kb.simple_back())
        return

    if outcome == payments_service.Outcome.UNAVAILABLE:
        await call.answer(
            "Платёжная система не отвечает. Повторите проверку через минуту.", show_alert=True
        )
        return

    if outcome == payments_service.Outcome.NOT_FOUND:
        await call.answer("Заказ не найден", show_alert=True)
        return

    await call.answer(await text_service.get(session, "payment_not_confirmed"), show_alert=True)


async def _notify_manual(bot: Bot, session: AsyncSession, order) -> None:
    """Зовёт администраторов на ручную выдачу.

    Уведомление отправляется один раз — при переводе заказа в ожидание.
    Повторная проверка оплаты в этот путь уже не заходит.
    """
    if order.status != OrderStatus.AWAITING:
        return
    buyer = await users_repo.get_user(session, order.user_id)
    contact = f"@{buyer.username}" if buyer and buyer.username else "без username"
    await notify_service.notify_admins(
        bot,
        session,
        f"🙋 <b>Заказ #{order.id} ждёт ручной выдачи</b>\n\n"
        f"📦 {html.escape(order.product_title)} × {order.qty}\n"
        f"💰 {format_kop(order.total_kop)}\n"
        f"👤 Покупатель: {html.escape(contact)} "
        f"(<code>{order.user_id}</code>)\n\n"
        f"Свяжитесь с покупателем и закройте заказ кнопкой «Выдать вручную» "
        f"в карточке заказа.",
    )


async def _after_sale(bot: Bot, session: AsyncSession, order) -> None:
    """Уведомления после успешной продажи."""
    if await settings_store.get_bool(session, "notify_admins_on_payment", True):
        await notify_service.notify_admins(
            bot,
            session,
            f"💰 Оплачен заказ #{order.id}\n"
            f"Товар: {html.escape(order.product_title)} × {order.qty}\n"
            f"Сумма: {format_kop(order.total_kop)}\n"
            f"Покупатель: <code>{order.user_id}</code>",
        )

    threshold = await settings_store.get_int(session, "low_stock_threshold", 3)
    if order.product_id is None or threshold <= 0:
        return
    from bot.repo import stock as stock_repo

    left = await stock_repo.available_count(session, order.product_id)
    if left <= threshold:
        await notify_service.notify_admins(
            bot,
            session,
            f"📉 Мало на складе: {html.escape(order.product_title)} — осталось {left} шт.",
        )
