"""Настройки, которые владелец меняет из админки.

Отличие от `bot.config`: там то, без чего процесс не стартует (токены, адрес
базы). Здесь то, что владелец крутит на ходу, — и оно живёт в базе.
"""

from __future__ import annotations

import time

from sqlalchemy.ext.asyncio import AsyncSession

from bot.repo import content as content_repo

_CACHE_TTL_SECONDS = 30

# Ключ → описание. `type` определяет, как админка проверит ввод.
DEFAULT_SETTINGS: dict[str, dict] = {
    "shop_name": {
        "title": "Название магазина",
        "value": "Shop",
        "type": "str",
    },
    "header_image_file_id": {
        "title": "Картинка-шапка (file_id)",
        "hint": "Заполняется автоматически при загрузке картинки в админке",
        "value": None,
        "type": "str",
    },
    "header_image_path": {
        "title": "Картинка-шапка (файл)",
        "hint": "Путь к файлу на диске; file_id кешируется отдельно",
        "value": None,
        "type": "str",
    },
    "support_contact": {
        "title": "Контакт поддержки",
        "hint": "Например @username",
        "value": "@support",
        "type": "str",
    },
    "maintenance": {
        "title": "Режим обслуживания",
        "hint": "Включён — магазин закрыт для покупателей, админка работает",
        "value": "0",
        "type": "bool",
    },
    "notify_admins_on_payment": {
        "title": "Уведомлять админов об оплатах",
        "value": "1",
        "type": "bool",
    },
    "price_markup_pct": {
        "title": "Сервисный сбор к цене, %",
        "hint": "Прибавляется к цене по курсу ЦБ. В каталоге показывается чистый курс",
        "value": "10",
        "type": "int",
    },
    "guarantee_hours": {
        "title": "Срок гарантии, часов",
        "value": "2",
        "type": "int",
    },
}


class SettingsStore:
    def __init__(self, ttl_seconds: int = _CACHE_TTL_SECONDS) -> None:
        self._cache: dict[str, str | None] = {}
        self._loaded_at = 0.0
        self._ttl = ttl_seconds

    def invalidate(self) -> None:
        self._loaded_at = 0.0

    async def _ensure(self, session: AsyncSession) -> None:
        if self._cache and (time.monotonic() - self._loaded_at) < self._ttl:
            return
        self._cache = await content_repo.settings_map(session)
        self._loaded_at = time.monotonic()

    async def get(self, session: AsyncSession, key: str) -> str | None:
        await self._ensure(session)
        if key in self._cache:
            return self._cache[key]
        default = DEFAULT_SETTINGS.get(key)
        return default.get("value") if default else None

    async def get_int(self, session: AsyncSession, key: str, fallback: int = 0) -> int:
        raw = await self.get(session, key)
        try:
            return int(str(raw))
        except (TypeError, ValueError):
            return fallback

    async def get_bool(self, session: AsyncSession, key: str, fallback: bool = False) -> bool:
        raw = await self.get(session, key)
        if raw is None:
            return fallback
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "да")

    async def set(
        self, session: AsyncSession, key: str, value: str | None, updated_by: int | None = None
    ) -> None:
        await content_repo.set_setting(session, key, value, updated_by)
        self.invalidate()


settings_store = SettingsStore()


async def seed(session: AsyncSession) -> int:
    added = await content_repo.seed_settings(session, DEFAULT_SETTINGS)
    if added:
        settings_store.invalidate()
    return added
