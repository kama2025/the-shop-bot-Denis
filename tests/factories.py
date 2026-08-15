"""Заготовки объектов для тестов."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import utcnow
from bot.db.models import (
    BalanceTxn,
    BalanceTxnKind,
    Category,
    ExchangeRate,
    Order,
    OrderStatus,
    Product,
    User,
)

DEFAULT_RATE_KOP = 9000
"""90,00 ₽ за доллар — круглое число, на котором расчёты читаются глазами."""


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
    price_usd_cents: int = 2000,
) -> Product:
    if category is None:
        category = await make_category(session)
    product = Product(
        category_id=category.id,
        title=title,
        price_usd_cents=price_usd_cents,
        sort_order=10,
    )
    session.add(product)
    await session.flush()
    return product


async def make_rate(
    session: AsyncSession, rate_kop: int = DEFAULT_RATE_KOP, code: str = "USD"
) -> ExchangeRate:
    row = ExchangeRate(code=code, rate_kop=rate_kop, source="test", fetched_at=utcnow())
    session.add(row)
    await session.flush()
    return row


async def make_paid_order(
    session: AsyncSession,
    user: User,
    product: Product,
    total_kop: int = 198000,
    status: str = OrderStatus.PAID,
) -> Order:
    """Заказ, за который уже заплатили.

    Нужен там, где проверяется не покупка, а то, что происходит после неё.
    Снимки цены заполнены — иначе тест проверяет заказ, который в бою
    возникнуть не может.
    """
    order = Order(
        user_id=user.tg_id,
        product_id=product.id,
        product_title=product.title,
        price_usd_cents=product.price_usd_cents,
        rate_kop=DEFAULT_RATE_KOP,
        markup_pct=10,
        qty=1,
        unit_price_kop=total_kop,
        subtotal_kop=total_kop,
        discount_kop=0,
        total_kop=total_kop,
        status=status,
        paid_at=utcnow(),
    )
    session.add(order)
    await session.flush()
    return order
