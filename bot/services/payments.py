"""Подтверждение оплаты — единственная точка, где заказ становится оплаченным.

Сюда сходятся три пути:

* callback провайдера;
* кнопка «Проверить оплату»;
* фоновый поллер.

Все три обязаны вести именно сюда. Как только появится второй путь выдачи,
он окажется без блокировки, без сверки суммы или без обоих — и разойдётся с
первым молча.

Два правила, из которых выведено остальное:

1. **Тело callback'а не является основанием.** У Platega нет подписи, поэтому
   подтверждением считается только ответ на наш собственный запрос статуса.
2. **Сумма сверяется.** Совпадения идентификатора транзакции мало: подменить
   сумму проще, чем идентификатор.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import utcnow
from bot.db.models import BalanceTxnKind, DeliveryType, Order, OrderKind, OrderStatus
from bot.logger import payment_log
from bot.payments.base import PayStatus, ProviderError
from bot.payments.registry import PaymentRegistry
from bot.repo import balance as balance_repo
from bot.repo import orders as orders_repo
from bot.services import delivery as delivery_service
from bot.services import promo as promo_service


class Outcome:
    DELIVERED = "delivered"          # оплачено и выдано только что
    ALREADY = "already"              # было выдано раньше
    AWAITING = "awaiting"            # оплачено, выдаёт администратор вручную
    TOPPED_UP = "topped_up"          # баланс пополнен
    PENDING = "pending"              # оплата ещё не пришла
    FAILED = "failed"                # провайдер сказал «отменено»
    SHORTAGE = "shortage"            # оплачено, но склада не хватило
    MISMATCH = "mismatch"            # сумма или заказ не сошлись
    CLOSED = "closed"                # заказ уже закрыт (отменён/истёк/возврат)
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"      # провайдер не ответил


@dataclass
class ConfirmResult:
    outcome: str
    order: Order | None = None
    contents: list[str] = field(default_factory=list)
    items: list = field(default_factory=list)
    detail: str | None = None

    @property
    def delivered_now(self) -> bool:
        return self.outcome == Outcome.DELIVERED


# --- выставление счёта ------------------------------------------------------


async def start_payment(
    session: AsyncSession,
    registry: PaymentRegistry,
    order: Order,
    method_code: str,
    username: str | None,
) -> str:
    """Выставляет счёт у провайдера и запоминает его в заказе."""
    provider = registry.provider_of(method_code)
    if provider is None:
        raise ProviderError(f"Способ оплаты недоступен: {method_code}")

    description = (
        f"Пополнение баланса, заказ #{order.id}"
        if order.kind == OrderKind.TOPUP
        else f"{order.product_title} × {order.qty}, заказ #{order.id}"
    )

    invoice = await provider.create_invoice(
        order_id=order.id,
        amount_kop=order.total_kop,
        description=description,
        method_code=method_code,
        user_id=order.user_id,
        username=username,
    )

    order.provider = provider.name
    order.payment_method = method_code
    order.provider_txn_id = invoice.txn_id
    order.pay_url = invoice.pay_url
    order.status = OrderStatus.PENDING
    await session.flush()

    await orders_repo.log_payment(
        session,
        order_id=order.id,
        provider=provider.name,
        provider_txn_id=invoice.txn_id,
        amount_kop=order.total_kop,
        status=PayStatus.PENDING,
        event="invoice_created",
        raw=invoice.raw,
    )
    payment_log.info(
        "Счёт выставлен",
        extra={
            "order_id": order.id,
            "user_id": order.user_id,
            "provider": provider.name,
            "method": method_code,
            "txn_id": invoice.txn_id,
            "amount_kop": order.total_kop,
        },
    )
    return invoice.pay_url


# --- подтверждение ----------------------------------------------------------


async def confirm_order(
    session: AsyncSession,
    registry: PaymentRegistry,
    order_id: int,
    source: str,
) -> ConfirmResult:
    """Проверяет оплату и, если она подтверждена, завершает заказ.

    `source` — откуда пришёл вызов: `callback`, `button`, `poller`. Пишется в
    журнал: без него потом не понять, какой из путей сработал первым.
    """
    order = await orders_repo.get_for_update(session, order_id)
    if order is None:
        return ConfirmResult(Outcome.NOT_FOUND)

    # 1. Уже завершённые заказы дальше не пускаем — это и есть идемпотентность.
    if order.status == OrderStatus.DELIVERED:
        contents = await delivery_service.contents_of(session, order.id)
        return ConfirmResult(Outcome.ALREADY, order=order, contents=contents)
    if order.status == OrderStatus.AWAITING:
        # Оплата уже принята, заказ ждёт администратора. Повторная проверка не
        # должна ни перепроводить платёж, ни трогать статус.
        return ConfirmResult(Outcome.AWAITING, order=order)
    if order.status in (OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REFUNDED):
        return ConfirmResult(Outcome.CLOSED, order=order)

    # 2. Заказ уже оплачен, но выдача не завершилась — повторяем только её.
    if order.status == OrderStatus.PAID:
        return await _finalize(session, order, source)

    if not order.provider or not order.provider_txn_id:
        return ConfirmResult(Outcome.PENDING, order=order)

    provider = registry.get(order.provider)
    if provider is None:
        payment_log.error(
            "Провайдер заказа выключен в настройках",
            extra={"order_id": order.id, "provider": order.provider},
        )
        return ConfirmResult(Outcome.UNAVAILABLE, order=order)

    # 3. Настоящий статус спрашиваем сами. Телу callback'а не верим.
    try:
        status = await provider.fetch_status(order.provider_txn_id)
    except ProviderError as exc:
        payment_log.warning(
            "Не удалось получить статус платежа",
            extra={"order_id": order.id, "provider": order.provider, "error": str(exc)},
        )
        return ConfirmResult(Outcome.UNAVAILABLE, order=order, detail=str(exc))

    await orders_repo.log_payment(
        session,
        order_id=order.id,
        provider=order.provider,
        provider_txn_id=order.provider_txn_id,
        amount_kop=status.amount_kop or 0,
        status=status.status,
        event=f"status_check:{source}",
        raw=status.raw,
    )

    if status.status == PayStatus.PENDING or status.status == PayStatus.UNKNOWN:
        return ConfirmResult(Outcome.PENDING, order=order)

    if status.is_final_failure:
        from bot.services import orders as orders_service

        await orders_service.cancel(session, order, reason="canceled")
        payment_log.info(
            "Платёж отменён провайдером",
            extra={"order_id": order.id, "provider": order.provider, "source": source},
        )
        return ConfirmResult(Outcome.FAILED, order=order)

    # 4. Провайдер говорит «оплачено». Сверяем, что оплачен именно наш заказ
    #    и именно на нашу сумму.
    mismatch = _mismatch_reason(order, status.amount_kop, status.payload)
    if mismatch:
        payment_log.error(
            "Платёж не сошёлся с заказом — выдача остановлена",
            extra={
                "order_id": order.id,
                "provider": order.provider,
                "txn_id": order.provider_txn_id,
                "expected_kop": order.total_kop,
                "got_kop": status.amount_kop,
                "payload": status.payload,
                "reason": mismatch,
                "source": source,
            },
        )
        return ConfirmResult(Outcome.MISMATCH, order=order, detail=mismatch)

    order.status = OrderStatus.PAID
    order.paid_at = utcnow()
    await session.flush()

    payment_log.info(
        "Оплата подтверждена",
        extra={
            "order_id": order.id,
            "user_id": order.user_id,
            "provider": order.provider,
            "txn_id": order.provider_txn_id,
            "amount_kop": order.total_kop,
            "source": source,
        },
    )
    return await _finalize(session, order, source)


def _mismatch_reason(order: Order, amount_kop: int | None, payload: str | None) -> str | None:
    """Причина, по которой платёж нельзя засчитать этому заказу."""
    if payload is not None and str(payload).strip() and str(payload).strip() != str(order.id):
        return f"payload платежа {payload!r} не совпадает с заказом #{order.id}"
    if amount_kop is None:
        # Провайдер не вернул сумму. Заказ найден по нашему же идентификатору
        # транзакции, поэтому подмена суммы здесь маловероятна, но факт
        # фиксируем: молчаливое «сумму не проверяли» опаснее, чем шумное.
        payment_log.warning(
            "Провайдер не вернул сумму платежа",
            extra={"order_id": order.id, "provider": order.provider},
        )
        return None
    if int(amount_kop) != int(order.total_kop):
        return f"сумма {amount_kop} коп. вместо {order.total_kop} коп."
    return None


async def _finalize(session: AsyncSession, order: Order, source: str) -> ConfirmResult:
    """Завершает оплаченный заказ: пополнение баланса или выдача товара."""
    if order.kind == OrderKind.TOPUP:
        await balance_repo.move(
            session,
            user_id=order.user_id,
            amount_kop=order.total_kop,
            kind=BalanceTxnKind.TOPUP,
            order_id=order.id,
            comment=f"Пополнение по заказу #{order.id}",
        )
        order.status = OrderStatus.DELIVERED
        order.delivered_at = utcnow()
        await session.flush()
        payment_log.info(
            "Баланс пополнен",
            extra={"order_id": order.id, "user_id": order.user_id, "amount_kop": order.total_kop},
        )
        return ConfirmResult(Outcome.TOPPED_UP, order=order)

    # Промокод списывается только здесь — после подтверждённой оплаты.
    if order.promo_id:
        consumed = await promo_service.consume(session, order.promo_id, order.user_id, order.id)
        if not consumed:
            # Последнее использование забрал кто-то другой, пока шла оплата.
            # Деньги уже приняты — скидку оставляем, заказ выдаём, но факт
            # записываем: иначе счётчик промокода потом не сойдётся.
            payment_log.warning(
                "Промокод кончился между заказом и оплатой",
                extra={
                    "order_id": order.id,
                    "promo_id": order.promo_id,
                    "promo_code": order.promo_code,
                },
            )

    result = await delivery_service.deliver(session, order)
    if result.shortage:
        return ConfirmResult(Outcome.SHORTAGE, order=order)
    if result.manual:
        return ConfirmResult(Outcome.AWAITING, order=order)
    if not result.ok:
        return ConfirmResult(Outcome.PENDING, order=order)
    return ConfirmResult(
        Outcome.ALREADY if result.already_delivered else Outcome.DELIVERED,
        order=order,
        contents=result.contents,
        items=result.items,
    )


# --- оплата с внутреннего баланса ------------------------------------------


async def pay_with_balance(session: AsyncSession, order_id: int) -> ConfirmResult:
    """Оплата с внутреннего баланса.

    Идёт по тому же сценарию, но подтверждение мгновенное: списание и перевод
    заказа в оплаченный происходят в одной транзакции.
    """
    order = await orders_repo.get_for_update(session, order_id)
    if order is None:
        return ConfirmResult(Outcome.NOT_FOUND)
    if order.status == OrderStatus.DELIVERED:
        contents = await delivery_service.contents_of(session, order.id)
        return ConfirmResult(Outcome.ALREADY, order=order, contents=contents)
    if order.status == OrderStatus.AWAITING:
        return ConfirmResult(Outcome.AWAITING, order=order)
    if order.status not in OrderStatus.OPEN:
        return ConfirmResult(Outcome.CLOSED, order=order)
    if order.kind == OrderKind.TOPUP:
        return ConfirmResult(Outcome.MISMATCH, order=order, detail="Баланс нельзя пополнить с баланса")

    try:
        await balance_repo.move(
            session,
            user_id=order.user_id,
            amount_kop=-order.total_kop,
            kind=BalanceTxnKind.PURCHASE,
            order_id=order.id,
            comment=f"Оплата заказа #{order.id}",
        )
    except balance_repo.InsufficientFunds as exc:
        return ConfirmResult(Outcome.FAILED, order=order, detail=str(exc))

    order.provider = "balance"
    order.payment_method = "balance"
    order.status = OrderStatus.PAID
    order.paid_at = utcnow()
    await session.flush()

    await orders_repo.log_payment(
        session,
        order_id=order.id,
        provider="balance",
        provider_txn_id=None,
        amount_kop=order.total_kop,
        status=PayStatus.CONFIRMED,
        event="balance_charge",
        raw=None,
    )
    payment_log.info(
        "Оплата с баланса",
        extra={"order_id": order.id, "user_id": order.user_id, "amount_kop": order.total_kop},
    )
    return await _finalize(session, order, "balance")
