"""Доступ к рассылкам."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import utcnow
from bot.db.models import Broadcast, BroadcastStatus, BroadcastTarget, TargetStatus


async def create(
    session: AsyncSession,
    admin_id: int,
    content_type: str,
    text: str | None,
    file_id: str | None,
    buttons: list | None,
) -> Broadcast:
    broadcast = Broadcast(
        admin_id=admin_id,
        content_type=content_type,
        text=text,
        file_id=file_id,
        buttons=buttons,
    )
    session.add(broadcast)
    await session.flush()
    return broadcast


async def get(session: AsyncSession, broadcast_id: int) -> Broadcast | None:
    return await session.get(Broadcast, broadcast_id)


async def list_recent(session: AsyncSession, limit: int = 10) -> list[Broadcast]:
    stmt = select(Broadcast).order_by(Broadcast.created_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def enqueue_targets(session: AsyncSession, broadcast_id: int, user_ids: list[int]) -> int:
    """Раскладывает получателей по строкам.

    Отдельная строка на каждого нужна, чтобы после перезапуска рассылка
    продолжилась с места остановки, а не разослала всё заново.
    """
    if not user_ids:
        return 0
    session.add_all(
        BroadcastTarget(broadcast_id=broadcast_id, user_id=user_id) for user_id in user_ids
    )
    await session.flush()
    await session.execute(
        update(Broadcast).where(Broadcast.id == broadcast_id).values(total=len(user_ids))
    )
    return len(user_ids)


async def next_pending(
    session: AsyncSession, broadcast_id: int, limit: int = 50
) -> list[BroadcastTarget]:
    stmt = (
        select(BroadcastTarget)
        .where(
            BroadcastTarget.broadcast_id == broadcast_id,
            BroadcastTarget.status == TargetStatus.PENDING,
        )
        .order_by(BroadcastTarget.id)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def mark_target(
    session: AsyncSession, target_id: int, status: str, error: str | None = None
) -> None:
    await session.execute(
        update(BroadcastTarget)
        .where(BroadcastTarget.id == target_id)
        .values(status=status, error=(error or None) and error[:255], sent_at=utcnow())
    )


async def refresh_counters(session: AsyncSession, broadcast_id: int) -> Broadcast | None:
    stmt = (
        select(BroadcastTarget.status, func.count(BroadcastTarget.id))
        .where(BroadcastTarget.broadcast_id == broadcast_id)
        .group_by(BroadcastTarget.status)
    )
    counts = {status: int(count) for status, count in (await session.execute(stmt)).all()}
    broadcast = await session.get(Broadcast, broadcast_id)
    if broadcast is None:
        return None
    broadcast.sent = counts.get(TargetStatus.SENT, 0)
    broadcast.failed = counts.get(TargetStatus.FAILED, 0)
    broadcast.blocked = counts.get(TargetStatus.BLOCKED, 0)
    await session.flush()
    return broadcast


async def set_status(session: AsyncSession, broadcast_id: int, status: str) -> None:
    values: dict = {"status": status}
    if status == BroadcastStatus.RUNNING:
        values["started_at"] = utcnow()
    if status in (BroadcastStatus.DONE, BroadcastStatus.CANCELED):
        values["finished_at"] = utcnow()
    await session.execute(update(Broadcast).where(Broadcast.id == broadcast_id).values(**values))


async def running_ids(session: AsyncSession) -> list[int]:
    stmt = select(Broadcast.id).where(Broadcast.status == BroadcastStatus.RUNNING)
    return [int(x) for x in (await session.execute(stmt)).scalars().all()]
