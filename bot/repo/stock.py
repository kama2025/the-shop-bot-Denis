"""Доступ к складу.

Здесь живёт самый ответственный запрос магазина — захват позиций под заказ.
Он обязан быть устойчив к двум одновременным покупателям: без блокировки строк
оба получат одну и ту же ссылку, и это выяснится только из жалоб.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import utcnow
from bot.db.models import StockBatch, StockItem, StockStatus


async def available_count(session: AsyncSession, product_id: int) -> int:
    stmt = select(func.count(StockItem.id)).where(
        StockItem.product_id == product_id, StockItem.status == StockStatus.AVAILABLE
    )
    return int((await session.execute(stmt)).scalar_one())


async def counts_by_status(session: AsyncSession, product_id: int) -> dict[str, int]:
    stmt = (
        select(StockItem.status, func.count(StockItem.id))
        .where(StockItem.product_id == product_id)
        .group_by(StockItem.status)
    )
    rows = (await session.execute(stmt)).all()
    counts = dict.fromkeys(StockStatus.ALL, 0)
    counts.update({status: int(count) for status, count in rows})
    return counts


async def reserve_items(
    session: AsyncSession,
    product_id: int,
    qty: int,
    order_id: int,
    reserved_until: datetime,
) -> list[StockItem]:
    """Захватывает `qty` свободных позиций под заказ.

    `FOR UPDATE SKIP LOCKED` — параллельная покупка не ждёт нашу транзакцию, а
    сразу берёт следующие свободные позиции. Если свободных меньше, чем нужно,
    возвращается пустой список и заказ не создаётся: выдать половину заказа
    хуже, чем не продать.
    """
    if qty < 1:
        return []
    stmt = (
        select(StockItem)
        .where(
            StockItem.product_id == product_id,
            StockItem.status == StockStatus.AVAILABLE,
        )
        .order_by(StockItem.id)
        .limit(qty)
        .with_for_update(skip_locked=True)
    )
    items = list((await session.execute(stmt)).scalars().all())
    if len(items) < qty:
        return []

    for item in items:
        item.status = StockStatus.RESERVED
        item.order_id = order_id
        item.reserved_until = reserved_until
    await session.flush()
    return items


async def items_of_order(session: AsyncSession, order_id: int) -> list[StockItem]:
    stmt = select(StockItem).where(StockItem.order_id == order_id).order_by(StockItem.id)
    return list((await session.execute(stmt)).scalars().all())


async def mark_sold(session: AsyncSession, order_id: int) -> int:
    result = await session.execute(
        update(StockItem)
        .where(StockItem.order_id == order_id, StockItem.status == StockStatus.RESERVED)
        .values(status=StockStatus.SOLD, sold_at=utcnow(), reserved_until=None)
    )
    return int(result.rowcount or 0)


async def release_order(session: AsyncSession, order_id: int) -> int:
    """Возвращает зарезервированные позиции заказа обратно в продажу."""
    result = await session.execute(
        update(StockItem)
        .where(StockItem.order_id == order_id, StockItem.status == StockStatus.RESERVED)
        .values(
            status=StockStatus.AVAILABLE,
            order_id=None,
            reserved_until=None,
        )
    )
    return int(result.rowcount or 0)


async def expired_reserved_order_ids(session: AsyncSession, now: datetime | None = None) -> list[int]:
    now = now or utcnow()
    stmt = (
        select(StockItem.order_id)
        .where(
            StockItem.status == StockStatus.RESERVED,
            StockItem.reserved_until.is_not(None),
            StockItem.reserved_until < now,
            StockItem.order_id.is_not(None),
        )
        .group_by(StockItem.order_id)
    )
    return [int(x) for x in (await session.execute(stmt)).scalars().all() if x]


async def add_batch(
    session: AsyncSession,
    product_id: int,
    contents: list[str],
    admin_id: int | None,
    note: str | None = None,
) -> StockBatch:
    batch = StockBatch(
        product_id=product_id,
        admin_id=admin_id,
        items_count=len(contents),
        note=note,
    )
    session.add(batch)
    await session.flush()
    session.add_all(
        StockItem(product_id=product_id, batch_id=batch.id, content=content)
        for content in contents
    )
    await session.flush()
    return batch


async def list_batches(session: AsyncSession, product_id: int, limit: int = 20) -> list[StockBatch]:
    stmt = (
        select(StockBatch)
        .where(StockBatch.product_id == product_id)
        .order_by(StockBatch.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_batch(session: AsyncSession, batch_id: int) -> StockBatch | None:
    return await session.get(StockBatch, batch_id)


async def mark_batch_defective(session: AsyncSession, batch_id: int, reason: str) -> int:
    """Бракует всю партию.

    Трогает только свободные и зарезервированные позиции. Проданные не
    переписываются: заказ, который уже выдан, — исторический факт, а разбор
    по нему идёт через возврат, а не через правку склада.
    """
    result = await session.execute(
        update(StockItem)
        .where(
            StockItem.batch_id == batch_id,
            StockItem.status.in_([StockStatus.AVAILABLE, StockStatus.RESERVED]),
        )
        .values(
            status=StockStatus.DEFECTIVE,
            defect_reason=reason[:255],
            order_id=None,
            reserved_until=None,
        )
    )
    return int(result.rowcount or 0)


async def mark_items_defective(
    session: AsyncSession, item_ids: list[int], reason: str
) -> int:
    if not item_ids:
        return 0
    result = await session.execute(
        update(StockItem)
        .where(StockItem.id.in_(item_ids))
        .values(status=StockStatus.DEFECTIVE, defect_reason=reason[:255])
    )
    return int(result.rowcount or 0)


async def take_available(
    session: AsyncSession, product_id: int, qty: int, order_id: int
) -> list[StockItem]:
    """Берёт свободные позиции сразу как проданные — для замены по гарантии."""
    stmt = (
        select(StockItem)
        .where(
            StockItem.product_id == product_id,
            StockItem.status == StockStatus.AVAILABLE,
        )
        .order_by(StockItem.id)
        .limit(qty)
        .with_for_update(skip_locked=True)
    )
    items = list((await session.execute(stmt)).scalars().all())
    if len(items) < qty:
        return []
    for item in items:
        item.status = StockStatus.SOLD
        item.order_id = order_id
        item.sold_at = utcnow()
    await session.flush()
    return items


async def purge_defective(session: AsyncSession, product_id: int) -> int:
    """Удаляет бракованные позиции товара, чтобы они не копились в базе."""
    from sqlalchemy import delete

    result = await session.execute(
        delete(StockItem).where(
            StockItem.product_id == product_id,
            StockItem.status == StockStatus.DEFECTIVE,
        )
    )
    return int(result.rowcount or 0)
