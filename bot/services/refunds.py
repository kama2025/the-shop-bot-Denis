"""Гарантия: замена товара, возврат денег, брак партии.

Возврат на карту через Platega невозможен — в её API такого метода нет. Поэтому
возврат идёт на внутренний баланс: покупатель получает деньги за секунду и
может купить заново, а магазину не приходится ждать разбирательства.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import utcnow
from bot.db.models import BalanceTxnKind, Order, OrderStatus, StockStatus
from bot.logger import payment_log
from bot.repo import balance as balance_repo
from bot.repo import orders as orders_repo
from bot.repo import stock as stock_repo
from bot.services import promo as promo_service


@dataclass(frozen=True)
class RefundResult:
    ok: bool
    amount_kop: int = 0
    detail: str | None = None


@dataclass(frozen=True)
class ReplaceResult:
    ok: bool
    contents: list[str] | None = None
    detail: str | None = None


async def refund_to_balance(
    session: AsyncSession, order_id: int, admin_id: int, comment: str | None = None
) -> RefundResult:
    """Возвращает сумму заказа на баланс покупателя.

    Заказ переводится в `refunded`, использование промокода откатывается —
    иначе клиент теряет и товар, и промокод.
    """
    order = await orders_repo.get_for_update(session, order_id)
    if order is None:
        return RefundResult(False, detail="Заказ не найден")
    if order.status == OrderStatus.REFUNDED:
        return RefundResult(False, detail="Возврат по этому заказу уже сделан")
    if order.status not in (OrderStatus.PAID, OrderStatus.DELIVERED):
        return RefundResult(False, detail="Возврат возможен только по оплаченному заказу")

    await balance_repo.move(
        session,
        user_id=order.user_id,
        amount_kop=order.total_kop,
        kind=BalanceTxnKind.REFUND,
        order_id=order.id,
        admin_id=admin_id,
        comment=comment or f"Возврат по заказу #{order.id}",
    )

    if order.promo_id:
        await promo_service.release(session, order.promo_id, order.id)

    # Выданные позиции помечаем браком: они уже у покупателя, продавать их
    # повторно нельзя.
    items = await stock_repo.items_of_order(session, order.id)
    await stock_repo.mark_items_defective(
        session,
        [item.id for item in items if item.status == StockStatus.SOLD],
        reason=f"Возврат по заказу #{order.id}",
    )

    order.status = OrderStatus.REFUNDED
    order.refunded_at = utcnow()
    if comment:
        order.admin_note = comment[:255]
    await session.flush()

    payment_log.info(
        "Возврат на баланс",
        extra={
            "order_id": order.id,
            "user_id": order.user_id,
            "amount_kop": order.total_kop,
            "admin_id": admin_id,
        },
    )
    return RefundResult(True, amount_kop=order.total_kop)


async def replace_items(
    session: AsyncSession, order_id: int, admin_id: int, reason: str
) -> ReplaceResult:
    """Выдаёт другие позиции взамен нерабочих."""
    order = await orders_repo.get_for_update(session, order_id)
    if order is None:
        return ReplaceResult(False, detail="Заказ не найден")
    if order.status != OrderStatus.DELIVERED:
        return ReplaceResult(False, detail="Заменить можно только выданный заказ")
    if order.product_id is None:
        return ReplaceResult(False, detail="Товар удалён из каталога — замена невозможна")

    available = await stock_repo.available_count(session, order.product_id)
    if available < order.qty:
        return ReplaceResult(
            False, detail=f"На складе только {available} шт., нужно {order.qty}"
        )

    old_items = [
        item
        for item in await stock_repo.items_of_order(session, order.id)
        if item.status == StockStatus.SOLD
    ]
    old_ids = [item.id for item in old_items]

    # Сначала отвязываем старые позиции, потом берём новые: иначе `items_of_order`
    # вернёт и те и другие, и следующая замена запутается.
    await stock_repo.mark_items_defective(
        session, old_ids, reason=f"Замена по заказу #{order.id}: {reason}"
    )
    for item in old_items:
        item.order_id = None
    await session.flush()

    new_items = await stock_repo.take_available(
        session, order.product_id, order.qty, order.id
    )
    if len(new_items) < order.qty:
        return ReplaceResult(False, detail="Позиции разобрали, пока шла замена")

    old_rows = await orders_repo.items_of(session, order.id)
    for row, new_item in zip(old_rows, new_items, strict=False):
        row.replaced_by_item_id = new_item.id
    await orders_repo.add_items(session, order.id, [item.id for item in new_items])

    order.admin_note = f"Замена: {reason}"[:255]
    await session.flush()

    payment_log.info(
        "Замена товара по гарантии",
        extra={
            "order_id": order.id,
            "user_id": order.user_id,
            "admin_id": admin_id,
            "qty": order.qty,
            "reason": reason,
        },
    )
    return ReplaceResult(True, contents=[item.content for item in new_items])


async def reject_batch(
    session: AsyncSession, batch_id: int, admin_id: int, reason: str
) -> int:
    """Бракует партию целиком. Возвращает число снятых с продажи позиций."""
    affected = await stock_repo.mark_batch_defective(session, batch_id, reason)
    payment_log.info(
        "Партия забракована",
        extra={"batch_id": batch_id, "admin_id": admin_id, "items": affected, "reason": reason},
    )
    return affected


def order_can_be_refunded(order: Order) -> bool:
    return order.status in (OrderStatus.PAID, OrderStatus.DELIVERED)
