"""Базовый класс моделей и общие соглашения.

Время везде хранится наивным UTC. Смешивать наивное и осведомлённое время в
одной базе нельзя: сравнения начинают падать, а разница в три часа
обнаруживается в отчёте о продажах.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Явные шаблоны имён: иначе имя ограничения придумывает СУБД, и в откате
# миграции его нечем назвать.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}

# Коллацию задаём явно каждой таблице. Без этого одна таблица однажды создаётся
# с коллацией по умолчанию сервера, расходится с родительской, и внешний ключ
# падает с ошибкой 3780 — на проде, при накате миграции.
TABLE_ARGS: dict[str, Any] = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def utcnow() -> datetime:
    """Текущее время в UTC без таймзоны."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
