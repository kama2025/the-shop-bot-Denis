"""Доступ к настраиваемому содержимому: тексты, настройки, каналы."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import utcnow
from bot.db.models import Channel, SettingEntry, TextEntry


# --- Тексты -----------------------------------------------------------------


async def all_texts(session: AsyncSession) -> list[TextEntry]:
    result = await session.execute(select(TextEntry).order_by(TextEntry.sort_order, TextEntry.key))
    return list(result.scalars().all())


async def texts_map(session: AsyncSession) -> dict[str, str]:
    result = await session.execute(select(TextEntry.key, TextEntry.value))
    return {key: value for key, value in result.all()}


async def get_text(session: AsyncSession, key: str) -> TextEntry | None:
    return await session.get(TextEntry, key)


async def set_text(
    session: AsyncSession, key: str, value: str, updated_by: int | None = None
) -> None:
    entry = await session.get(TextEntry, key)
    if entry is None:
        entry = TextEntry(key=key, value=value, title=key)
        session.add(entry)
    else:
        entry.value = value
    entry.updated_at = utcnow()
    entry.updated_by = updated_by
    await session.flush()


async def seed_texts(session: AsyncSession, defaults: dict[str, dict]) -> int:
    """Добавляет недостающие тексты, не трогая уже отредактированные."""
    existing = {key for key in (await session.execute(select(TextEntry.key))).scalars().all()}
    added = 0
    for order, (key, meta) in enumerate(defaults.items(), start=1):
        if key in existing:
            continue
        session.add(
            TextEntry(
                key=key,
                value=meta["value"],
                title=meta["title"],
                hint=meta.get("hint"),
                sort_order=order * 10,
            )
        )
        added += 1
    await session.flush()
    return added


# --- Настройки --------------------------------------------------------------


async def all_settings(session: AsyncSession) -> list[SettingEntry]:
    result = await session.execute(
        select(SettingEntry).order_by(SettingEntry.sort_order, SettingEntry.key)
    )
    return list(result.scalars().all())


async def settings_map(session: AsyncSession) -> dict[str, str | None]:
    result = await session.execute(select(SettingEntry.key, SettingEntry.value))
    return {key: value for key, value in result.all()}


async def get_setting(session: AsyncSession, key: str) -> SettingEntry | None:
    return await session.get(SettingEntry, key)


async def set_setting(
    session: AsyncSession, key: str, value: str | None, updated_by: int | None = None
) -> None:
    entry = await session.get(SettingEntry, key)
    if entry is None:
        entry = SettingEntry(key=key, value=value, title=key)
        session.add(entry)
    else:
        entry.value = value
    entry.updated_at = utcnow()
    entry.updated_by = updated_by
    await session.flush()


async def seed_settings(session: AsyncSession, defaults: dict[str, dict]) -> int:
    existing = {key for key in (await session.execute(select(SettingEntry.key))).scalars().all()}
    added = 0
    for order, (key, meta) in enumerate(defaults.items(), start=1):
        if key in existing:
            continue
        session.add(
            SettingEntry(
                key=key,
                value=meta.get("value"),
                title=meta["title"],
                hint=meta.get("hint"),
                value_type=meta.get("type", "str"),
                sort_order=order * 10,
            )
        )
        added += 1
    await session.flush()
    return added


# --- Каналы -----------------------------------------------------------------


async def list_channels(session: AsyncSession, only_active: bool = True) -> list[Channel]:
    stmt = select(Channel)
    if only_active:
        stmt = stmt.where(Channel.is_active.is_(True))
    stmt = stmt.order_by(Channel.sort_order, Channel.id)
    return list((await session.execute(stmt)).scalars().all())


async def get_channel(session: AsyncSession, channel_id: int) -> Channel | None:
    return await session.get(Channel, channel_id)


async def add_channel(
    session: AsyncSession, chat_ref: str, title: str, invite_url: str
) -> Channel:
    channels = await list_channels(session, only_active=False)
    channel = Channel(
        chat_ref=chat_ref,
        title=title,
        invite_url=invite_url,
        sort_order=(len(channels) + 1) * 10,
    )
    session.add(channel)
    await session.flush()
    return channel


async def remove_channel(session: AsyncSession, channel_id: int) -> None:
    await session.execute(delete(Channel).where(Channel.id == channel_id))


async def set_channel_error(session: AsyncSession, channel_id: int, error: str | None) -> None:
    channel = await session.get(Channel, channel_id)
    if channel is not None:
        channel.last_error = error[:255] if error else None
