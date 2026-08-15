"""Доступ к пользователям и администраторам."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import utcnow
from bot.db.models import Admin, Order, OrderStatus, User


async def get_user(session: AsyncSession, tg_id: int) -> User | None:
    return await session.get(User, tg_id)


async def get_user_for_update(session: AsyncSession, tg_id: int) -> User | None:
    """Пользователь с блокировкой строки — для операций с балансом."""
    result = await session.execute(select(User).where(User.tg_id == tg_id).with_for_update())
    return result.scalar_one_or_none()


async def upsert_user(
    session: AsyncSession,
    tg_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> tuple[User, bool]:
    """Возвращает `(пользователь, создан_ли_сейчас)`."""
    user = await session.get(User, tg_id)
    if user is None:
        user = User(
            tg_id=tg_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            created_at=utcnow(),
            last_seen_at=utcnow(),
        )
        session.add(user)
        await session.flush()
        return user, True

    user.username = username
    user.first_name = first_name
    user.last_name = last_name
    user.last_seen_at = utcnow()
    if user.has_blocked_bot:
        # Написал — значит разблокировал.
        user.has_blocked_bot = False
    return user, False


async def set_blocked(
    session: AsyncSession, tg_id: int, blocked: bool, reason: str | None = None
) -> None:
    await session.execute(
        update(User)
        .where(User.tg_id == tg_id)
        .values(is_blocked=blocked, block_reason=reason if blocked else None)
    )


async def mark_bot_blocked(session: AsyncSession, tg_id: int, blocked: bool = True) -> None:
    await session.execute(
        update(User).where(User.tg_id == tg_id).values(has_blocked_bot=blocked)
    )


async def all_reachable_ids(session: AsyncSession) -> list[int]:
    """Кому имеет смысл слать рассылку."""
    result = await session.execute(
        select(User.tg_id).where(User.has_blocked_bot.is_(False), User.is_blocked.is_(False))
    )
    return list(result.scalars().all())


async def count_users(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count(User.tg_id)))).scalar_one())


async def count_users_since(session: AsyncSession, since: datetime) -> int:
    result = await session.execute(
        select(func.count(User.tg_id)).where(User.created_at >= since)
    )
    return int(result.scalar_one())


async def count_active_since(session: AsyncSession, since: datetime) -> int:
    result = await session.execute(
        select(func.count(User.tg_id)).where(User.last_seen_at >= since)
    )
    return int(result.scalar_one())


async def search(session: AsyncSession, query: str, limit: int = 20) -> list[User]:
    """Поиск пользователя по Telegram ID или username."""
    query = query.strip().lstrip("@")
    conditions = [User.username.ilike(f"%{query}%")]
    if query.isdigit():
        conditions.append(User.tg_id == int(query))
    result = await session.execute(
        select(User).where(or_(*conditions)).order_by(User.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def user_summary(session: AsyncSession, tg_id: int) -> dict:
    """Сводка по покупателю для карточки в админке."""
    result = await session.execute(
        select(func.count(Order.id), func.coalesce(func.sum(Order.total_kop), 0)).where(
            Order.user_id == tg_id,
            Order.status.in_([OrderStatus.PAID, OrderStatus.DELIVERED]),
        )
    )
    orders_count, spent = result.one()
    return {"orders": int(orders_count), "spent_kop": int(spent or 0)}


# --- Администраторы ---------------------------------------------------------


async def list_admins(session: AsyncSession) -> list[Admin]:
    result = await session.execute(select(Admin).order_by(Admin.created_at))
    return list(result.scalars().all())


async def get_admin(session: AsyncSession, user_id: int) -> Admin | None:
    result = await session.execute(select(Admin).where(Admin.user_id == user_id))
    return result.scalar_one_or_none()


async def add_admin(session: AsyncSession, user_id: int, added_by: int | None) -> Admin:
    existing = await get_admin(session, user_id)
    if existing is not None:
        return existing
    admin = Admin(user_id=user_id, added_by=added_by)
    session.add(admin)
    await session.flush()
    return admin


async def remove_admin(session: AsyncSession, user_id: int) -> None:
    await session.execute(delete(Admin).where(Admin.user_id == user_id))


async def ensure_owners(session: AsyncSession, owner_ids: list[int]) -> None:
    """Восстанавливает администраторов из окружения.

    Список задаётся переменной `OWNER_IDS` и восстанавливается при каждом
    старте: иначе неудачная правка в админке однажды оставит магазин без
    единого администратора, и починить его будет нечем.
    """
    for owner_id in owner_ids:
        if await get_admin(session, owner_id) is None:
            session.add(Admin(user_id=owner_id, added_by=None))
    await session.flush()


async def recent_users(session: AsyncSession, days: int = 7, limit: int = 10) -> list[User]:
    since = utcnow() - timedelta(days=days)
    result = await session.execute(
        select(User).where(User.created_at >= since).order_by(User.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())
