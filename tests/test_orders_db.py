"""Заказы, склад, промокоды и баланс — на настоящей MySQL."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from bot.db.models import (
    BalanceTxnKind,
    Order,
    OrderStatus,
    PromoCode,
    StockItem,
    StockStatus,
)
from bot.repo import balance as balance_repo
from bot.repo import promo as promo_repo
from bot.repo import stock as stock_repo
from bot.services import delivery as delivery_service
from bot.services import orders as orders_service
from bot.utils.money import DISCOUNT_PERCENT
from tests.factories import fill_stock, make_product, make_user

pytestmark = pytest.mark.db


async def test_order_reserves_exactly_requested(session_factory) -> None:
    async with session_factory() as session:
        user = await make_user(session)
        product = await make_product(session, price_kop=9000)
        await fill_stock(session, product, 5)
        await session.commit()

        order = await orders_service.create_order(
            session, user.tg_id, product, qty=2, promo=None, reserve_minutes=20, max_qty=10
        )
        await session.commit()

        assert order.total_kop == 18000
        assert await stock_repo.available_count(session, product.id) == 3
        reserved = await stock_repo.items_of_order(session, order.id)
        assert len(reserved) == 2
        assert all(item.status == StockStatus.RESERVED for item in reserved)
        assert all(item.reserved_until is not None for item in reserved)


async def test_order_refuses_when_stock_is_short(session_factory) -> None:
    async with session_factory() as session:
        user = await make_user(session)
        product = await make_product(session)
        await fill_stock(session, product, 1)
        await session.commit()
        # Снимаем идентификатор заранее: после rollback объекты ORM
        # просрочены, и обращение к атрибуту уходит в ленивую подгрузку.
        product_id = product.id

        with pytest.raises(orders_service.OutOfStock) as exc:
            await orders_service.create_order(
                session, user.tg_id, product, qty=3, promo=None, reserve_minutes=20, max_qty=10
            )
        assert exc.value.available == 1

        # Частичного резерва не осталось.
        await session.rollback()
        assert await stock_repo.available_count(session, product_id) == 1
        orders_count = (await session.execute(select(func.count(Order.id)))).scalar_one()
        assert orders_count == 0, "отказ оставил заказ в базе"


async def test_two_buyers_never_get_the_same_item(session_factory) -> None:
    """Главное свойство магазина.

    На складе одна позиция, два покупателя жмут «Купить» одновременно. Ровно
    один должен получить заказ, второй — отказ. Без `FOR UPDATE SKIP LOCKED`
    оба получают одну и ту же позицию, и это выясняется из жалоб.
    """
    async with session_factory() as session:
        buyer_a = await make_user(session, tg_id=2001)
        buyer_b = await make_user(session, tg_id=2002)
        product = await make_product(session)
        await fill_stock(session, product, 1)
        await session.commit()
        product_id = product.id
        ids = (buyer_a.tg_id, buyer_b.tg_id)

    async def buy(user_id: int) -> str:
        async with session_factory() as session:
            from bot.repo import catalog as catalog_repo

            fresh = await catalog_repo.get_product(session, product_id)
            try:
                await orders_service.create_order(
                    session, user_id, fresh, qty=1, promo=None, reserve_minutes=20, max_qty=10
                )
                await session.commit()
                return "ok"
            except orders_service.OutOfStock:
                await session.rollback()
                return "out"

    results = await asyncio.gather(buy(ids[0]), buy(ids[1]))
    assert sorted(results) == ["ok", "out"], f"получили {results}"

    async with session_factory() as session:
        orders = (await session.execute(select(func.count(Order.id)))).scalar_one()
        reserved = (
            await session.execute(
                select(func.count(StockItem.id)).where(StockItem.status == StockStatus.RESERVED)
            )
        ).scalar_one()
        assert orders == 1
        assert reserved == 1


async def test_reserve_never_leaves_partial_hold(session_factory) -> None:
    """Внутренний слой резерва — с выключенным соседом.

    Через покупку эту защиту не увидеть: `create_order` проверяет тот же
    случай сам и откатывает частичный резерв, поэтому итог одинаков с
    проверкой и без неё. Мутационный прогон это и показал — мутация в
    `reserve_items` выжила на тестах покупки.

    Защиту в глубину проверяют с выключенными соседями: зовём репозиторий
    напрямую и требуем, чтобы при нехватке он не только вернул пустой список,
    но и **ничего не пометил зарезервированным**. Иначе позиции повиснут
    в резерве за заказом, которого не будет.
    """
    from datetime import timedelta

    from bot.db.base import utcnow

    async with session_factory() as session:
        user = await make_user(session)
        product = await make_product(session)
        await fill_stock(session, product, 2)
        await session.commit()

        held = await stock_repo.reserve_items(
            session,
            product_id=product.id,
            qty=3,               # просим больше, чем есть
            order_id=999999,
            reserved_until=utcnow() + timedelta(minutes=20),
        )
        await session.flush()

        assert held == [], "частичный резерв не должен возвращаться"

        counts = await stock_repo.counts_by_status(session, product.id)
        assert counts[StockStatus.RESERVED] == 0, "позиции повисли в резерве"
        assert counts[StockStatus.AVAILABLE] == 2
        assert await stock_repo.available_count(session, product.id) == 2

        # И ровный случай: сколько просили, столько и захватили.
        exact = await stock_repo.reserve_items(
            session,
            product_id=product.id,
            qty=2,
            order_id=999999,
            reserved_until=utcnow() + timedelta(minutes=20),
        )
        await session.flush()
        assert len(exact) == 2
        assert await stock_repo.available_count(session, product.id) == 0


async def test_delivery_is_idempotent(session_factory) -> None:
    """Три источника подтверждения не должны выдать товар трижды."""
    async with session_factory() as session:
        user = await make_user(session)
        product = await make_product(session)
        await fill_stock(session, product, 3)
        await session.commit()

        order = await orders_service.create_order(
            session, user.tg_id, product, qty=2, promo=None, reserve_minutes=20, max_qty=10
        )
        order.status = OrderStatus.PAID
        await session.commit()

        first = await delivery_service.deliver(session, order)
        await session.commit()
        assert first.ok is True
        assert first.already_delivered is False
        assert len(first.contents) == 2

        for _ in range(3):
            again = await delivery_service.deliver(session, order)
            await session.commit()
            assert again.ok is True
            assert again.already_delivered is True
            assert again.contents == first.contents

        sold = (
            await session.execute(
                select(func.count(StockItem.id)).where(StockItem.status == StockStatus.SOLD)
            )
        ).scalar_one()
        assert sold == 2, "выдали больше позиций, чем в заказе"

        from bot.repo import orders as orders_repo

        rows = await orders_repo.items_of(session, order.id)
        assert len(rows) == 2, "позиции заказа задвоились"


async def test_expired_reserve_returns_stock(session_factory) -> None:
    async with session_factory() as session:
        user = await make_user(session)
        product = await make_product(session)
        await fill_stock(session, product, 2)
        await session.commit()

        order = await orders_service.create_order(
            session, user.tg_id, product, qty=2, promo=None, reserve_minutes=20, max_qty=10
        )
        await session.commit()
        assert await stock_repo.available_count(session, product.id) == 0

        # Отматываем срок назад — так же, как сделает время.
        from bot.db.base import utcnow
        from datetime import timedelta

        past = utcnow() - timedelta(minutes=1)
        order.reserve_expires_at = past
        for item in await stock_repo.items_of_order(session, order.id):
            item.reserved_until = past
        await session.commit()

        expired = await orders_service.expire_stale(session)
        await session.commit()

        assert order.id in expired
        await session.refresh(order)
        assert order.status == OrderStatus.EXPIRED
        assert await stock_repo.available_count(session, product.id) == 2


async def test_promo_last_use_goes_to_one_order_only(session_factory) -> None:
    """Последнее использование промокода не должно достаться двоим.

    Проверка «прочитать счётчик, потом записать» здесь неверна: между чтением
    и записью помещается чужая покупка.
    """
    async with session_factory() as session:
        promo = PromoCode(
            code="LAST1", discount_type=DISCOUNT_PERCENT, discount_value=10, usage_limit=1
        )
        session.add(promo)
        users = [await make_user(session, tg_id=3000 + i) for i in range(2)]
        product = await make_product(session)
        await fill_stock(session, product, 2)
        await session.flush()

        orders = []
        for user in users:
            order = await orders_service.create_order(
                session, user.tg_id, product, qty=1, promo=promo, reserve_minutes=20, max_qty=10
            )
            orders.append(order)
        await session.commit()
        promo_id = promo.id
        pairs = [(order.id, order.user_id) for order in orders]

    async def consume(order_id: int, user_id: int) -> bool:
        async with session_factory() as session:
            ok = await promo_repo.consume(session, promo_id, user_id, order_id)
            await session.commit()
            return ok

    results = await asyncio.gather(*(consume(oid, uid) for oid, uid in pairs))
    assert sorted(results) == [False, True], f"получили {results}"

    async with session_factory() as session:
        promo = await promo_repo.get(session, promo_id)
        assert promo.used_count == 1
        assert await promo_repo.uses_total(session, promo_id) == 1


async def test_promo_consume_is_idempotent_per_order(session_factory) -> None:
    """Повторное подтверждение одного заказа не списывает промокод дважды."""
    async with session_factory() as session:
        promo = PromoCode(
            code="TWICE", discount_type=DISCOUNT_PERCENT, discount_value=10, usage_limit=5
        )
        session.add(promo)
        user = await make_user(session)
        product = await make_product(session)
        await fill_stock(session, product, 1)
        await session.flush()
        order = await orders_service.create_order(
            session, user.tg_id, product, qty=1, promo=promo, reserve_minutes=20, max_qty=10
        )
        await session.commit()

        for _ in range(3):
            assert await promo_repo.consume(session, promo.id, user.tg_id, order.id) is True
            await session.commit()

        await session.refresh(promo)
        assert promo.used_count == 1
        assert await promo_repo.uses_total(session, promo.id) == 1


async def test_balance_ledger_matches_cached_field(session_factory) -> None:
    async with session_factory() as session:
        user = await make_user(session, balance_kop=0)
        await session.commit()

        await balance_repo.move(session, user.tg_id, 50000, BalanceTxnKind.TOPUP)
        await balance_repo.move(session, user.tg_id, -18000, BalanceTxnKind.PURCHASE)
        await balance_repo.move(session, user.tg_id, 18000, BalanceTxnKind.REFUND)
        await session.commit()

        await session.refresh(user)
        assert user.balance_kop == 50000
        assert await balance_repo.ledger_balance(session, user.tg_id) == 50000
        assert await balance_repo.find_mismatches(session) == []


async def test_balance_cannot_go_negative(session_factory) -> None:
    async with session_factory() as session:
        user = await make_user(session, balance_kop=1000)
        await session.commit()

        with pytest.raises(balance_repo.InsufficientFunds):
            await balance_repo.move(session, user.tg_id, -5000, BalanceTxnKind.PURCHASE)
        await session.rollback()

        await session.refresh(user)
        assert user.balance_kop == 1000


async def test_parallel_spending_cannot_overdraw(session_factory) -> None:
    """Два одновременных списания не должны увести баланс в минус."""
    async with session_factory() as session:
        user = await make_user(session, balance_kop=10000)
        await session.commit()
        user_id = user.tg_id

    async def spend() -> bool:
        async with session_factory() as session:
            try:
                await balance_repo.move(session, user_id, -8000, BalanceTxnKind.PURCHASE)
                await session.commit()
                return True
            except balance_repo.InsufficientFunds:
                await session.rollback()
                return False

    results = await asyncio.gather(spend(), spend())
    assert sorted(results) == [False, True], f"получили {results}"

    async with session_factory() as session:
        assert await balance_repo.ledger_balance(session, user_id) == 2000
