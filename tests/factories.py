"""Заготовки объектов для тестов."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import utcnow
from bot.db.models import BalanceTxn, BalanceTxnKind, Category, Product, StockItem, User


async def make_user(session: AsyncSession, tg_id: int = 1001, balance_kop: int = 0) -> User:
    """Создаёт пользователя.

    Стартовый баланс кладётся и в кеш, и в леджер. Иначе заготовка сама ломает
    инвариант «леджер — источник правды», и тесты про баланс начинают проверять
    несуществующее состояние.
    """
    user = User(
        tg_id=tg_id,
        username=f"user{tg_id}",
        first_name="Тест",
        balance_kop=balance_kop,
        created_at=utcnow(),
        last_seen_at=utcnow(),
    )
    session.add(user)
    await session.flush()

    if balance_kop:
        session.add(
            BalanceTxn(
                user_id=tg_id,
                amount_kop=balance_kop,
                balance_after_kop=balance_kop,
                kind=BalanceTxnKind.TOPUP,
                comment="Стартовый баланс в тесте",
            )
        )
        await session.flush()
    return user


async def make_category(session: AsyncSession, title: str = "Категория") -> Category:
    category = Category(title=title, sort_order=10)
    session.add(category)
    await session.flush()
    return category


async def make_product(
    session: AsyncSession,
    category: Category | None = None,
    title: str = "Товар",
    price_kop: int = 9000,
) -> Product:
    if category is None:
        category = await make_category(session)
    product = Product(
        category_id=category.id, title=title, price_kop=price_kop, sort_order=10
    )
    session.add(product)
    await session.flush()
    return product


async def fill_stock(session: AsyncSession, product: Product, count: int) -> list[StockItem]:
    items = [
        StockItem(product_id=product.id, content=f"позиция-{product.id}-{index}")
        for index in range(count)
    ]
    session.add_all(items)
    await session.flush()
    return items
