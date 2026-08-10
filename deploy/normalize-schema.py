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
* внутри `CREATE TABLE` **порядок колонок сохраняется** (он значим), а строки
  ключей и ограничений сортируются.

Порядок колонок намеренно не трогаем: переставленная колонка — настоящее
расхождение схем, и прятать его канонизацией нельзя.

Использование:
    normalize-schema.py < dump.sql > canonical.sql
"""

from __future__ import annotations

import re
import sys

AUTO_INCREMENT = re.compile(r" AUTO_INCREMENT=\d+")
DIRECTIVE = re.compile(r"^/\*!.*\*/;?$")
KEY_LINE = re.compile(r"^\s+(PRIMARY KEY|UNIQUE KEY|KEY|CONSTRAINT|FULLTEXT KEY)\b")


def canonical(lines: list[str]) -> list[str]:
    out: list[str] = []
    columns: list[str] = []
    keys: list[str] = []
    inside = False
    skipping = False

    def flush() -> None:
        out.extend(columns)
        out.extend(sorted(keys))
        columns.clear()
        keys.clear()

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
            # Последняя строка тела заканчивается запятой, которой быть не должно.
            if out and out[-1].endswith(","):
                out[-1] = out[-1][:-1]
            out.append(line)
            inside = False
            continue

        if inside:
            (keys if KEY_LINE.match(line) else columns).append(line)
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
