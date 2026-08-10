"""Выдача товара.

Единственное место, где содержимое склада попадает покупателю. Функция
идемпотентна: сколько бы раз её ни вызвали и в каком бы порядке ни пришли
callback, нажатие кнопки и опрос поллера — товар выдаётся один раз.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import utcnow
from bot.db.models import Order, OrderStatus, StockItem
from bot.logger import payment_log
from bot.repo import orders as orders_repo
from bot.repo import stock as stock_repo


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    contents: list[str]
    already_delivered: bool = False
    shortage: bool = False


async def deliver(session: AsyncSession, order: Order) -> DeliveryResult:
    """Выдаёт позиции заказа.

    Вызывающий обязан держать заказ под блокировкой строки
    (`orders_repo.get_for_update`) — иначе идемпотентность держится на удаче.
    """
    if order.status == OrderStatus.DELIVERED:
        contents = await _delivered_contents(session, order.id)
        return DeliveryResult(ok=True, contents=contents, already_delivered=True)

    if order.status not in (OrderStatus.PAID, OrderStatus.DELIVERED):
        return DeliveryResult(ok=False, contents=[])

    items = [
        item
        for item in await stock_repo.items_of_order(session, order.id)
        if item.status in ("reserved", "sold")
    ]

    if len(items) < order.qty:
        # Оплата прошла, а склада не хватило. Такое возможно, если позиции
        # забраковали между резервом и оплатой. Молчать нельзя: и покупателю,
        # и админам нужно узнать об этом сразу.
        payment_log.error(
            "Не хватает позиций для выдачи",
            extra={
                "order_id": order.id,
                "user_id": order.user_id,
                "need": order.qty,
                "have": len(items),
            },
        )
        return DeliveryResult(ok=False, contents=[], shortage=True)

    await stock_repo.mark_sold(session, order.id)
    for item in items:
        if item.sold_at is None:
            item.sold_at = utcnow()

    existing = {row.stock_item_id for row in await orders_repo.items_of(session, order.id)}
    new_ids = [item.id for item in items if item.id not in existing]
    if new_ids:
        await orders_repo.add_items(session, order.id, new_ids)

    order.status = OrderStatus.DELIVERED
    order.delivered_at = utcnow()
    await session.flush()

    payment_log.info(
        "Товар выдан",
        extra={
            "order_id": order.id,
            "user_id": order.user_id,
            "qty": order.qty,
            "total_kop": order.total_kop,
        },
    )
    return DeliveryResult(ok=True, contents=[item.content for item in items])


async def _delivered_contents(session: AsyncSession, order_id: int) -> list[str]:
    rows = await orders_repo.items_of(session, order_id)
    contents: list[str] = []
    for row in rows:
        item = await session.get(StockItem, row.stock_item_id)
        if item is not None:
            contents.append(item.content)
    return contents


async def contents_of(session: AsyncSession, order_id: int) -> list[str]:
    """Что было выдано по заказу — для истории покупок и карточки в админке."""
    return await _delivered_contents(session, order_id)


def format_contents(contents: list[str]) -> str:
    """Оформляет выданные позиции в сообщение.

    Каждая позиция отдельным блоком `<code>`: так её удобно скопировать одним
    касанием, и Telegram не превратит ссылку в предпросмотр.
    """
    if not contents:
        return "—"
    if len(contents) == 1:
        return f"<code>{_escape(contents[0])}</code>"
    return "\n\n".join(
        f"<b>{index}.</b>\n<code>{_escape(content)}</code>"
        for index, content in enumerate(contents, start=1)
    )


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
