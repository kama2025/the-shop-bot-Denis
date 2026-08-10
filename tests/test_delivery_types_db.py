"""Три типа выдачи: текст со склада, файл со склада, ручная выдача."""

from __future__ import annotations

import pytest

from bot.db.models import DeliveryType, OrderStatus, StockItem, StockStatus
from bot.repo import catalog as catalog_repo
from bot.repo import stock as stock_repo
from bot.services import delivery as delivery_service
from bot.services import orders as orders_service
from bot.services import payments as payments_service
from bot.services import refunds as refunds_service
from bot.services.payments import Outcome
from tests.factories import fill_stock, make_category, make_product, make_user
from tests.test_payments_db import FakeProvider, FakeRegistry

pytestmark = pytest.mark.db


async def _manual_product(session, price_kop: int = 50000):
    category = await make_category(session)
    return await catalog_repo.create_product(
        session,
        category_id=category.id,
        title="Настройка под ключ",
        description="Делаем руками",
        price_kop=price_kop,
        delivery_type=DeliveryType.MANUAL,
    )


async def _file_product(session, files: int = 2):
    category = await make_category(session)
    product = await catalog_repo.create_product(
        session,
        category_id=category.id,
        title="Архив с материалами",
        description=None,
        price_kop=30000,
        delivery_type=DeliveryType.FILE,
    )
    session.add_all(
        StockItem(
            product_id=product.id,
            content=f"подпись {i}",
            file_id=f"BQACAgIAAxkBAAI-file-{i}",
            file_kind="document",
            file_name=f"materials-{i}.zip",
        )
        for i in range(files)
    )
    await session.flush()
    return product


# --- ручная выдача ----------------------------------------------------------


async def test_manual_product_is_always_buyable(session_factory) -> None:
    """У товара с ручной выдачей склада нет, но купить его можно всегда."""
    async with session_factory() as session:
        product = await _manual_product(session)
        await session.commit()

        assert await stock_repo.available_count(session, product.id) > 0
        counts = await catalog_repo.stock_counts(session, [product.id])
        assert counts[product.id] > 0


async def test_manual_order_reserves_nothing(session_factory) -> None:
    async with session_factory() as session:
        user = await make_user(session)
        product = await _manual_product(session)
        await session.commit()

        order = await orders_service.create_order(
            session, user.tg_id, product, qty=2, promo=None, reserve_minutes=20, max_qty=10
        )
        await session.commit()

        assert order.delivery_type == DeliveryType.MANUAL
        assert order.total_kop == 100000
        assert await stock_repo.items_of_order(session, order.id) == []


async def test_manual_order_waits_for_admin_after_payment(session_factory) -> None:
    """Оплаченный заказ не считается выданным, пока админ его не закрыл.

    Пометить такой заказ выданным сразу значит потерять его среди завершённых,
    а покупателя оставить без товара.
    """
    async with session_factory() as session:
        user = await make_user(session)
        product = await _manual_product(session)
        await session.commit()

        order = await orders_service.create_order(
            session, user.tg_id, product, qty=1, promo=None, reserve_minutes=20, max_qty=10
        )
        order.provider = "fake"
        order.payment_method = "fake:card"
        order.provider_txn_id = f"txn-{order.id}"
        order.status = OrderStatus.PENDING
        await session.commit()

        provider = FakeProvider()
        provider.status = "confirmed"
        provider.amount_kop = order.total_kop
        provider.payload = str(order.id)
        registry = FakeRegistry(provider)

        result = await payments_service.confirm_order(session, registry, order.id, "callback")
        await session.commit()

        assert result.outcome == Outcome.AWAITING
        await session.refresh(order)
        assert order.status == OrderStatus.AWAITING
        assert order.paid_at is not None
        assert order.delivered_at is None


async def test_repeated_check_does_not_reprocess_manual_order(session_factory) -> None:
    async with session_factory() as session:
        user = await make_user(session)
        product = await _manual_product(session)
        await session.commit()

        order = await orders_service.create_order(
            session, user.tg_id, product, qty=1, promo=None, reserve_minutes=20, max_qty=10
        )
        order.provider = "fake"
        order.provider_txn_id = f"txn-{order.id}"
        order.status = OrderStatus.PENDING
        await session.commit()

        provider = FakeProvider()
        provider.status = "confirmed"
        provider.amount_kop = order.total_kop
        provider.payload = str(order.id)
        registry = FakeRegistry(provider)

        await payments_service.confirm_order(session, registry, order.id, "callback")
        await session.commit()
        calls_after_first = provider.calls

        for source in ("button", "poller", "callback"):
            again = await payments_service.confirm_order(session, registry, order.id, source)
            await session.commit()
            assert again.outcome == Outcome.AWAITING

        assert provider.calls == calls_after_first, "лишние обращения к провайдеру"
        await session.refresh(order)
        assert order.status == OrderStatus.AWAITING


async def test_admin_closes_manual_order_and_it_is_recorded(session_factory) -> None:
    """Отправленное админом сохраняется — «что выдали» видно и через месяц."""
    async with session_factory() as session:
        user = await make_user(session)
        product = await _manual_product(session)
        await session.commit()

        order = await orders_service.create_order(
            session, user.tg_id, product, qty=1, promo=None, reserve_minutes=20, max_qty=10
        )
        order.status = OrderStatus.AWAITING
        await session.commit()

        payload = delivery_service.DeliveredItem(content="Доступ выдан, логин: demo")
        result = await delivery_service.complete_manual(session, order, admin_id=1, item=payload)
        await session.commit()

        assert result.ok is True
        assert result.items[0].content == "Доступ выдан, логин: demo"

        await session.refresh(order)
        assert order.status == OrderStatus.DELIVERED
        assert order.delivered_at is not None

        stored = await delivery_service.items_of(session, order.id)
        assert [item.content for item in stored] == ["Доступ выдан, логин: demo"]


async def test_manual_completion_is_idempotent(session_factory) -> None:
    async with session_factory() as session:
        user = await make_user(session)
        product = await _manual_product(session)
        await session.commit()
        order = await orders_service.create_order(
            session, user.tg_id, product, qty=1, promo=None, reserve_minutes=20, max_qty=10
        )
        order.status = OrderStatus.AWAITING
        await session.commit()

        first = delivery_service.DeliveredItem(content="первое")
        await delivery_service.complete_manual(session, order, 1, first)
        await session.commit()

        second = delivery_service.DeliveredItem(content="второе")
        again = await delivery_service.complete_manual(session, order, 1, second)
        await session.commit()

        assert again.already_delivered is True
        stored = await delivery_service.items_of(session, order.id)
        assert [item.content for item in stored] == ["первое"], "выдали дважды"


async def test_awaiting_order_can_be_refunded(session_factory) -> None:
    """Договориться не вышло — деньги обязаны вернуться."""
    async with session_factory() as session:
        user = await make_user(session)
        product = await _manual_product(session)
        await session.commit()
        order = await orders_service.create_order(
            session, user.tg_id, product, qty=1, promo=None, reserve_minutes=20, max_qty=10
        )
        order.status = OrderStatus.AWAITING
        await session.commit()

        assert refunds_service.order_can_be_refunded(order) is True
        result = await refunds_service.refund_to_balance(session, order.id, admin_id=1)
        await session.commit()

        assert result.ok is True
        await session.refresh(user)
        assert user.balance_kop == order.total_kop


# --- файловые товары --------------------------------------------------------


async def test_file_product_delivers_file_ids(session_factory) -> None:
    async with session_factory() as session:
        user = await make_user(session)
        product = await _file_product(session, files=3)
        await session.commit()

        order = await orders_service.create_order(
            session, user.tg_id, product, qty=2, promo=None, reserve_minutes=20, max_qty=10
        )
        order.status = OrderStatus.PAID
        await session.commit()

        result = await delivery_service.deliver(session, order)
        await session.commit()

        assert result.ok is True
        assert len(result.items) == 2
        assert all(item.is_file for item in result.items)
        assert all(item.file_name.endswith(".zip") for item in result.items)
        assert await stock_repo.available_count(session, product.id) == 1


async def test_file_product_needs_stock_like_any_other(session_factory) -> None:
    async with session_factory() as session:
        user = await make_user(session)
        product = await _file_product(session, files=1)
        await session.commit()

        with pytest.raises(orders_service.OutOfStock):
            await orders_service.create_order(
                session, user.tg_id, product, qty=2, promo=None, reserve_minutes=20, max_qty=10
            )


async def test_delivery_type_is_snapshotted_on_the_order(session_factory) -> None:
    """Тип поменяли после продажи — заказ обязан завершиться как был оформлен."""
    async with session_factory() as session:
        user = await make_user(session)
        product = await make_product(session)
        await fill_stock(session, product, 2)
        await session.commit()

        order = await orders_service.create_order(
            session, user.tg_id, product, qty=1, promo=None, reserve_minutes=20, max_qty=10
        )
        order.status = OrderStatus.PAID
        await session.commit()

        # Админ переключает товар на ручную выдачу уже после оформления.
        product.delivery_type = DeliveryType.MANUAL
        await session.commit()

        result = await delivery_service.deliver(session, order)
        await session.commit()

        assert result.manual is False, "заказ повис бы в ожидании, хотя склад зарезервирован"
        assert result.ok is True
        assert len(result.items) == 1
        await session.refresh(order)
        assert order.status == OrderStatus.DELIVERED


async def test_format_items_marks_files_separately(session_factory) -> None:
    items = [
        delivery_service.DeliveredItem(content="просто текст"),
        delivery_service.DeliveredItem(
            content="", file_id="abc", file_kind="document", file_name="archive.zip"
        ),
    ]
    text = delivery_service.format_items(items)
    assert "просто текст" in text
    assert "archive.zip" in text
    assert "отдельным сообщением" in text


async def test_manual_product_sold_items_are_visible_in_stock_card(session_factory) -> None:
    """Выданное вручную попадает в склад проданным — иначе учёт не сойдётся."""
    async with session_factory() as session:
        user = await make_user(session)
        product = await _manual_product(session)
        await session.commit()
        order = await orders_service.create_order(
            session, user.tg_id, product, qty=1, promo=None, reserve_minutes=20, max_qty=10
        )
        order.status = OrderStatus.AWAITING
        await session.commit()

        await delivery_service.complete_manual(
            session, order, 1, delivery_service.DeliveredItem(content="готово")
        )
        await session.commit()

        counts = await stock_repo.counts_by_status(session, product.id)
        assert counts[StockStatus.SOLD] == 1
        assert counts[StockStatus.AVAILABLE] == 0
