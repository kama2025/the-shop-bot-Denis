"""Гарантия: возврат на баланс, замена товара, брак партии."""

from __future__ import annotations

import pytest

from bot.db.models import OrderStatus, PromoCode, StockStatus
from bot.repo import balance as balance_repo
from bot.repo import promo as promo_repo
from bot.repo import stock as stock_repo
from bot.services import delivery as delivery_service
from bot.services import orders as orders_service
from bot.services import refunds as refunds_service
from bot.utils.money import DISCOUNT_PERCENT
from tests.factories import fill_stock, make_product, make_user

pytestmark = pytest.mark.db


async def _delivered_order(session, qty: int = 1, price_kop: int = 9000, stock: int = 5):
    user = await make_user(session)
    product = await make_product(session, price_kop=price_kop)
    await fill_stock(session, product, stock)
    await session.commit()

    order = await orders_service.create_order(
        session, user.tg_id, product, qty=qty, promo=None, reserve_minutes=20, max_qty=10
    )
    order.status = OrderStatus.PAID
    await session.commit()
    await delivery_service.deliver(session, order)
    await session.commit()
    return order, product, user


async def test_refund_credits_balance_and_closes_order(session_factory) -> None:
    async with session_factory() as session:
        order, product, user = await _delivered_order(session, qty=2)

        result = await refunds_service.refund_to_balance(session, order.id, admin_id=1, comment="брак")
        await session.commit()

        assert result.ok is True
        assert result.amount_kop == order.total_kop

        await session.refresh(order)
        await session.refresh(user)
        assert order.status == OrderStatus.REFUNDED
        assert user.balance_kop == order.total_kop
        assert await balance_repo.ledger_balance(session, user.tg_id) == order.total_kop

        # Выданные позиции нельзя продать повторно.
        counts = await stock_repo.counts_by_status(session, product.id)
        assert counts[StockStatus.SOLD] == 0
        assert counts[StockStatus.DEFECTIVE] == 2


async def test_refund_is_not_repeatable(session_factory) -> None:
    """Двойной возврат — прямая потеря денег."""
    async with session_factory() as session:
        order, _, user = await _delivered_order(session)

        first = await refunds_service.refund_to_balance(session, order.id, admin_id=1)
        await session.commit()
        assert first.ok is True

        second = await refunds_service.refund_to_balance(session, order.id, admin_id=1)
        await session.commit()
        assert second.ok is False
        assert "уже" in (second.detail or "")

        await session.refresh(user)
        assert user.balance_kop == order.total_kop


async def test_refund_returns_promo_use(session_factory) -> None:
    """Возврат обязан вернуть и промокод: иначе клиент теряет и товар, и скидку."""
    async with session_factory() as session:
        promo = PromoCode(
            code="BACK10", discount_type=DISCOUNT_PERCENT, discount_value=10, usage_limit=5
        )
        session.add(promo)
        user = await make_user(session)
        product = await make_product(session, price_kop=10000)
        await fill_stock(session, product, 3)
        await session.flush()

        order = await orders_service.create_order(
            session, user.tg_id, product, qty=1, promo=promo, reserve_minutes=20, max_qty=10
        )
        order.status = OrderStatus.PAID
        await session.commit()

        await promo_repo.consume(session, promo.id, user.tg_id, order.id)
        await delivery_service.deliver(session, order)
        await session.commit()
        await session.refresh(promo)
        assert promo.used_count == 1

        await refunds_service.refund_to_balance(session, order.id, admin_id=1)
        await session.commit()

        await session.refresh(promo)
        assert promo.used_count == 0
        assert await promo_repo.uses_total(session, promo.id) == 0


async def test_replace_gives_new_items(session_factory) -> None:
    async with session_factory() as session:
        order, product, _ = await _delivered_order(session, qty=1, stock=3)
        old = await delivery_service.contents_of(session, order.id)

        result = await refunds_service.replace_items(session, order.id, admin_id=1, reason="не работает")
        await session.commit()

        assert result.ok is True
        assert result.contents is not None
        assert set(result.contents).isdisjoint(set(old)), "выдали ту же позицию"

        counts = await stock_repo.counts_by_status(session, product.id)
        assert counts[StockStatus.DEFECTIVE] == 1
        assert counts[StockStatus.SOLD] == 1
        assert counts[StockStatus.AVAILABLE] == 1


async def test_replace_refuses_without_stock(session_factory) -> None:
    async with session_factory() as session:
        order, _, _ = await _delivered_order(session, qty=1, stock=1)

        result = await refunds_service.replace_items(session, order.id, admin_id=1, reason="нет")
        assert result.ok is False
        assert "склад" in (result.detail or "").lower()


async def test_reject_batch_spares_sold_items(session_factory) -> None:
    """Брак партии снимает с продажи свободное, но не переписывает проданное."""
    async with session_factory() as session:
        user = await make_user(session)
        product = await make_product(session)
        batch = await stock_repo.add_batch(
            session, product.id, [f"поз-{i}" for i in range(5)], admin_id=1
        )
        await session.commit()

        order = await orders_service.create_order(
            session, user.tg_id, product, qty=1, promo=None, reserve_minutes=20, max_qty=10
        )
        order.status = OrderStatus.PAID
        await session.commit()
        await delivery_service.deliver(session, order)
        await session.commit()

        affected = await refunds_service.reject_batch(session, batch.id, admin_id=1, reason="мертвяк")
        await session.commit()

        assert affected == 4
        counts = await stock_repo.counts_by_status(session, product.id)
        assert counts[StockStatus.DEFECTIVE] == 4
        assert counts[StockStatus.SOLD] == 1
        assert counts[StockStatus.AVAILABLE] == 0


async def test_refund_refuses_unpaid_order(session_factory) -> None:
    async with session_factory() as session:
        user = await make_user(session)
        product = await make_product(session)
        await fill_stock(session, product, 2)
        await session.commit()
        order = await orders_service.create_order(
            session, user.tg_id, product, qty=1, promo=None, reserve_minutes=20, max_qty=10
        )
        await session.commit()

        result = await refunds_service.refund_to_balance(session, order.id, admin_id=1)
        assert result.ok is False
