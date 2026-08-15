"""Жизненный цикл заказа.

Заказ — это обязательство сделать работу над аккаунтом покупателя. Склада нет,
резервировать нечего; вместо остатка заказ держит **снимок цены**.

Снимок — не украшение. Товар стоит в долларах, платят рублями по курсу ЦБ, а
курс меняется. Если сумму пересчитывать при каждом обращении, покупатель,
вернувшийся к неоплаченному счёту через час, увидит другое число, а сверка
суммы с провайдером перестанет сходиться. Поэтому цена, курс и наценка
замораживаются в момент создания заказа и дальше только читаются.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import utcnow
from bot.db.models import Order, OrderStatus, Product, PromoCode
from bot.repo import orders as orders_repo
from bot.services import pricing
from bot.utils.money import apply_discount


class OrderError(Exception):
    """Заказ создать нельзя."""


class ProductUnavailable(OrderError):
    pass


class RateUnavailable(OrderError):
    """Курса нет — продавать нечем.

    Отдельный класс, а не общий отказ: это временная беда на нашей стороне,
    и покупателю надо сказать «попробуйте позже», а не «товара нет».
    """


@dataclass(frozen=True)
class Quote:
    """Расчёт стоимости до создания заказа — для карточки товара."""

    price_usd_cents: int
    rate_kop: int
    markup_pct: int
    base_kop: int       # чистый пересчёт по курсу, без наценки
    unit_price_kop: int  # с наценкой — то, что уйдёт в счёт
    subtotal_kop: int
    discount_kop: int
    total_kop: int


def quote(
    product: Product,
    rate_kop: int,
    markup_pct: int,
    promo: PromoCode | None = None,
) -> Quote:
    """Считает цену заказа.

    Порядок обязателен: курс → наценка → скидка. Промокод применяется к сумме
    с наценкой, то есть к тому числу, которое покупателю назвали. Скидка от
    суммы без наценки выглядела бы в чеке как обман.
    """
    if rate_kop <= 0:
        raise RateUnavailable("Курс валют недоступен")

    price_usd_cents = int(product.price_usd_cents)
    base = pricing.base_kop(price_usd_cents, rate_kop)
    unit = pricing.with_markup(base, markup_pct)

    if promo is None:
        discount, total = apply_discount(unit, None, None)
    else:
        discount, total = apply_discount(unit, promo.discount_type, int(promo.discount_value))

    return Quote(
        price_usd_cents=price_usd_cents,
        rate_kop=rate_kop,
        markup_pct=markup_pct,
        base_kop=base,
        unit_price_kop=unit,
        subtotal_kop=unit,
        discount_kop=discount,
        total_kop=total,
    )


async def create_order(
    session: AsyncSession,
    user_id: int,
    product: Product,
    promo: PromoCode | None,
    rate_kop: int,
    markup_pct: int,
    reserve_minutes: int,
) -> Order:
    """Создаёт заказ с замороженной ценой.

    Количество всегда 1: один заказ — один аккаунт. Поле в таблице осталось,
    чтобы не переписывать выгрузку и отчёты, но выбора количества у покупателя
    больше нет.
    """
    if not product.is_active:
        raise ProductUnavailable("Товар снят с продажи")

    calc = quote(product, rate_kop, markup_pct, promo)
    expires_at = utcnow() + timedelta(minutes=reserve_minutes)

    order = Order(
        user_id=user_id,
        product_id=product.id,
        product_title=product.title,
        price_usd_cents=calc.price_usd_cents,
        rate_kop=calc.rate_kop,
        markup_pct=calc.markup_pct,
        qty=1,
        unit_price_kop=calc.unit_price_kop,
        subtotal_kop=calc.subtotal_kop,
        discount_kop=calc.discount_kop,
        total_kop=calc.total_kop,
        promo_id=promo.id if promo else None,
        promo_code=promo.code if promo else None,
        status=OrderStatus.NEW,
        reserve_expires_at=expires_at,
    )
    session.add(order)
    await session.flush()
    return order


async def attach_payment(
    session: AsyncSession,
    order: Order,
    provider: str,
    payment_method: str,
    provider_txn_id: str,
    pay_url: str | None,
) -> None:
    order.provider = provider
    order.payment_method = payment_method
    order.provider_txn_id = provider_txn_id
    order.pay_url = pay_url
    order.status = OrderStatus.PENDING
    await session.flush()


async def cancel(session: AsyncSession, order: Order, reason: str = "canceled") -> None:
    """Отменяет неоплаченный заказ."""
    if order.status not in OrderStatus.OPEN:
        return
    order.status = OrderStatus.EXPIRED if reason == "expired" else OrderStatus.CANCELED
    await session.flush()


async def expire_stale(session: AsyncSession, limit: int = 200) -> list[int]:
    """Закрывает счета, по которым не заплатили.

    Возвращает номера истёкших заказов, чтобы бот мог сообщить покупателям.
    Оплаченные заказы сюда не попадают: `expired_candidates` отбирает только
    открытые, а деньги, за которые работа ещё не сделана, по таймауту не
    сгорают.
    """
    now = utcnow()
    expired: list[int] = []
    for order in await orders_repo.expired_candidates(session, now, limit):
        order.status = OrderStatus.EXPIRED
        expired.append(order.id)
    await session.flush()
    return expired


def is_payable(order: Order) -> bool:
    return order.status in OrderStatus.OPEN


def summary_lines(order: Order) -> list[str]:
    from bot.utils.money import format_kop

    lines = [f"🧾 Заказ <b>#{order.id}</b>"]
    if order.token:
        lines.append(f"🎟 Токен: <code>{order.token}</code>")
    lines.append(f"📦 {order.product_title}")
    if order.price_usd_cents:
        lines.append(
            f"💵 Цена: {pricing.format_usd(order.price_usd_cents)}"
            f" по курсу {format_kop(order.rate_kop)} за $1"
        )
    if order.markup_pct:
        lines.append(f"➕ Сервисный сбор: {order.markup_pct}%")
    if order.discount_kop:
        promo = f" ({order.promo_code})" if order.promo_code else ""
        lines.append(f"🎟 Скидка{promo}: −{format_kop(order.discount_kop)}")
    lines.append(f"💰 Итого: <b>{format_kop(order.total_kop)}</b>")
    lines.append(f"📌 Статус: {OrderStatus.TITLES.get(order.status, order.status)}")
    return lines
