"""Профиль: покупки и промокод."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import OrderStatus, User
from bot.keyboards import user as user_kb
from bot.repo import orders as orders_repo
from bot.repo import users as users_repo
from bot.services import delivery as delivery_service
from bot.services import header as header_service
from bot.services import promo as promo_service
from bot.services.texts import text_service
from bot.states.user import UserSG
from bot.utils.money import format_kop
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
        orders=summary["orders"],
        spent=format_kop(summary["spent_kop"]),
        since=user.created_at.strftime("%d.%m.%Y"),
    )
    if promo_code:
        text += f"\n🎟 <b>Активный промокод:</b> {html.escape(promo_code)}"


    await show(
        event,
        text,
        user_kb.profile(has_promo=bool(promo_code)),
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
        f"📦 {html.escape(order.product_title)}",
    ]
    if order.token:
        lines.append(f"🎟 Токен: <code>{order.token}</code>")
    lines += [
        f"📅 {order.created_at.strftime('%d.%m.%Y %H:%M')}",
        f"💰 Итого: {format_kop(order.total_kop)}",
    ]
    if order.discount_kop:
        lines.append(
            f"🎟 Скидка ({html.escape(order.promo_code or '')}): −{format_kop(order.discount_kop)}"
        )
    lines.append(f"📌 {OrderStatus.TITLES.get(order.status, order.status)}")

    # Оплаченный заказ без реквизитов — тупик, если из него некуда нажать.
    # Покупатель мог закрыть чат на полпути, и вернуть его должна кнопка,
    # а не просьба «напишите в поддержку».
    if delivery_service.needs_credentials(order):
        lines += ["", "🔑 Осталось прислать логин и пароль от аккаунта."]
        await show(call, "\n".join(lines), user_kb.send_credentials(order.id))
        return

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
