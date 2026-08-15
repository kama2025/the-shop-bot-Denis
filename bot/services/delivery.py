"""Исполнение заказа.

Единственное место, где оплаченный заказ движется дальше. Три перехода, и у
каждого своё условие:

    оплачен → ждёт реквизиты   `start` — после подтверждения оплаты, выдаёт токен
    ждёт реквизиты → в работе  `accept_credentials` — покупатель прислал логин и пароль
    в работе → выполнен        `confirm_done` — администратор подтвердил

Все три идемпотентны. Подтверждение оплаты приходит тремя путями (callback
провайдера, кнопка «Проверить оплату», фоновый поллер), кнопки в Telegram
нажимаются дважды, а уведомление администратору не должно уходить второй раз.
Вызывающий обязан держать заказ под блокировкой строки
(`orders_repo.get_for_update`) — иначе идемпотентность держится на удаче.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import utcnow
from bot.db.models import Order, OrderStatus
from bot.logger import payment_log
from bot.repo import orders as orders_repo
from bot.services import tokens


@dataclass(frozen=True)
class StepResult:
    ok: bool
    order: Order | None = None
    repeated: bool = False
    """Шаг уже был сделан раньше — состояние не менялось."""


async def start(session: AsyncSession, order: Order) -> StepResult:
    """Оплата подтверждена: выдаём токен и просим реквизиты.

    Токен выдаётся здесь и только здесь — в той же транзакции, что и смена
    статуса. Выдать его раньше значило бы назвать номер заказа, за который
    ещё не заплачено; выдать позже — оставить покупателя без идентификатора
    в промежутке, когда деньги уже списаны.
    """
    if order.status in (OrderStatus.AWAITING_CREDENTIALS, OrderStatus.IN_WORK, OrderStatus.DELIVERED):
        return StepResult(ok=True, order=order, repeated=True)

    if order.status != OrderStatus.PAID:
        return StepResult(ok=False, order=order)

    if not order.token:
        async def exists(candidate: str) -> bool:
            return await orders_repo.token_exists(session, candidate)

        order.token = await tokens.generate_unique(exists)

    order.status = OrderStatus.AWAITING_CREDENTIALS
    await session.flush()

    payment_log.info(
        "Заказ ждёт реквизиты",
        extra={
            "order_id": order.id,
            "user_id": order.user_id,
            "token": order.token,
            "product": order.product_title,
            "total_kop": order.total_kop,
        },
    )
    return StepResult(ok=True, order=order)


async def accept_credentials(
    session: AsyncSession, order: Order, login: str, password: str
) -> StepResult:
    """Покупатель прислал логин и пароль — заказ уходит в работу.

    Пустой логин или пустой пароль не принимаются. Заказ, ушедший к
    администратору с половиной реквизитов, выглядит как готовый к работе, а
    работать по нему нельзя — и выяснится это, когда покупатель уже ждёт.
    """
    login = (login or "").strip()
    password = (password or "").strip()
    if not login or not password:
        return StepResult(ok=False, order=order)

    if order.status in (OrderStatus.IN_WORK, OrderStatus.DELIVERED):
        return StepResult(ok=True, order=order, repeated=True)

    if order.status != OrderStatus.AWAITING_CREDENTIALS:
        return StepResult(ok=False, order=order)

    order.account_login = login
    order.account_password = password
    order.credentials_at = utcnow()
    order.status = OrderStatus.IN_WORK
    await session.flush()

    payment_log.info(
        "Реквизиты получены",
        extra={"order_id": order.id, "user_id": order.user_id, "token": order.token},
    )
    return StepResult(ok=True, order=order)


async def confirm_done(session: AsyncSession, order: Order, admin_id: int) -> StepResult:
    """Администратор подтвердил выполнение.

    Повторное нажатие возвращает `repeated=True` и не трогает заказ: иначе
    покупатель получит второе «заказ выполнен», а в журнале появится второй
    исполнитель.
    """
    if order.status == OrderStatus.DELIVERED:
        return StepResult(ok=True, order=order, repeated=True)

    if order.status != OrderStatus.IN_WORK:
        return StepResult(ok=False, order=order)

    order.status = OrderStatus.DELIVERED
    order.delivered_at = utcnow()
    order.delivered_by = admin_id
    await session.flush()

    payment_log.info(
        "Заказ выполнен",
        extra={
            "order_id": order.id,
            "user_id": order.user_id,
            "token": order.token,
            "admin_id": admin_id,
        },
    )
    return StepResult(ok=True, order=order)


def needs_credentials(order: Order) -> bool:
    """Ждёт ли заказ логин с паролем от покупателя."""
    return order.status == OrderStatus.AWAITING_CREDENTIALS
