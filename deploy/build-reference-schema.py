#!/usr/bin/env python3
"""Строит эталонную схему прямо из моделей — в отдельной базе.

Нужна для сверки: миграция и модели обязаны описывать одно и то же. Расхождение
между ними — самый тихий вид поломки: код читает колонку, которой на проде нет,
и узнаёт об этом от покупателя.

Сравнивать надо не чтением кода, а дампами схем — этим занимается
`deploy/check-migrations.sh`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402

from bot.config import get_settings  # noqa: E402
from bot.db import models  # noqa: F401,E402 — регистрирует таблицы
from bot.db.base import Base  # noqa: E402

CHECK_DB = os.getenv("SHOPBOT_SCHEMA_CHECK_DB", "shopbot_schema_check")


def main() -> int:
    settings = get_settings()
    password = quote_plus(settings.db_password)
    url = (
        f"mysql+pymysql://{settings.db_user}:{password}"
        f"@{settings.db_host}:{settings.db_port}/{CHECK_DB}?charset=utf8mb4"
    )

    engine = create_engine(url)
    with engine.begin() as conn:
        # Чистим базу ЦЕЛИКОМ, а не по списку из моделей.
        #
        # Раньше здесь удалялись таблицы из `Base.metadata` — то есть те, что
        # в моделях есть сейчас. Таблица, УБРАННАЯ из моделей, в этот список не
        # попадает и остаётся от прошлого прогона. Эталон тогда содержит то,
        # чего в моделях уже нет, и сверка показывает расхождение, которого
        # в реальности нет. База отведена под эту проверку целиком, поэтому
        # спрашивать её саму, что в ней лежит, — единственный надёжный способ.
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        existing = [
            row[0]
            for row in conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :db"
                ),
                {"db": CHECK_DB},
            )
        ]
        for name in existing:
            conn.execute(text(f"DROP TABLE IF EXISTS `{name}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

    Base.metadata.create_all(engine)
    engine.dispose()

    print(f"Эталонная схема из моделей построена в `{CHECK_DB}`: "
          f"{len(Base.metadata.tables)} таблиц")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
