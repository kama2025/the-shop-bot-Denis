#!/usr/bin/env python3
"""Демонстрационное наполнение каталога.

Нужно, чтобы магазин можно было потрогать сразу после запуска: пустой каталог
не даёт проверить ни покупку, ни выдачу, ни промокод.

Всё созданное помечено словом «ДЕМО» в названии и удаляется из админки за
несколько нажатий либо этим же скриптом с `--clean`.

    scripts/seed_demo.py           наполнить
    scripts/seed_demo.py --clean   убрать демо-данные
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select  # noqa: E402

from bot.config import get_settings  # noqa: E402
from bot.db.models import (  # noqa: E402
    Category,
    Product,
    PromoCode,
    StockBatch,
    StockItem,
)
from bot.db.session import make_engine, make_session_factory, session_scope  # noqa: E402
from bot.repo import stock as stock_repo  # noqa: E402
from bot.utils.money import DISCOUNT_PERCENT  # noqa: E402

MARK = "ДЕМО"

CATALOG = [
    (
        f"{MARK}: Подписки",
        [
            (
                "Gemini Pro 12 мес.",
                "▪️ Выдача: подарочной ссылкой\n"
                "▪️ Гарантия: 2 часа с момента выдачи\n"
                "▪️ Активировать просто — перейти по ссылке",
                9000,
                [
                    "https://gemini.google.com/gift/DEMO-AAA-111",
                    "https://gemini.google.com/gift/DEMO-BBB-222",
                    "https://gemini.google.com/gift/DEMO-CCC-333",
                ],
            ),
            (
                "ChatGPT Plus 1 мес.",
                "▪️ Выдача: логин и пароль\n▪️ Гарантия: 2 часа",
                45000,
                [
                    "login: demo1@example.com\npass: DemoPass111\nСрок: 1 мес.",
                    "login: demo2@example.com\npass: DemoPass222\nСрок: 1 мес.",
                ],
            ),
        ],
    ),
    (
        f"{MARK}: Инструменты",
        [
            (
                "Cursor Pro 1 мес.",
                "▪️ Выдача: код активации",
                30000,
                ["CURSOR-DEMO-KEY-0001", "CURSOR-DEMO-KEY-0002"],
            ),
        ],
    ),
]

PROMO_CODE = "DEMO10"


async def seed() -> None:
    settings = get_settings()
    engine = make_engine(settings)
    factory = make_session_factory(engine)

    async with session_scope(factory) as session:
        created_products = 0
        created_items = 0

        for order, (title, products) in enumerate(CATALOG, start=1):
            existing = await session.execute(select(Category).where(Category.title == title))
            category = existing.scalar_one_or_none()
            if category is None:
                category = Category(title=title, sort_order=order * 10)
                session.add(category)
                await session.flush()

            for product_order, (name, description, price_kop, items) in enumerate(
                products, start=1
            ):
                found = await session.execute(select(Product).where(Product.title == name))
                if found.scalar_one_or_none() is not None:
                    continue
                product = Product(
                    category_id=category.id,
                    title=name,
                    description=description,
                    price_kop=price_kop,
                    sort_order=product_order * 10,
                )
                session.add(product)
                await session.flush()
                created_products += 1

                await stock_repo.add_batch(
                    session, product.id, items, admin_id=None, note="демо-завоз"
                )
                created_items += len(items)

        found = await session.execute(select(PromoCode).where(PromoCode.code == PROMO_CODE))
        if found.scalar_one_or_none() is None:
            session.add(
                PromoCode(
                    code=PROMO_CODE,
                    discount_type=DISCOUNT_PERCENT,
                    discount_value=10,
                    usage_limit=None,
                    per_user_limit=None,
                    min_order_kop=0,
                )
            )

    await engine.dispose()
    print(f"Создано товаров: {created_products}, позиций на складе: {created_items}")
    print(f"Промокод: {PROMO_CODE} — 10 %, без лимитов, на весь магазин")


async def clean() -> None:
    settings = get_settings()
    engine = make_engine(settings)
    factory = make_session_factory(engine)

    async with session_scope(factory) as session:
        categories = (
            (await session.execute(select(Category).where(Category.title.like(f"{MARK}%"))))
            .scalars()
            .all()
        )
        removed = 0
        for category in categories:
            products = (
                (await session.execute(select(Product).where(Product.category_id == category.id)))
                .scalars()
                .all()
            )
            for product in products:
                # Проданные позиции связаны с заказами: их не трогаем, товар
                # просто выключаем — история покупок важнее чистоты каталога.
                sold = await session.execute(
                    select(StockItem).where(
                        StockItem.product_id == product.id, StockItem.status == "sold"
                    )
                )
                if sold.scalars().first() is not None:
                    product.is_active = False
                    continue
                await session.execute(
                    delete(StockItem).where(StockItem.product_id == product.id)
                )
                await session.execute(
                    delete(StockBatch).where(StockBatch.product_id == product.id)
                )
                await session.delete(product)
                removed += 1
            await session.flush()

            left = await session.execute(
                select(Product).where(Product.category_id == category.id)
            )
            if left.scalars().first() is None:
                await session.delete(category)
            else:
                category.is_active = False

        await session.execute(delete(PromoCode).where(PromoCode.code == PROMO_CODE))

    await engine.dispose()
    print(f"Удалено демо-товаров: {removed}")
    print("Товары с продажами не удалены, а выключены — история заказов сохранена.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true", help="убрать демо-данные")
    args = parser.parse_args()
    asyncio.run(clean() if args.clean else seed())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
