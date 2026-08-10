#!/usr/bin/env python3
"""Приводит дамп схемы MySQL к каноническому виду для сравнения.

Зачем. `mysqldump` печатает индексы в порядке их создания. Миграция создаёт их
одним порядком, `metadata.create_all` — другим, и дампы расходятся, хотя схемы
одинаковые. Сравнение начинает шуметь, а шумное сравнение перестают читать.

Что делается:

* выбрасываются комментарии, директивы `/*! ... */` и пустые строки;
* убирается `AUTO_INCREMENT=N` — он зависит от числа прошедших строк, а не от
  устройства схемы;
* выбрасывается служебная таблица `alembic_version` — её ведёт сам Alembic,
  в моделях её нет;
* внутри `CREATE TABLE` строки тела сортируются, а завершающие запятые
  снимаются.

Почему сортируются и колонки тоже. `ALTER TABLE ... ADD COLUMN` дописывает
колонку в конец таблицы, а `metadata.create_all` ставит её туда, где она
объявлена в модели. Физический порядок колонок в MySQL не влияет ни на что,
пока к ним обращаются по имени, — а SQLAlchemy всегда обращается по имени.
Сравнивать порядок значит получать красный гейт на каждой второй миграции;
красный без причины гейт перестают читать.

Что при этом **не** теряется: состав колонок, их типы, NULL/NOT NULL,
умолчания, коллации, индексы и внешние ключи. Переименованная, потерянная или
изменённая колонка по-прежнему видна в сравнении — меняется только строка,
а не её место.

Использование:
    normalize-schema.py < dump.sql > canonical.sql
"""

from __future__ import annotations

import re
import sys

AUTO_INCREMENT = re.compile(r" AUTO_INCREMENT=\d+")
DIRECTIVE = re.compile(r"^/\*!.*\*/;?$")


def canonical(lines: list[str]) -> list[str]:
    out: list[str] = []
    body: list[str] = []
    inside = False
    skipping = False

    def flush() -> None:
        # Запятые снимаются до сортировки. Иначе строка, стоявшая последней и
        # потому без запятой, после сортировки оказывается в середине — и две
        # одинаковые схемы расходятся на пустом месте.
        out.extend(sorted(line.rstrip(",") for line in body))
        body.clear()

    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        if line.startswith("--") or DIRECTIVE.match(line.strip()):
            continue

        line = AUTO_INCREMENT.sub("", line)

        # Служебная таблица Alembic — от `DROP TABLE` до закрывающей скобки.
        if "`alembic_version`" in line and line.startswith("DROP TABLE"):
            skipping = True
            continue
        if skipping:
            if line.startswith(") ENGINE="):
                skipping = False
            continue

        if line.startswith("CREATE TABLE"):
            inside = True
            out.append(line)
            continue

        if inside and line.startswith(")"):
            flush()
            out.append(line)
            inside = False
            continue

        if inside:
            body.append(line)
            continue

        out.append(line)

    return out


def main() -> int:
    lines = sys.stdin.read().splitlines(keepends=True)
    result = canonical(lines)
    if not result:
        print("normalize-schema: на входе пусто", file=sys.stderr)
        return 3
    sys.stdout.write("\n".join(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
