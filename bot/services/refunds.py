"""Возврат денег.

Возврат на карту невозможен — у платёжного провайдера такого метода нет.
Поэтому возврат идёт на внутренний баланс: покупатель получает деньги за
секунду и может заказать заново, а магазину не приходится ждать
разбирательства.

Замена товара и брак партии отсюда убраны вместе со складом: заменять больше
нечего — заказ описывает работу, а не позицию. Если работа не сделана,
правильный выход один, и он здесь.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import utcnow
from bot.db.models import BalanceTxnKind, Order, OrderKind, OrderStatus
from bot.logger import payment_log
from bot.repo import balance as balance_repo
from bot.repo import orders as orders_repo
from bot.services import promo as promo_service

REFUNDABLE = (
    OrderStatus.PAID,
    OrderStatus.AWAITING_CREDENTIALS,
    OrderStatus.IN_WORK,
    OrderStatus.DELIVERED,
)
"""Состояния, из которых можно вернуть деньги.

Все четыре означают «деньги у нас». Заказ, ждущий реквизиты, и заказ в работе
входят сюда наравне с выполненным: если договориться не вышло на любом из
этих шагов, человека нельзя оставлять без денег и без работы.
"""


@dataclass(frozen=True)
class RefundResult:
    ok: bool
    amount_kop: int = 0
    detail: str | None = None


async def refund_to_balance(
    session: AsyncSession, order_id: int, admin_id: int, comment: str | None = None
) -> RefundResult:
    """Возвращает сумму заказа на баланс покупателя.

    Заказ переводится в `refunded`, использование промокода откатывается —
    иначе клиент теряет и работу, и промокод.
    """
    order = await orders_repo.get_for_update(session, order_id)
    if order is None:
        return RefundResult(False, detail="Заказ не найден")
    if order.status == OrderStatus.REFUNDED:
        return RefundResult(False, detail="Возврат по этому заказу уже сделан")
    if order.kind == OrderKind.TOPUP:
        # Пополнение баланса — не покупка. Деньги по нему УЖЕ на балансе, и
        # «возврат» начислил бы их второй раз: пополнил на тысячу, нажали
        # возврат — на балансе две. Забрать пополнение обратно тоже нельзя:
        # покупатель мог его уже потратить, и баланс ушёл бы в минус.
        # Правка баланса вручную для этого есть в карточке пользователя.
        return RefundResult(
            False,
            detail="Это пополнение баланса, а не покупка. "
            "Правьте баланс вручную в карточке пользователя.",
        )
    if order.status not in REFUNDABLE:
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
            "token": order.token,
            "amount_kop": order.total_kop,
            "admin_id": admin_id,
        },
    )
    return RefundResult(True, amount_kop=order.total_kop)


def order_can_be_refunded(order: Order) -> bool:
    return order.kind != OrderKind.TOPUP and order.status in REFUNDABLE
