"""Общая оснастка тестов.

Тесты, помеченные `@pytest.mark.db`, идут на **настоящую MySQL**. Замена на
SQLite была бы удобнее, но бессмысленна: проверяются именно те свойства,
которых у SQLite нет — `SELECT ... FOR UPDATE SKIP LOCKED`, поведение
блокировок при параллельных покупках, атомарность условного `UPDATE`.
Тест, прошедший на другой СУБД, измеряет не тот мир.

Если MySQL недоступна, такие тесты **пропускаются с явным сообщением**, а не
считаются пройденными: ноль падений без счётчика проверенного — это «не
проверяли», а не «чисто».
"""

from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.config import get_settings
from bot.db.base import Base
from bot.db import models  # noqa: F401 — регистрирует таблицы

TEST_DB_NAME = os.getenv("SHOPBOT_TEST_DB", "shopbot_test")


def _admin_url() -> str:
    settings = get_settings()
    from urllib.parse import quote_plus

    password = quote_plus(settings.db_password)
    return (
        f"mysql+aiomysql://{settings.db_user}:{password}"
        f"@{settings.db_host}:{settings.db_port}/?charset=utf8mb4"
    )


def _test_url() -> str:
    settings = get_settings()
    from urllib.parse import quote_plus

    password = quote_plus(settings.db_password)
    return (
        f"mysql+aiomysql://{settings.db_user}:{password}"
        f"@{settings.db_host}:{settings.db_port}/{TEST_DB_NAME}?charset=utf8mb4"
    )


async def _prepare_database() -> bool:
    admin = create_async_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(
                text(
                    f"CREATE DATABASE IF NOT EXISTS `{TEST_DB_NAME}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
    except Exception:
        return False
    finally:
        await admin.dispose()

    engine = create_async_engine(_test_url())
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()
    return True


@pytest.fixture(scope="session")
def db_available() -> bool:
    try:
        return asyncio.run(_prepare_database())
    except Exception:
        return False


@pytest_asyncio.fixture
async def engine(db_available: bool):
    if not db_available:
        pytest.skip(
            "MySQL недоступна — тесты с БД пропущены. "
            "Поднимите её: docker compose -f docker-compose.dev.yml up -d"
        )
    engine = create_async_engine(_test_url(), pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    # Чистим перед каждым тестом, а не после: упавший тест оставляет данные,
    # и их видно при разборе.
    async with engine.begin() as conn:
        await conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(text(f"TRUNCATE TABLE `{table.name}`"))
        await conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

    yield factory


@pytest_asyncio.fixture
async def session(session_factory):
    async with session_factory() as session:
        yield session
        await session.rollback()
