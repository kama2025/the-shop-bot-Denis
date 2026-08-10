"""Доступ к заказам, позициям заказа и журналу платежей."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Order, OrderItem, OrderStatus, Payment


async def get(session: AsyncSession, order_id: int) -> Order | None:
    return await session.get(Order, order_id)


async def get_for_update(session: AsyncSession, order_id: int) -> Order | None:
    """Заказ с блокировкой строки.

    Через эту функцию проходит подтверждение оплаты: три разных источника
    (callback, кнопка, поллер) не должны выдать товар дважды.
    """
    result = await session.execute(select(Order).where(Order.id == order_id).with_for_update())
    return result.scalar_one_or_none()


async def by_provider_txn(
    session: AsyncSession, provider: str, txn_id: str
) -> Order | None:
    result = await session.execute(
        select(Order).where(Order.provider == provider, Order.provider_txn_id == txn_id)
    )
    return result.scalar_one_or_none()


async def list_for_user(
    session: AsyncSession, user_id: int, limit: int = 20, offset: int = 0
) -> list[Order]:
    stmt = (
        select(Order)
        .where(Order.user_id == user_id)
        .order_by(Order.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars().all())


async def count_for_user(session: AsyncSession, user_id: int) -> int:
    stmt = select(func.count(Order.id)).where(Order.user_id == user_id)
    return int((await session.execute(stmt)).scalar_one())


async def list_paid_for_user(
    session: AsyncSession, user_id: int, limit: int = 20, offset: int = 0
) -> list[Order]:
    stmt = (
        select(Order)
        .where(
            Order.user_id == user_id,
            Order.status.in_([OrderStatus.DELIVERED, OrderStatus.PAID, OrderStatus.REFUNDED]),
        )
        .order_by(Order.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars().all())


async def count_paid_for_user(session: AsyncSession, user_id: int) -> int:
    stmt = select(func.count(Order.id)).where(
        Order.user_id == user_id,
        Order.status.in_([OrderStatus.DELIVERED, OrderStatus.PAID, OrderStatus.REFUNDED]),
    )
    return int((await session.execute(stmt)).scalar_one())


async def list_all(
    session: AsyncSession,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[Order]:
    stmt = select(Order)
    if status:
        stmt = stmt.where(Order.status == status)
    stmt = stmt.order_by(Order.created_at.desc()).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


async def count_all(session: AsyncSession, status: str | None = None) -> int:
    stmt = select(func.count(Order.id))
    if status:
        stmt = stmt.where(Order.status == status)
    return int((await session.execute(stmt)).scalar_one())


async def search(session: AsyncSession, query: str, limit: int = 20) -> list[Order]:
    """Поиск заказа по его номеру или по Telegram ID покупателя."""
    query = query.strip().lstrip("#@")
    if not query.isdigit():
        return []
    number = int(query)
    stmt = (
        select(Order)
        .where((Order.id == number) | (Order.user_id == number))
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def stale_pending(
    session: AsyncSession, older_than: datetime, limit: int = 100
) -> list[Order]:
    """Заказы, которые ждут оплаты и требуют сверки с провайдером."""
    stmt = (
        select(Order)
        .where(
            Order.status == OrderStatus.PENDING,
            Order.provider_txn_id.is_not(None),
            Order.created_at <= older_than,
        )
        .order_by(Order.created_at)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def expired_candidates(session: AsyncSession, now: datetime, limit: int = 200) -> list[Order]:
    stmt = (
        select(Order)
        .where(
            Order.status.in_(list(OrderStatus.OPEN)),
            Order.reserve_expires_at.is_not(None),
            Order.reserve_expires_at < now,
        )
        .order_by(Order.reserve_expires_at)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def add_items(session: AsyncSession, order_id: int, stock_item_ids: list[int]) -> None:
    session.add_all(
        OrderItem(order_id=order_id, stock_item_id=item_id) for item_id in stock_item_ids
    )
    await session.flush()


async def items_of(session: AsyncSession, order_id: int) -> list[OrderItem]:
    stmt = select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.id)
    return list((await session.execute(stmt)).scalars().all())


async def log_payment(
    session: AsyncSession,
    order_id: int,
    provider: str,
    provider_txn_id: str | None,
    amount_kop: int,
    status: str,
    event: str,
    raw: dict | None = None,
) -> Payment:
    payment = Payment(
        order_id=order_id,
        provider=provider,
        provider_txn_id=provider_txn_id,
        amount_kop=amount_kop,
        status=status,
        event=event,
        raw=raw,
    )
    session.add(payment)
    await session.flush()
    return payment


async def payments_of(session: AsyncSession, order_id: int) -> list[Payment]:
    stmt = select(Payment).where(Payment.order_id == order_id).order_by(Payment.created_at)
    return list((await session.execute(stmt)).scalars().all())
