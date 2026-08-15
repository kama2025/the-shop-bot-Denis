"""Доступ к категориям и товарам."""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Category, Product
from bot.services import search as search_service


# --- Категории --------------------------------------------------------------


async def list_categories(session: AsyncSession, only_active: bool = True) -> list[Category]:
    stmt = select(Category)
    if only_active:
        stmt = stmt.where(Category.is_active.is_(True))
    stmt = stmt.order_by(Category.sort_order, Category.id)
    return list((await session.execute(stmt)).scalars().all())


async def get_category(session: AsyncSession, category_id: int) -> Category | None:
    return await session.get(Category, category_id)


async def create_category(
    session: AsyncSession, title: str, description: str | None = None
) -> Category:
    next_order = (
        await session.execute(select(func.coalesce(func.max(Category.sort_order), 0)))
    ).scalar_one()
    category = Category(title=title, description=description, sort_order=int(next_order) + 10)
    session.add(category)
    await session.flush()
    return category


async def delete_category(session: AsyncSession, category_id: int) -> None:
    await session.execute(delete(Category).where(Category.id == category_id))


async def count_products_in_category(
    session: AsyncSession, category_id: int, only_active: bool = False
) -> int:
    stmt = select(func.count(Product.id)).where(Product.category_id == category_id)
    if only_active:
        stmt = stmt.where(Product.is_active.is_(True))
    return int((await session.execute(stmt)).scalar_one())


async def count_stock_in_category(session: AsyncSession, category_id: int) -> int:
    """Сколько свободных позиций склада лежит внутри категории.

    Нужно, чтобы предупредить админа перед удалением: «внутри 3 товара и
    47 позиций».
    """
    stmt = (
        select(func.count(StockItem.id))
        .join(Product, Product.id == StockItem.product_id)
        .where(Product.category_id == category_id, StockItem.status == StockStatus.AVAILABLE)
    )
    return int((await session.execute(stmt)).scalar_one())


async def reorder_categories(session: AsyncSession, ordered_ids: list[int]) -> None:
    for position, category_id in enumerate(ordered_ids, start=1):
        category = await session.get(Category, category_id)
        if category is not None:
            category.sort_order = position * 10


async def swap_category_order(session: AsyncSession, category_id: int, direction: int) -> bool:
    """Двигает категорию на одну позицию. `direction` = -1 вверх, +1 вниз."""
    categories = await list_categories(session, only_active=False)
    index = next((i for i, c in enumerate(categories) if c.id == category_id), None)
    if index is None:
        return False
    target = index + direction
    if target < 0 or target >= len(categories):
        return False
    categories[index].sort_order, categories[target].sort_order = (
        categories[target].sort_order,
        categories[index].sort_order,
    )
    return True


# --- Товары -----------------------------------------------------------------


async def list_products(
    session: AsyncSession, category_id: int | None = None, only_active: bool = True
) -> list[Product]:
    stmt = select(Product)
    if category_id is not None:
        stmt = stmt.where(Product.category_id == category_id)
    if only_active:
        stmt = stmt.where(Product.is_active.is_(True))
    stmt = stmt.order_by(Product.sort_order, Product.id)
    return list((await session.execute(stmt)).scalars().all())


async def get_product(session: AsyncSession, product_id: int) -> Product | None:
    return await session.get(Product, product_id)


async def create_product(
    session: AsyncSession,
    category_id: int,
    title: str,
    description: str | None,
    price_usd_cents: int,
    image_path: str | None = None,
    image_file_id: str | None = None,
) -> Product:
    next_order = (
        await session.execute(
            select(func.coalesce(func.max(Product.sort_order), 0)).where(
                Product.category_id == category_id
            )
        )
    ).scalar_one()
    product = Product(
        category_id=category_id,
        title=title,
        description=description,
        price_usd_cents=price_usd_cents,
        image_path=image_path,
        image_file_id=image_file_id,
        sort_order=int(next_order) + 10,
    )
    session.add(product)
    await session.flush()
    return product


async def delete_product(session: AsyncSession, product_id: int) -> None:
    await session.execute(delete(Product).where(Product.id == product_id))


async def swap_product_order(session: AsyncSession, product_id: int, direction: int) -> bool:
    product = await session.get(Product, product_id)
    if product is None:
        return False
    products = await list_products(session, product.category_id, only_active=False)
    index = next((i for i, p in enumerate(products) if p.id == product_id), None)
    if index is None:
        return False
    target = index + direction
    if target < 0 or target >= len(products):
        return False
    products[index].sort_order, products[target].sort_order = (
        products[target].sort_order,
        products[index].sort_order,
    )
    return True


async def search_products(
    session: AsyncSession, query: str, limit: int = 30, only_active: bool = True
) -> list[Product]:
    """Поиск по названию с допуском на опечатку.

    Похожесть считается в Python, а не в SQL: каталог здесь — десятки записей,
    и вытащить пары «id, название» дешевле, чем заводить полнотекстовый индекс,
    который всё равно плохо прощает опечатки.
    """
    query = query.strip()
    if not query:
        return []

    stmt = select(Product.id, Product.title)
    if only_active:
        stmt = stmt.where(Product.is_active.is_(True))
    stmt = stmt.order_by(Product.sort_order, Product.id)
    rows = [(int(pid), title) for pid, title in (await session.execute(stmt)).all()]

    ranked_ids = search_service.rank(query, rows, limit=limit)
    if not ranked_ids:
        return []

    found = (
        await session.execute(select(Product).where(Product.id.in_(ranked_ids)))
    ).scalars().all()
    by_id = {product.id: product for product in found}
    # Порядок задаёт ранжирование, а не база: SQL вернёт строки как ему удобно.
    return [by_id[pid] for pid in ranked_ids if pid in by_id]
