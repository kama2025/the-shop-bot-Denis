#!/usr/bin/env python3
"""Демонстрационное наполнение каталога.

Нужно, чтобы магазин можно было потрогать сразу после запуска: пустой каталог
не даёт проверить ни покупку, ни сбор реквизитов, ни промокод.

Кроме товаров скрипт кладёт **курс доллара**. Без курса магазин честно
отказывается продавать, и первый же запуск без интернета выглядел бы как
поломка. Курс помечен источником `demo` — настоящий приедет от ЦБ в течение
часа и заменит его собой.

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
from bot.db.models import Category, ExchangeRate, Order, Product, PromoCode  # noqa: E402
from bot.db.session import make_engine, make_session_factory, session_scope  # noqa: E402
from bot.repo import rates as rates_repo  # noqa: E402
from bot.services import pricing  # noqa: E402
from bot.utils.money import DISCOUNT_PERCENT, format_kop  # noqa: E402

MARK = "ДЕМО"

DEMO_RATE_KOP = 9000
"""90,00 ₽ за доллар — правдоподобное круглое число для первого запуска."""

# (название категории, цвет, товары)
CATALOG = [
    (
        f"{MARK}: Подписки",
        "success",
        [
            (
                "ChatGPT Plus — 1 месяц",
                "▪️ Оплата вашего аккаунта на месяц\n"
                "▪️ Нужен доступ: логин и пароль\n"
                "▪️ Срок выполнения: до 2 часов",
                2000,
            ),
            (
                "Netflix Premium — 1 месяц",
                "▪️ Продление вашей подписки\n"
                "▪️ Нужен доступ: логин и пароль\n"
                "▪️ 4K, четыре экрана",
                1999,
            ),
            (
                "Spotify Premium — 3 месяца",
                "▪️ Продление на три месяца\n▪️ Нужен доступ: логин и пароль",
                3050,
            ),
        ],
    ),
    (
        f"{MARK}: Инструменты",
        "primary",
        [
            (
                "Cursor Pro — 1 месяц",
                "▪️ Оплата вашего аккаунта\n▪️ Нужен доступ: логин и пароль",
                2000,
            ),
            (
                "Midjourney Basic — 1 месяц",
                "▪️ Оплата вашего аккаунта\n▪️ Нужен доступ: логин и пароль",
                1000,
            ),
        ],
    ),
]

PROMO_CODE = "DEMO10"


async def seed() -> None:
    settings = get_settings()
    engine = make_engine(settings)
    factory = make_session_factory(engine)

    created_products = 0
    rate_note = "курс уже был"

    async with session_scope(factory) as session:
        # Курс — первым делом: без него каталог покажет цены только в долларах.
        if await rates_repo.latest(session, "USD") is None:
            await rates_repo.add(session, DEMO_RATE_KOP, code="USD", source="demo")
            rate_note = f"курс поставлен: {format_kop(DEMO_RATE_KOP)} за $1"

        for order, (title, accent, products) in enumerate(CATALOG, start=1):
            existing = await session.execute(select(Category).where(Category.title == title))
            category = existing.scalar_one_or_none()
            if category is None:
                category = Category(title=title, accent=accent, sort_order=order * 10)
                session.add(category)
                await session.flush()

            for product_order, (name, description, price_usd_cents) in enumerate(
                products, start=1
            ):
                found = await session.execute(select(Product).where(Product.title == name))
                if found.scalar_one_or_none() is not None:
                    continue
                session.add(
                    Product(
                        category_id=category.id,
                        title=name,
                        description=description,
                        price_usd_cents=price_usd_cents,
                        sort_order=product_order * 10,
                    )
                )
                created_products += 1

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

    print(f"Создано товаров: {created_products}")
    print(rate_note)
    print(f"Промокод: {PROMO_CODE} — 10 %, без лимитов, на весь магазин")
    print()
    print("Цены при этом курсе (в скобках — к оплате с наценкой 10 %):")
    for _, _, products in CATALOG:
        for name, _, cents in products:
            base = pricing.base_kop(cents, DEMO_RATE_KOP)
            charge = pricing.with_markup(base, 10)
            print(
                f"  {pricing.format_usd(cents):>8}  {format_kop(base):>12}"
                f"  ({format_kop(charge)})   {name}"
            )
    print()
    print("У товаров нет картинок — их добавляет админ через «Выложить товар».")


async def clean() -> None:
    settings = get_settings()
    engine = make_engine(settings)
    factory = make_session_factory(engine)

    removed = 0
    kept = 0

    async with session_scope(factory) as session:
        categories = (
            (await session.execute(select(Category).where(Category.title.like(f"{MARK}%"))))
            .scalars()
            .all()
        )
        for category in categories:
            products = (
                (await session.execute(select(Product).where(Product.category_id == category.id)))
                .scalars()
                .all()
            )
            for product in products:
                # Товар, по которому были заказы, не удаляем: `orders.product_id`
                # ссылается на него, и в истории покупателя останется дыра.
                sold = await session.execute(
                    select(Order.id).where(Order.product_id == product.id).limit(1)
                )
                if sold.scalar_one_or_none() is not None:
                    product.is_active = False
                    kept += 1
                    continue
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
        # Демонстрационный курс убираем, настоящий от ЦБ не трогаем.
        await session.execute(delete(ExchangeRate).where(ExchangeRate.source == "demo"))

    await engine.dispose()
    print(f"Удалено демо-товаров: {removed}")
    if kept:
        print(f"Выключено, но не удалено (есть заказы): {kept} — история сохранена.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true", help="убрать демо-данные")
    args = parser.parse_args()
    asyncio.run(clean() if args.clean else seed())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
