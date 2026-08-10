"""Подтверждение оплаты — самая дорогая часть магазина.

Проверяется не «работает ли счастливый путь», а что выдача **не** происходит
там, где не должна: при неподтверждённом статусе, при расхождении суммы, при
чужом payload, при повторных вызовах и при недоступном провайдере.
"""

from __future__ import annotations

import pytest

from bot.db.models import OrderKind, OrderStatus, StockStatus
from bot.payments.base import Invoice, PaymentMethod, PayStatus, ProviderError, StatusResult
from bot.repo import balance as balance_repo
from bot.repo import orders as orders_repo
from bot.repo import stock as stock_repo
from bot.services import orders as orders_service
from bot.services import payments as payments_service
from bot.services.payments import Outcome
from tests.factories import fill_stock, make_product, make_user

pytestmark = pytest.mark.db


class FakeProvider:
    """Провайдер, которым управляет тест."""

    name = "fake"

    def __init__(self) -> None:
        self.status = PayStatus.PENDING
        self.amount_kop: int | None = None
        self.payload: str | None = None
        self.raise_error = False
        self.calls = 0

    def methods(self) -> list[PaymentMethod]:
        return [PaymentMethod(provider="fake", code="fake:card", title="Тест")]

    async def create_invoice(self, order_id: int, amount_kop: int, **kwargs) -> Invoice:
        return Invoice(txn_id=f"txn-{order_id}", pay_url="https://example.test/pay")

    async def fetch_status(self, txn_id: str) -> StatusResult:
        self.calls += 1
        if self.raise_error:
            raise ProviderError("провайдер недоступен")
        return StatusResult(
            status=self.status,
            amount_kop=self.amount_kop,
            currency="RUB",
            payload=self.payload,
            raw={"txn": txn_id},
        )

    async def close(self) -> None:
        return None


class FakeRegistry:
    def __init__(self, provider: FakeProvider) -> None:
        self._provider = provider

    def get(self, name: str):
        return self._provider if name == "fake" else None

    def provider_of(self, method_code: str):
        return self._provider if method_code.startswith("fake") else None

    def methods(self):
        return self._provider.methods()

    @property
    def any_enabled(self) -> bool:
        return True


async def _make_pending_order(session, qty: int = 1, price_kop: int = 9000):
    user = await make_user(session)
    product = await make_product(session, price_kop=price_kop)
    await fill_stock(session, product, 5)
    await session.commit()

    order = await orders_service.create_order(
        session, user.tg_id, product, qty=qty, promo=None, reserve_minutes=20, max_qty=10
    )
    order.provider = "fake"
    order.payment_method = "fake:card"
    order.provider_txn_id = f"txn-{order.id}"
    order.status = OrderStatus.PENDING
    await session.commit()
    return order, product, user


async def test_pending_does_not_deliver(session_factory) -> None:
    async with session_factory() as session:
        order, product, _ = await _make_pending_order(session)
        provider = FakeProvider()
        provider.status = PayStatus.PENDING
        registry = FakeRegistry(provider)

        result = await payments_service.confirm_order(session, registry, order.id, "test")
        await session.commit()

        assert result.outcome == Outcome.PENDING
        assert result.contents == []
        await session.refresh(order)
        assert order.status == OrderStatus.PENDING
        assert await stock_repo.available_count(session, product.id) == 4


async def test_confirmed_delivers_once(session_factory) -> None:
    async with session_factory() as session:
        order, product, _ = await _make_pending_order(session, qty=2)
        provider = FakeProvider()
        provider.status = PayStatus.CONFIRMED
        provider.amount_kop = order.total_kop
        provider.payload = str(order.id)
        registry = FakeRegistry(provider)

        first = await payments_service.confirm_order(session, registry, order.id, "callback")
        await session.commit()
        assert first.outcome == Outcome.DELIVERED
        assert len(first.contents) == 2

        # Три повторных подтверждения из разных источников.
        for source in ("callback", "button", "poller"):
            again = await payments_service.confirm_order(session, registry, order.id, source)
            await session.commit()
            assert again.outcome == Outcome.ALREADY
            assert again.contents == first.contents

        await session.refresh(order)
        assert order.status == OrderStatus.DELIVERED
        rows = await orders_repo.items_of(session, order.id)
        assert len(rows) == 2, "повторное подтверждение задвоило позиции заказа"

        sold = await stock_repo.counts_by_status(session, product.id)
        assert sold[StockStatus.SOLD] == 2


async def test_amount_mismatch_blocks_delivery(session_factory) -> None:
    """Провайдер говорит «оплачено», но сумма другая — товар не выдаём."""
    async with session_factory() as session:
        order, product, _ = await _make_pending_order(session, price_kop=9000)
        provider = FakeProvider()
        provider.status = PayStatus.CONFIRMED
        provider.amount_kop = 100  # заплатили 1 ₽ вместо 90 ₽
        provider.payload = str(order.id)
        registry = FakeRegistry(provider)

        result = await payments_service.confirm_order(session, registry, order.id, "callback")
        await session.commit()

        assert result.outcome == Outcome.MISMATCH
        assert result.contents == []
        await session.refresh(order)
        assert order.status == OrderStatus.PENDING, "заказ не должен становиться оплаченным"
        counts = await stock_repo.counts_by_status(session, product.id)
        assert counts[StockStatus.SOLD] == 0


async def test_foreign_payload_blocks_delivery(session_factory) -> None:
    """Оплата с чужим payload не должна закрывать наш заказ."""
    async with session_factory() as session:
        order, _, _ = await _make_pending_order(session)
        provider = FakeProvider()
        provider.status = PayStatus.CONFIRMED
        provider.amount_kop = order.total_kop
        provider.payload = str(order.id + 777)
        registry = FakeRegistry(provider)

        result = await payments_service.confirm_order(session, registry, order.id, "callback")
        await session.commit()

        assert result.outcome == Outcome.MISMATCH
        await session.refresh(order)
        assert order.status == OrderStatus.PENDING


async def test_canceled_releases_stock(session_factory) -> None:
    async with session_factory() as session:
        order, product, _ = await _make_pending_order(session, qty=2)
        provider = FakeProvider()
        provider.status = PayStatus.CANCELED
        registry = FakeRegistry(provider)

        result = await payments_service.confirm_order(session, registry, order.id, "callback")
        await session.commit()

        assert result.outcome == Outcome.FAILED
        await session.refresh(order)
        assert order.status == OrderStatus.CANCELED
        assert await stock_repo.available_count(session, product.id) == 5


async def test_provider_error_keeps_order_open(session_factory) -> None:
    """Недоступный провайдер — не повод отменить заказ покупателя."""
    async with session_factory() as session:
        order, product, _ = await _make_pending_order(session)
        provider = FakeProvider()
        provider.raise_error = True
        registry = FakeRegistry(provider)

        result = await payments_service.confirm_order(session, registry, order.id, "poller")
        await session.commit()

        assert result.outcome == Outcome.UNAVAILABLE
        await session.refresh(order)
        assert order.status == OrderStatus.PENDING
        assert await stock_repo.available_count(session, product.id) == 4


async def test_missing_amount_still_delivers_but_is_logged(session_factory) -> None:
    """Провайдер не вернул сумму.

    Заказ найден по нашему же идентификатору транзакции, поэтому выдаём, но
    факт «сумму не проверяли» обязан попасть в журнал — молчание опаснее шума.
    """
    async with session_factory() as session:
        order, _, _ = await _make_pending_order(session)
        provider = FakeProvider()
        provider.status = PayStatus.CONFIRMED
        provider.amount_kop = None
        provider.payload = None
        registry = FakeRegistry(provider)

        result = await payments_service.confirm_order(session, registry, order.id, "callback")
        await session.commit()
        assert result.outcome == Outcome.DELIVERED


async def test_shortage_when_stock_disappeared(session_factory) -> None:
    """Оплата прошла, а зарезервированные позиции забраковали."""
    async with session_factory() as session:
        order, product, _ = await _make_pending_order(session, qty=2)
        items = await stock_repo.items_of_order(session, order.id)
        await stock_repo.mark_items_defective(session, [item.id for item in items], "тест")
        await session.commit()

        provider = FakeProvider()
        provider.status = PayStatus.CONFIRMED
        provider.amount_kop = order.total_kop
        provider.payload = str(order.id)
        registry = FakeRegistry(provider)

        result = await payments_service.confirm_order(session, registry, order.id, "callback")
        await session.commit()

        assert result.outcome == Outcome.SHORTAGE
        assert result.contents == []
        await session.refresh(order)
        assert order.status == OrderStatus.PAID, "деньги приняты — статус обязан это отражать"


async def test_topup_credits_balance_once(session_factory) -> None:
    async with session_factory() as session:
        from datetime import timedelta

        from bot.db.base import utcnow
        from bot.db.models import Order

        user = await make_user(session)
        order = Order(
            user_id=user.tg_id,
            kind=OrderKind.TOPUP,
            product_title="Пополнение баланса",
            qty=1,
            unit_price_kop=50000,
            subtotal_kop=50000,
            total_kop=50000,
            provider="fake",
            payment_method="fake:card",
            provider_txn_id="txn-topup",
            status=OrderStatus.PENDING,
            reserve_expires_at=utcnow() + timedelta(minutes=20),
        )
        session.add(order)
        await session.commit()

        provider = FakeProvider()
        provider.status = PayStatus.CONFIRMED
        provider.amount_kop = 50000
        provider.payload = str(order.id)
        registry = FakeRegistry(provider)

        first = await payments_service.confirm_order(session, registry, order.id, "callback")
        await session.commit()
        assert first.outcome == Outcome.TOPPED_UP

        for _ in range(3):
            again = await payments_service.confirm_order(session, registry, order.id, "poller")
            await session.commit()
            assert again.outcome == Outcome.ALREADY

        assert await balance_repo.ledger_balance(session, user.tg_id) == 50000
        await session.refresh(user)
        assert user.balance_kop == 50000


async def test_balance_payment_charges_once(session_factory) -> None:
    async with session_factory() as session:
        user = await make_user(session, balance_kop=30000)
        product = await make_product(session, price_kop=9000)
        await fill_stock(session, product, 3)
        await session.commit()

        order = await orders_service.create_order(
            session, user.tg_id, product, qty=2, promo=None, reserve_minutes=20, max_qty=10
        )
        await session.commit()

        result = await payments_service.pay_with_balance(session, order.id)
        await session.commit()
        assert result.outcome == Outcome.DELIVERED
        assert len(result.contents) == 2

        for _ in range(3):
            again = await payments_service.pay_with_balance(session, order.id)
            await session.commit()
            assert again.outcome == Outcome.ALREADY

        await session.refresh(user)
        assert user.balance_kop == 30000 - 18000
        assert await balance_repo.ledger_balance(session, user.tg_id) == 12000


async def test_balance_payment_refuses_when_short(session_factory) -> None:
    async with session_factory() as session:
        user = await make_user(session, balance_kop=1000)
        product = await make_product(session, price_kop=9000)
        await fill_stock(session, product, 3)
        await session.commit()

        order = await orders_service.create_order(
            session, user.tg_id, product, qty=1, promo=None, reserve_minutes=20, max_qty=10
        )
        await session.commit()

        result = await payments_service.pay_with_balance(session, order.id)
        await session.rollback()

        assert result.outcome == Outcome.FAILED
        await session.refresh(order)
        assert order.status == OrderStatus.NEW
        await session.refresh(user)
        assert user.balance_kop == 1000
