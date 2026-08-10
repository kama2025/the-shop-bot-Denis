"""Статистика для админки.

Считается запросами к базе, а не в Python: выгружать все заказы, чтобы их
сложить, работает ровно до первой тысячи.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import utcnow
from bot.db.models import Order, OrderKind, OrderStatus, Product, StockItem, StockStatus, User

PAID_STATUSES = (OrderStatus.PAID, OrderStatus.DELIVERED)


@dataclass
class Snapshot:
    users_total: int = 0
    users_today: int = 0
    users_week: int = 0
    users_month: int = 0
    active_today: int = 0

    orders_total: int = 0
    orders_paid: int = 0
    orders_today: int = 0
    revenue_kop: int = 0
    revenue_today_kop: int = 0
    revenue_month_kop: int = 0
    refunded_kop: int = 0
    average_check_kop: int = 0

    stock_available: int = 0
    stock_sold: int = 0
    stock_defective: int = 0

    top_products: list[tuple[str, int, int]] = field(default_factory=list)


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def collect(session: AsyncSession) -> Snapshot:
    now = utcnow()
    today = _day_start(now)
    week = now - timedelta(days=7)
    month = now - timedelta(days=30)

    snapshot = Snapshot()

    snapshot.users_total = int(
        (await session.execute(select(func.count(User.tg_id)))).scalar_one()
    )
    snapshot.users_today = int(
        (
            await session.execute(select(func.count(User.tg_id)).where(User.created_at >= today))
        ).scalar_one()
    )
    snapshot.users_week = int(
        (
            await session.execute(select(func.count(User.tg_id)).where(User.created_at >= week))
        ).scalar_one()
    )
    snapshot.users_month = int(
        (
            await session.execute(select(func.count(User.tg_id)).where(User.created_at >= month))
        ).scalar_one()
    )
    snapshot.active_today = int(
        (
            await session.execute(
                select(func.count(User.tg_id)).where(User.last_seen_at >= today)
            )
        ).scalar_one()
    )

    purchases = Order.kind == OrderKind.PURCHASE

    snapshot.orders_total = int(
        (await session.execute(select(func.count(Order.id)).where(purchases))).scalar_one()
    )

    paid_row = (
        await session.execute(
            select(func.count(Order.id), func.coalesce(func.sum(Order.total_kop), 0)).where(
                purchases, Order.status.in_(PAID_STATUSES)
            )
        )
    ).one()
    snapshot.orders_paid = int(paid_row[0])
    snapshot.revenue_kop = int(paid_row[1] or 0)

    snapshot.orders_today = int(
        (
            await session.execute(
                select(func.count(Order.id)).where(
                    purchases, Order.status.in_(PAID_STATUSES), Order.paid_at >= today
                )
            )
        ).scalar_one()
    )
    snapshot.revenue_today_kop = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Order.total_kop), 0)).where(
                    purchases, Order.status.in_(PAID_STATUSES), Order.paid_at >= today
                )
            )
        ).scalar_one()
        or 0
    )
    snapshot.revenue_month_kop = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Order.total_kop), 0)).where(
                    purchases, Order.status.in_(PAID_STATUSES), Order.paid_at >= month
                )
            )
        ).scalar_one()
        or 0
    )
    snapshot.refunded_kop = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Order.total_kop), 0)).where(
                    Order.status == OrderStatus.REFUNDED
                )
            )
        ).scalar_one()
        or 0
    )

    if snapshot.orders_paid:
        snapshot.average_check_kop = snapshot.revenue_kop // snapshot.orders_paid

    stock_rows = (
        await session.execute(
            select(StockItem.status, func.count(StockItem.id)).group_by(StockItem.status)
        )
    ).all()
    stock_counts = {status: int(count) for status, count in stock_rows}
    snapshot.stock_available = stock_counts.get(StockStatus.AVAILABLE, 0)
    snapshot.stock_sold = stock_counts.get(StockStatus.SOLD, 0)
    snapshot.stock_defective = stock_counts.get(StockStatus.DEFECTIVE, 0)

    top_rows = (
        await session.execute(
            select(
                Order.product_title,
                func.coalesce(func.sum(Order.qty), 0),
                func.coalesce(func.sum(Order.total_kop), 0),
            )
            .where(purchases, Order.status.in_(PAID_STATUSES))
            .group_by(Order.product_title)
            .order_by(func.sum(Order.qty).desc())
            .limit(5)
        )
    ).all()
    snapshot.top_products = [(str(a), int(b or 0), int(c or 0)) for a, b, c in top_rows]

    return snapshot


async def low_stock(session: AsyncSession, threshold: int) -> list[tuple[Product, int]]:
    """Товары, у которых остаток ниже порога."""
    counts = (
        select(
            StockItem.product_id.label("pid"),
            func.count(StockItem.id).label("free"),
        )
        .where(StockItem.status == StockStatus.AVAILABLE)
        .group_by(StockItem.product_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Product, func.coalesce(counts.c.free, 0))
            .outerjoin(counts, counts.c.pid == Product.id)
            .where(Product.is_active.is_(True), func.coalesce(counts.c.free, 0) <= threshold)
            .order_by(func.coalesce(counts.c.free, 0))
        )
    ).all()
    return [(product, int(free)) for product, free in rows]


def format_snapshot(snapshot: Snapshot) -> str:
    from bot.utils.money import format_kop

    lines = [
        "📊 <b>Статистика</b>",
        "",
        "<b>Пользователи</b>",
        f"• Всего: {snapshot.users_total}",
        f"• За сегодня: +{snapshot.users_today}",
        f"• За неделю: +{snapshot.users_week}",
        f"• За месяц: +{snapshot.users_month}",
        f"• Активны сегодня: {snapshot.active_today}",
        "",
        "<b>Продажи</b>",
        f"• Заказов всего: {snapshot.orders_total}",
        f"• Оплачено: {snapshot.orders_paid}",
        f"• Оплат сегодня: {snapshot.orders_today}",
        f"• Выручка всего: {format_kop(snapshot.revenue_kop)}",
        f"• Выручка сегодня: {format_kop(snapshot.revenue_today_kop)}",
        f"• Выручка за месяц: {format_kop(snapshot.revenue_month_kop)}",
        f"• Средний чек: {format_kop(snapshot.average_check_kop)}",
        f"• Возвращено: {format_kop(snapshot.refunded_kop)}",
        "",
        "<b>Склад</b>",
        f"• Свободно: {snapshot.stock_available}",
        f"• Продано: {snapshot.stock_sold}",
        f"• Брак: {snapshot.stock_defective}",
    ]
    if snapshot.top_products:
        lines += ["", "<b>Популярные товары</b>"]
        for index, (title, qty, revenue) in enumerate(snapshot.top_products, start=1):
            lines.append(f"{index}. {title} — {qty} шт., {format_kop(revenue)}")
    return "\n".join(lines)
