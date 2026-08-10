"""Доступ к промокодам."""

from __future__ import annotations

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import PromoCode, PromoScope, PromoUse


def normalize_code(code: str) -> str:
    return code.strip().upper()


async def get(session: AsyncSession, promo_id: int) -> PromoCode | None:
    return await session.get(PromoCode, promo_id)


async def get_by_code(session: AsyncSession, code: str) -> PromoCode | None:
    result = await session.execute(
        select(PromoCode).where(PromoCode.code == normalize_code(code))
    )
    return result.scalar_one_or_none()


async def list_all(session: AsyncSession) -> list[PromoCode]:
    result = await session.execute(select(PromoCode).order_by(PromoCode.created_at.desc()))
    return list(result.scalars().all())


async def create(
    session: AsyncSession,
    code: str,
    discount_type: str,
    discount_value: int,
    created_by: int | None,
) -> PromoCode:
    promo = PromoCode(
        code=normalize_code(code),
        discount_type=discount_type,
        discount_value=discount_value,
        created_by=created_by,
    )
    session.add(promo)
    await session.flush()
    return promo


async def remove(session: AsyncSession, promo_id: int) -> None:
    await session.execute(delete(PromoCode).where(PromoCode.id == promo_id))


async def scopes_of(session: AsyncSession, promo_id: int) -> list[PromoScope]:
    result = await session.execute(select(PromoScope).where(PromoScope.promo_id == promo_id))
    return list(result.scalars().all())


async def set_scope(
    session: AsyncSession,
    promo_id: int,
    category_id: int | None = None,
    product_id: int | None = None,
) -> None:
    session.add(PromoScope(promo_id=promo_id, category_id=category_id, product_id=product_id))
    await session.flush()


async def clear_scopes(session: AsyncSession, promo_id: int) -> None:
    await session.execute(delete(PromoScope).where(PromoScope.promo_id == promo_id))


async def uses_by_user(session: AsyncSession, promo_id: int, user_id: int) -> int:
    stmt = select(func.count(PromoUse.id)).where(
        PromoUse.promo_id == promo_id, PromoUse.user_id == user_id
    )
    return int((await session.execute(stmt)).scalar_one())


async def uses_total(session: AsyncSession, promo_id: int) -> int:
    stmt = select(func.count(PromoUse.id)).where(PromoUse.promo_id == promo_id)
    return int((await session.execute(stmt)).scalar_one())


async def usage_stats(session: AsyncSession, promo_id: int) -> dict:
    from bot.db.models import Order

    stmt = (
        select(
            func.count(PromoUse.id),
            func.count(func.distinct(PromoUse.user_id)),
            func.coalesce(func.sum(Order.total_kop), 0),
            func.coalesce(func.sum(Order.discount_kop), 0),
        )
        .select_from(PromoUse)
        .join(Order, Order.id == PromoUse.order_id)
        .where(PromoUse.promo_id == promo_id)
    )
    total, users, revenue, discount = (await session.execute(stmt)).one()
    return {
        "uses": int(total),
        "users": int(users),
        "revenue_kop": int(revenue or 0),
        "discount_kop": int(discount or 0),
    }


async def consume(session: AsyncSession, promo_id: int, user_id: int, order_id: int) -> bool:
    """Списывает одно использование промокода.

    Счётчик увеличивается условным `UPDATE`: два одновременных заказа не смогут
    оба забрать последнее использование. Проверка «сначала прочитать, потом
    записать» здесь неверна — между чтением и записью помещается чужая покупка.

    Факт использования пишется в `promo_uses` с уникальным индексом на
    `(promo_id, order_id)`: повторное подтверждение одного заказа не спишет
    промокод второй раз.
    """
    existing = await session.execute(
        select(PromoUse.id).where(PromoUse.promo_id == promo_id, PromoUse.order_id == order_id)
    )
    if existing.scalar_one_or_none() is not None:
        return True

    result = await session.execute(
        update(PromoCode)
        .where(
            PromoCode.id == promo_id,
            or_(
                PromoCode.usage_limit.is_(None),
                PromoCode.used_count < PromoCode.usage_limit,
            ),
        )
        .values(used_count=PromoCode.used_count + 1)
    )
    if int(result.rowcount or 0) != 1:
        return False

    session.add(PromoUse(promo_id=promo_id, user_id=user_id, order_id=order_id))
    await session.flush()
    return True


async def release(session: AsyncSession, promo_id: int, order_id: int) -> None:
    """Возвращает использование обратно — при возврате заказа."""
    result = await session.execute(
        delete(PromoUse).where(PromoUse.promo_id == promo_id, PromoUse.order_id == order_id)
    )
    if int(result.rowcount or 0) > 0:
        await session.execute(
            update(PromoCode)
            .where(PromoCode.id == promo_id, PromoCode.used_count > 0)
            .values(used_count=PromoCode.used_count - 1)
        )
