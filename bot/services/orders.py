"""Жизненный цикл заказа.

Заказ создаётся вместе с резервом позиций склада. Резерв — не украшение:
без него два одновременных покупателя получают одну и ту же ссылку, и это
выясняется из жалоб, а не из логов.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import utcnow
from bot.db.models import DeliveryType, Order, OrderStatus, Product, PromoCode
from bot.repo import orders as orders_repo
from bot.repo import stock as stock_repo
from bot.utils.money import apply_discount


class OrderError(Exception):
    """Заказ создать нельзя."""


class OutOfStock(OrderError):
    def __init__(self, available: int) -> None:
        super().__init__(f"Свободно только {available} шт.")
        self.available = available


class ProductUnavailable(OrderError):
    pass


class BadQuantity(OrderError):
    def __init__(self, maximum: int) -> None:
        super().__init__(f"Количество должно быть от 1 до {maximum}")
        self.maximum = maximum


@dataclass(frozen=True)
class Quote:
    """Расчёт стоимости до создания заказа — для карточки товара."""

    qty: int
    unit_price_kop: int
    subtotal_kop: int
    discount_kop: int
    total_kop: int


def quote(product: Product, qty: int, promo: PromoCode | None = None) -> Quote:
    subtotal = int(product.price_kop) * int(qty)
    if promo is None:
        discount, total = apply_discount(subtotal, None, None)
    else:
        discount, total = apply_discount(subtotal, promo.discount_type, int(promo.discount_value))
    return Quote(
        qty=qty,
        unit_price_kop=int(product.price_kop),
        subtotal_kop=subtotal,
        discount_kop=discount,
        total_kop=total,
    )


async def create_order(
    session: AsyncSession,
    user_id: int,
    product: Product,
    qty: int,
    promo: PromoCode | None,
    reserve_minutes: int,
    max_qty: int,
) -> Order:
    """Создаёт заказ и захватывает позиции склада.

    Если позиций не хватает — исключение, заказ не создаётся. Выдать половину
    заказа хуже, чем не продать: половину придётся разбирать вручную.
    """
    if not product.is_active:
        raise ProductUnavailable("Товар снят с продажи")
    if qty < 1 or qty > max_qty:
        raise BadQuantity(max_qty)

    # Товар с ручной выдачей склада не имеет: выдаёт администратор, а не бот.
    # Проверять и резервировать нечего.
    needs_stock = product.delivery_type in DeliveryType.NEEDS_STOCK
    if needs_stock:
        available = await stock_repo.available_count(session, product.id)
        if available < qty:
            raise OutOfStock(available)

    calc = quote(product, qty, promo)
    expires_at = utcnow() + timedelta(minutes=reserve_minutes)

    order = Order(
        user_id=user_id,
        product_id=product.id,
        product_title=product.title,
        delivery_type=product.delivery_type,
        qty=qty,
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

    if not needs_stock:
        return order

    reserved = await stock_repo.reserve_items(
        session, product.id, qty, order.id, expires_at
    )
    if len(reserved) < qty:
        # Между подсчётом и захватом кто-то успел купить. Считаем остаток
        # заново и отказываем: частичный резерв не оставляем.
        await stock_repo.release_order(session, order.id)
        await session.flush()
        current = await stock_repo.available_count(session, product.id)
        raise OutOfStock(current)

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
    """Отменяет заказ и возвращает позиции в продажу."""
    if order.status not in OrderStatus.OPEN:
        return
    await stock_repo.release_order(session, order.id)
    order.status = (
        OrderStatus.EXPIRED if reason == "expired" else OrderStatus.CANCELED
    )
    await session.flush()


async def expire_stale(session: AsyncSession, limit: int = 200) -> list[int]:
    """Освобождает просроченные резервы.

    Возвращает номера истёкших заказов, чтобы бот мог сообщить покупателям.
    """
    now = utcnow()
    expired: list[int] = []
    for order in await orders_repo.expired_candidates(session, now, limit):
        await stock_repo.release_order(session, order.id)
        order.status = OrderStatus.EXPIRED
        expired.append(order.id)
    await session.flush()

    # Подстраховка: позиции, у которых истёк резерв, а заказ по какой-то
    # причине не нашёлся. Без неё товар зависает в резерве навсегда.
    for order_id in await stock_repo.expired_reserved_order_ids(session, now):
        await stock_repo.release_order(session, order_id)
    await session.flush()
    return expired


def is_payable(order: Order) -> bool:
    return order.status in OrderStatus.OPEN


def summary_lines(order: Order) -> list[str]:
    from bot.utils.money import format_kop

    lines = [
        f"🧾 Заказ <b>#{order.id}</b>",
        f"📦 {order.product_title} — {order.qty} шт.",
        f"💵 Сумма: {format_kop(order.subtotal_kop)}",
    ]
    if order.discount_kop:
        promo = f" ({order.promo_code})" if order.promo_code else ""
        lines.append(f"🎟 Скидка{promo}: −{format_kop(order.discount_kop)}")
    lines.append(f"💰 Итого: <b>{format_kop(order.total_kop)}</b>")
    lines.append(f"📌 Статус: {OrderStatus.TITLES.get(order.status, order.status)}")
    return lines
