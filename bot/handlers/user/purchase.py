"""Покупка: заказ, оплата, реквизиты аккаунта.

После подтверждения оплаты бот просит логин и пароль. Просьба отправляется
**только когда оплата принята именно этим вызовом** — иначе повторное нажатие
«Проверить оплату» переспрашивало бы реквизиты у покупателя, который их уже
прислал.
"""

from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import OrderStatus, User
from bot.keyboards import admin as admin_kb
from bot.keyboards import user as user_kb
from bot.payments.base import ProviderError
from bot.payments.registry import PaymentRegistry
from bot.repo import catalog as catalog_repo
from bot.repo import orders as orders_repo
from bot.repo import users as users_repo
from bot.services import currency as currency_service
from bot.services import delivery as delivery_service
from bot.services import fulfillment as fulfillment_service
from bot.services import header as header_service
from bot.services import notify as notify_service
from bot.services import orders as orders_service
from bot.services import payments as payments_service
from bot.services import promo as promo_service
from bot.services.settings_store import settings_store
from bot.services.texts import text_service
from bot.states.user import UserSG
from bot.utils.money import format_kop
from bot.utils.render import show

log = logging.getLogger(__name__)

router = Router(name="user.purchase")


# --- создание заказа --------------------------------------------------------


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
    product_id = int(call.data.split(":")[2])

    product = await catalog_repo.get_product(session, product_id)
    if product is None or not product.is_active:
        await call.answer("Товар недоступен", show_alert=True)
        return

    rate_kop = await currency_service.current_usd_kop(session)
    if not rate_kop:
        await call.answer()
        await show(
            call,
            await text_service.get(session, "rate_unavailable"),
            user_kb.simple_back(f"u:cat:{product.category_id}:0"),
        )
        return

    markup_pct = await settings_store.get_int(session, "price_markup_pct", 10)

    data = await state.get_data()
    promo = None
    promo_code = data.get("promo_code")
    if promo_code:
        preview = orders_service.quote(product, rate_kop, markup_pct)
        check = await promo_service.validate(
            session,
            promo_code,
            user.tg_id,
            product=product,
            subtotal_kop=preview.unit_price_kop,
        )
        promo = check.promo if check.ok else None

    try:
        order = await orders_service.create_order(
            session,
            user_id=user.tg_id,
            product=product,
            promo=promo,
            rate_kop=rate_kop,
            markup_pct=markup_pct,
            reserve_minutes=settings.order_reserve_minutes,
        )
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


# --- оплата -----------------------------------------------------------------


@router.callback_query(F.data.startswith("u:pay:"))
async def choose_method(
    call: CallbackQuery,
    session: AsyncSession,
    user: User,
    registry: PaymentRegistry,
    settings: Settings,
    state: FSMContext,
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
        await _present_result(call, session, bot, state, result)
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
    state: FSMContext,
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
    await _present_result(call, session, bot, state, result)


@router.callback_query(F.data.startswith("u:cancel:"))
async def cancel_order(
    call: CallbackQuery, session: AsyncSession, user: User, **_: object
) -> None:
    order_id = int(call.data.split(":")[2])
    order = await orders_repo.get_for_update(session, order_id)
    if order is None or order.user_id != user.tg_id:
        await call.answer("Заказ не найден", show_alert=True)
        return

    if order.status not in OrderStatus.OPEN:
        await call.answer("Заказ уже оплачен — отменить нельзя", show_alert=True)
        return

    await orders_service.cancel(session, order)
    await call.answer("Заказ отменён")
    await show(call, f"🚫 Заказ #{order.id} отменён.", user_kb.simple_back())


# --- реквизиты аккаунта -----------------------------------------------------


@router.callback_query(F.data.startswith("u:creds:"))
async def resume_credentials(
    call: CallbackQuery,
    session: AsyncSession,
    user: User,
    state: FSMContext,
    **_: object,
) -> None:
    """Возврат к вводу реквизитов из истории покупок."""
    order_id = int(call.data.split(":")[2])
    order = await orders_repo.get(session, order_id)
    if order is None or order.user_id != user.tg_id:
        await call.answer("Заказ не найден", show_alert=True)
        return
    if not delivery_service.needs_credentials(order):
        await call.answer("По этому заказу реквизиты уже приняты", show_alert=True)
        return

    await call.answer()
    await _ask_credentials(call, session, state, order)


async def _ask_credentials(event, session: AsyncSession, state: FSMContext, order) -> None:
    await state.set_state(UserSG.credentials)
    await state.update_data(credentials_order_id=order.id)
    text = await text_service.get(
        session,
        "ask_credentials",
        order_id=order.id,
        title=html.escape(order.product_title),
        token=order.token or "",
    )
    await show(event, text, user_kb.simple_back())


@router.message(UserSG.credentials)
async def receive_credentials(
    message: Message,
    session: AsyncSession,
    user: User,
    state: FSMContext,
    bot: Bot,
    **_: object,
) -> None:
    parsed = fulfillment_service.parse_credentials(message.text or "")
    if parsed is None:
        await message.answer(
            "Не разобрал сообщение. Пришлите логин первой строкой, пароль — второй."
        )
        return

    data = await state.get_data()
    order_id = data.get("credentials_order_id")

    if parsed.password is None:
        # Логин есть, пароля нет — спрашиваем отдельно, а не гадаем.
        await state.set_state(UserSG.credentials_password)
        await state.update_data(credentials_login=parsed.login, credentials_order_id=order_id)
        await message.answer("Принял логин. Теперь пришлите пароль отдельным сообщением.")
        return

    await _finish_credentials(message, session, user, state, bot, order_id, parsed.login, parsed.password)


@router.message(UserSG.credentials_password)
async def receive_password(
    message: Message,
    session: AsyncSession,
    user: User,
    state: FSMContext,
    bot: Bot,
    **_: object,
) -> None:
    password = (message.text or "").strip()
    if not password:
        await message.answer("Пароль пустой. Пришлите его текстом.")
        return

    data = await state.get_data()
    await _finish_credentials(
        message,
        session,
        user,
        state,
        bot,
        data.get("credentials_order_id"),
        data.get("credentials_login") or "",
        password,
    )


async def _finish_credentials(
    message: Message,
    session: AsyncSession,
    user: User,
    state: FSMContext,
    bot: Bot,
    order_id: int | None,
    login: str,
    password: str,
) -> None:
    if not order_id:
        await state.set_state(None)
        await message.answer(
            "Не понял, к какому заказу это относится. Откройте заказ в профиле "
            "и нажмите «Отправить логин и пароль».",
            reply_markup=user_kb.simple_back(),
        )
        return

    order = await orders_repo.get_for_update(session, int(order_id))
    if order is None or order.user_id != user.tg_id:
        await state.set_state(None)
        await message.answer("Заказ не найден.", reply_markup=user_kb.simple_back())
        return

    result = await delivery_service.accept_credentials(session, order, login, password)
    await state.set_state(None)
    await state.update_data(credentials_order_id=None, credentials_login=None)

    if result.repeated:
        await message.answer(
            "Реквизиты по этому заказу уже приняты.", reply_markup=user_kb.simple_back()
        )
        return
    if not result.ok:
        await message.answer(
            "Этот заказ сейчас не ждёт реквизиты.", reply_markup=user_kb.simple_back()
        )
        return

    text = await text_service.get(
        session,
        "credentials_accepted",
        order_id=order.id,
        token=order.token or "",
        title=html.escape(order.product_title),
    )
    await message.answer(text, reply_markup=user_kb.simple_back())
    await _notify_admins_in_work(bot, session, order)


async def _notify_admins_in_work(bot: Bot, session: AsyncSession, order) -> None:
    """Отдаёт заказ администраторам вместе с реквизитами."""
    buyer = await users_repo.get_user(session, order.user_id)
    await notify_service.notify_admins(
        bot,
        session,
        fulfillment_service.admin_card(order, buyer),
        reply_markup=admin_kb.fulfillment_card(
            order.id, fulfillment_service.buyer_username(buyer)
        ),
    )


# --- исход проверки оплаты --------------------------------------------------


async def _present_result(
    call: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    state: FSMContext,
    result: payments_service.ConfirmResult,
) -> None:
    outcome = result.outcome
    order = result.order

    if outcome == payments_service.Outcome.ACCEPTED:
        await _after_payment(bot, session, order)
        await _ask_credentials(call, session, state, order)
        return

    if outcome == payments_service.Outcome.ALREADY:
        if delivery_service.needs_credentials(order):
            await _ask_credentials(call, session, state, order)
            return
        status = OrderStatus.TITLES.get(order.status, order.status)
        await show(
            call,
            f"🧾 Заказ #{order.id} · токен <code>{order.token or '—'}</code>\n"
            f"📌 {status}",
            user_kb.simple_back(),
        )
        return

    if outcome == payments_service.Outcome.TOPPED_UP:
        await show(
            call,
            f"✅ Баланс пополнен на {format_kop(order.total_kop)}.",
            user_kb.simple_back(),
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
            f"Покупатель: <code>{order.user_id}</code>. Заказ остановлен.",
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


async def _after_payment(bot: Bot, session: AsyncSession, order) -> None:
    """Сообщает администраторам о поступивших деньгах.

    Это не то же самое, что заказ в работу: реквизитов ещё нет. Уведомление
    отделено намеренно — по нему видно оплаченные заказы, до которых покупатель
    так и не дошёл.
    """
    if not await settings_store.get_bool(session, "notify_admins_on_payment", True):
        return
    await notify_service.notify_admins(
        bot,
        session,
        f"💰 Оплачен заказ #{order.id}\n"
        f"🎟 Токен: <code>{order.token or '—'}</code>\n"
        f"📦 {html.escape(order.product_title)}\n"
        f"💵 {format_kop(order.total_kop)}\n"
        f"👤 <code>{order.user_id}</code>\n\n"
        f"Ждём от покупателя логин и пароль.",
    )
