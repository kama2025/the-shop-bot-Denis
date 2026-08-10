"""Журнал действий администраторов.

Нужен не для отчётности, а для разбора: «кто вчера удалил категорию» — вопрос,
который однажды обязательно задают.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import AuditLog


async def record(
    session: AsyncSession,
    admin_id: int,
    action: str,
    entity: str | None = None,
    entity_id: str | int | None = None,
    details: dict | None = None,
) -> None:
    session.add(
        AuditLog(
            admin_id=admin_id,
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            details=details,
        )
    )
    await session.flush()


async def recent(session: AsyncSession, limit: int = 30) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())
