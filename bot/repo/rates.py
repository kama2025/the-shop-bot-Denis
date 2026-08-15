"""Доступ к истории курса."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import utcnow
from bot.db.models import ExchangeRate


async def latest(session: AsyncSession, code: str = "USD") -> ExchangeRate | None:
    stmt = (
        select(ExchangeRate)
        .where(ExchangeRate.code == code)
        .order_by(ExchangeRate.fetched_at.desc(), ExchangeRate.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def add(
    session: AsyncSession, rate_kop: int, code: str = "USD", source: str = "cbr"
) -> ExchangeRate:
    """Добавляет строку. Существующие не трогает — история нужна целиком."""
    row = ExchangeRate(code=code, rate_kop=int(rate_kop), source=source, fetched_at=utcnow())
    session.add(row)
    await session.flush()
    return row
