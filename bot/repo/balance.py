"""Доступ к балансу пользователей.

Баланс — это леджер `balance_txns`. Поле `users.balance_kop` служит кешем для
скорости; их сходимость проверяется тестом и кнопкой сверки в админке.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import BalanceTxn, User


class InsufficientFunds(Exception):
    """На балансе не хватает денег."""


async def ledger_balance(session: AsyncSession, user_id: int) -> int:
    stmt = select(func.coalesce(func.sum(BalanceTxn.amount_kop), 0)).where(
        BalanceTxn.user_id == user_id
    )
    return int((await session.execute(stmt)).scalar_one())


async def move(
    session: AsyncSession,
    user_id: int,
    amount_kop: int,
    kind: str,
    order_id: int | None = None,
    admin_id: int | None = None,
    comment: str | None = None,
    allow_negative: bool = False,
) -> BalanceTxn:
    """Двигает баланс на `amount_kop` (может быть отрицательным).

    Строка пользователя блокируется: два параллельных списания не должны
    увести баланс в минус.
    """
    result = await session.execute(select(User).where(User.tg_id == user_id).with_for_update())
    user = result.scalar_one_or_none()
    if user is None:
        raise ValueError(f"Пользователь {user_id} не найден")

    new_balance = user.balance_kop + int(amount_kop)
    if new_balance < 0 and not allow_negative:
        raise InsufficientFunds(
            f"Баланс {user.balance_kop} коп., списание {amount_kop} коп."
        )

    user.balance_kop = new_balance
    txn = BalanceTxn(
        user_id=user_id,
        amount_kop=int(amount_kop),
        balance_after_kop=new_balance,
        kind=kind,
        order_id=order_id,
        admin_id=admin_id,
        comment=(comment or None) and comment[:255],
    )
    session.add(txn)
    await session.flush()
    return txn


async def history(
    session: AsyncSession, user_id: int, limit: int = 20, offset: int = 0
) -> list[BalanceTxn]:
    stmt = (
        select(BalanceTxn)
        .where(BalanceTxn.user_id == user_id)
        .order_by(BalanceTxn.created_at.desc(), BalanceTxn.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars().all())


async def find_mismatches(session: AsyncSession) -> list[tuple[int, int, int]]:
    """Пользователи, у которых кеш баланса разошёлся с леджером.

    Возвращает `(tg_id, кеш, леджер)`. Пустой список — всё сошлось.
    """
    ledger = (
        select(
            BalanceTxn.user_id.label("uid"),
            func.coalesce(func.sum(BalanceTxn.amount_kop), 0).label("total"),
        )
        .group_by(BalanceTxn.user_id)
        .subquery()
    )
    stmt = (
        select(User.tg_id, User.balance_kop, func.coalesce(ledger.c.total, 0))
        .outerjoin(ledger, ledger.c.uid == User.tg_id)
        .where(User.balance_kop != func.coalesce(ledger.c.total, 0))
    )
    return [(int(a), int(b), int(c)) for a, b, c in (await session.execute(stmt)).all()]
