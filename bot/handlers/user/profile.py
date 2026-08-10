"""Профиль: баланс, покупки, промокод, пополнение."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import BalanceTxnKind, OrderKind, OrderStatus, User
from bot.keyboards import user as user_kb
from bot.payments.registry import PaymentRegistry
from bot.repo import balance as balance_repo
from bot.repo import orders as orders_repo
from bot.repo import users as users_repo
from bot.services import delivery as delivery_service
from bot.services import header as header_service
from bot.services import promo as promo_service
from bot.services.settings_store import settings_store
from bot.services.texts import text_service
from bot.states.user import UserSG
from bot.utils.money import PriceParseError, format_kop, parse_price_to_kop
from bot.utils.pagination import paginate
from bot.utils.render import show

router = Router(name="user.profile")

PER_PAGE = 8


@router.callback_query(F.data == "u:profile")
async def open_profile(
    call: CallbackQuery, session: AsyncSession, user: User, state: FSMContext, **_: object
) -> None:
    await call.answer()
    await _render_profile(call, session, user, state)


async def _render_profile(
    event: CallbackQuery | Message, session: AsyncSession, user: User, state: FSMContext
) -> None:
    summary = await users_repo.user_summary(session, user.tg_id)
    data = await state.get_data()
    promo_code = data.get("promo_code")

    text = await text_service.get(
        session,
        "profile",
        name=html.escape(user.display),
        user_id=user.tg_id,
        balance=format_kop(user.balance_kop),
        orders=summary["orders"],
        spent=format_kop(summary["spent_kop"]),
        since=user.created_at.strftime("%d.%m.%Y"),
    )
    if promo_code:
        text += f"\n🎟 <b>Активный промокод:</b> {html.escape(promo_code)}"

    topup_enabled = await settings_store.get_bool(session, "topup_enabled", True)
    await show(
        event,
        text,
        user_kb.profile(has_promo=bool(promo_code), topup_enabled=topup_enabled),
        await header_service.photo(session),
    )


# --- покупки ----------------------------------------------------------------


@router.callback_query(F.data.startswith("u:purchases:"))
async def purchases(call: CallbackQuery, session: AsyncSession, user: User, **_: object) -> None:
    await call.answer()
    page = int(call.data.split(":")[2])
    total = await orders_repo.count_paid_for_user(session, user.tg_id)
    if total == 0:
        await show(
            call,
            await text_service.get(session, "purchases_empty"),
            user_kb.simple_back("u:profile"),
        )
        return

    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    items = await orders_repo.list_paid_for_user(
        session, user.tg_id, limit=PER_PAGE, offset=page * PER_PAGE
    )
    await show(
        call,
        f"🧾 <b>Мои покупки</b> — всего {total}",
        user_kb.purchases(items, page, pages),
    )


@router.callback_query(F.data.startswith("u:purchase:"))
async def purchase_card(
    call: CallbackQuery, session: AsyncSession, user: User, **_: object
) -> None:
    await call.answer()
    order_id = int(call.data.split(":")[2])
    order = await orders_repo.get(session, order_id)
    if order is None or order.user_id != user.tg_id:
        await call.answer("Заказ не найден", show_alert=True)
        return

    lines = [
        f"🧾 <b>Заказ #{order.id}</b>",
        f"📦 {html.escape(order.product_title)} — {order.qty} шт.",
        f"📅 {order.created_at.strftime('%d.%m.%Y %H:%M')}",
        f"💰 Итого: {format_kop(order.total_kop)}",
    ]
    if order.discount_kop:
        lines.append(
            f"🎟 Скидка ({html.escape(order.promo_code or '')}): −{format_kop(order.discount_kop)}"
        )
    lines.append(f"📌 {OrderStatus.TITLES.get(order.status, order.status)}")

    if order.status == OrderStatus.DELIVERED and order.kind == OrderKind.PURCHASE:
        items = await delivery_service.items_of(session, order.id)
        lines += ["", "━━━━━━━━━━━━━━━━━━", delivery_service.format_items(items)]

    await show(call, "\n".join(lines), user_kb.simple_back("u:purchases:0"))


# --- промокод ---------------------------------------------------------------


@router.callback_query(F.data == "u:promo")
async def ask_promo(
    call: CallbackQuery, session: AsyncSession, state: FSMContext, **_: object
) -> None:
    await call.answer()
    await state.set_state(UserSG.promo)
    await show(
        call, await text_service.get(session, "promo_prompt"), user_kb.simple_back("u:profile")
    )


@router.message(UserSG.promo)
async def apply_promo(
    message: Message, session: AsyncSession, user: User, state: FSMContext, **_: object
) -> None:
    code = (message.text or "").strip()
    await state.set_state(None)

    check = await promo_service.validate(session, code, user.tg_id)
    if not check.ok:
        text = await text_service.get(
            session,
            check.reason or "promo_invalid",
            min_order=format_kop(check.min_order_kop),
        )
        await message.answer(text)
        await _render_profile(message, session, user, state)
        return

    await state.update_data(promo_code=check.promo.code)
    await message.answer(
        await text_service.get(
            session,
            "promo_applied",
            code=html.escape(check.promo.code),
            discount=promo_service.describe_discount(check.promo),
        )
    )
    await _render_profile(message, session, user, state)


@router.callback_query(F.data == "u:promo_clear")
async def clear_promo(
    call: CallbackQuery, session: AsyncSession, user: User, state: FSMContext, **_: object
) -> None:
    await state.update_data(promo_code=None)
    await call.answer(await text_service.get(session, "promo_cleared"))
    await _render_profile(call, session, user, state)


# --- баланс -----------------------------------------------------------------


@router.callback_query(F.data.startswith("u:balance:"))
async def balance_history(
    call: CallbackQuery, session: AsyncSession, user: User, **_: object
) -> None:
    await call.answer()
    page = int(call.data.split(":")[2])
    items = await balance_repo.history(session, user.tg_id, limit=PER_PAGE, offset=page * PER_PAGE)

    lines = [f"💼 <b>Баланс:</b> {format_kop(user.balance_kop)}", ""]
    if not items:
        lines.append("Движений пока не было.")
    else:
        for txn in items:
            sign = "+" if txn.amount_kop > 0 else "−"
            lines.append(
                f"{txn.created_at.strftime('%d.%m %H:%M')} · {sign}{format_kop(abs(txn.amount_kop))} "
                f"· {BalanceTxnKind.TITLES.get(txn.kind, txn.kind)}"
            )
    await show(call, "\n".join(lines), user_kb.simple_back("u:profile"))


@router.callback_query(F.data == "u:topup")
async def ask_topup(
    call: CallbackQuery, session: AsyncSession, state: FSMContext, **_: object
) -> None:
    if not await settings_store.get_bool(session, "topup_enabled", True):
        await call.answer("Пополнение сейчас выключено", show_alert=True)
        return
    await call.answer()
    minimum = await settings_store.get_int(session, "min_topup_kop", 5000)
    await state.set_state(UserSG.topup)
    await show(
        call,
        await text_service.get(session, "topup_prompt", min_amount=format_kop(minimum)),
        user_kb.simple_back("u:profile"),
    )


@router.message(UserSG.topup)
async def create_topup(
    message: Message,
    session: AsyncSession,
    user: User,
    state: FSMContext,
    registry: PaymentRegistry,
    settings: Settings,
    **_: object,
) -> None:
    await state.set_state(None)
    try:
        amount_kop = parse_price_to_kop(message.text or "")
    except PriceParseError as exc:
        await message.answer(f"Не понял сумму: {exc}")
        return

    minimum = await settings_store.get_int(session, "min_topup_kop", 5000)
    if amount_kop < minimum:
        await message.answer(f"Минимальная сумма пополнения — {format_kop(minimum)}.")
        return

    methods = registry.methods()
    if not methods:
        await message.answer("Способы оплаты пока не настроены. Напишите в поддержку.")
        return

    from datetime import timedelta

    from bot.db.base import utcnow
    from bot.db.models import Order

    order = Order(
        user_id=user.tg_id,
        kind=OrderKind.TOPUP,
        product_id=None,
        product_title="Пополнение баланса",
        qty=1,
        unit_price_kop=amount_kop,
        subtotal_kop=amount_kop,
        discount_kop=0,
        total_kop=amount_kop,
        status=OrderStatus.NEW,
        reserve_expires_at=utcnow() + timedelta(minutes=settings.order_reserve_minutes),
    )
    session.add(order)
    await session.flush()

    await message.answer(
        f"💼 Пополнение на {format_kop(amount_kop)}, заказ #{order.id}.\nВыберите способ оплаты:",
        reply_markup=user_kb.payment_methods(order, methods, balance_kop=None),
    )
