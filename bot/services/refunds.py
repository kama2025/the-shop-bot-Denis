"""Возврат по заказу.

**Денег бот не возвращает.** Внутреннего баланса больше нет, а возврат на карту
у платёжного провайдера недоступен — такого метода в его API нет. Поэтому здесь
остался учёт: заказ помечается возвращённым, промокод откатывается, покупателю
уходит сообщение, а сами деньги владелец отправляет покупателю сам — переводом,
как договорятся.

Это осознанное ограничение, а не недоделка. Отметка без перевода денег хуже,
чем ничего, ровно в одном случае: если про неё забыть. Поэтому в карточке
заказа и в сообщении покупателю прямо сказано, что перевод — ручной.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import utcnow
from bot.db.models import Order, OrderStatus
from bot.logger import payment_log
from bot.repo import orders as orders_repo
from bot.services import promo as promo_service

REFUNDABLE = (
    OrderStatus.PAID,
    OrderStatus.AWAITING_CREDENTIALS,
    OrderStatus.IN_WORK,
    OrderStatus.DELIVERED,
)
"""Состояния, из которых можно оформить возврат.

Все четыре означают «деньги у нас». Заказ, ждущий реквизиты, и заказ в работе
входят сюда наравне с выполненным: если договориться не вышло на любом из этих
шагов, человека нельзя оставлять и без денег, и без работы.
"""


@dataclass(frozen=True)
class RefundResult:
    ok: bool
    amount_kop: int = 0
    detail: str | None = None


async def mark_refunded(
    session: AsyncSession, order_id: int, admin_id: int, comment: str | None = None
) -> RefundResult:
    """Помечает заказ возвращённым и откатывает промокод.

    Возвращает сумму, которую владелец обязан перевести покупателю сам.
    Повторный вызов по тому же заказу отклоняется: отметка о возврате не должна
    появляться дважды, иначе по журналу не понять, сколько раз возвращали.
    """
    order = await orders_repo.get_for_update(session, order_id)
    if order is None:
        return RefundResult(False, detail="Заказ не найден")
    if order.status == OrderStatus.REFUNDED:
        return RefundResult(False, detail="Возврат по этому заказу уже оформлен")
    if order.status not in REFUNDABLE:
        return RefundResult(False, detail="Возврат возможен только по оплаченному заказу")

    if order.promo_id:
        await promo_service.release(session, order.promo_id, order.id)

    order.status = OrderStatus.REFUNDED
    order.refunded_at = utcnow()
    if comment:
        order.admin_note = comment[:255]
    await session.flush()

    payment_log.info(
        "Оформлен возврат (деньги переводятся вручную)",
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
    return order.status in REFUNDABLE
