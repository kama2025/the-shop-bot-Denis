"""Разбор пачки позиций, которую админ присылает одним сообщением.

Формат согласован с заказчиком: позиции разделяются пустой строкой, внутри
позиции может быть сколько угодно строк. Это покрывает и «просто ссылка», и
«логин:пароль + срок», и инструкцию.

Отдельная функция, а не разбор внутри хендлера, потому что её нужно проверять
тестами: ошибка разбора превращается в потерянный или задвоенный товар.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_BLANK_LINE = re.compile(r"\n\s*\n")


@dataclass(frozen=True)
class ParsedBatch:
    items: list[str]
    duplicates: list[str]

    @property
    def count(self) -> int:
        return len(self.items)


def parse_batch(raw: str) -> ParsedBatch:
    """Разбирает текст на позиции.

    Дубликаты внутри одной пачки отсеиваются: две одинаковые ссылки почти
    всегда означают промах при копировании, а выданная дважды позиция — это
    жалоба и возврат.
    """
    if not raw or not raw.strip():
        return ParsedBatch(items=[], duplicates=[])

    chunks = [chunk.strip() for chunk in _BLANK_LINE.split(raw.strip())]
    chunks = [chunk for chunk in chunks if chunk]

    seen: set[str] = set()
    items: list[str] = []
    duplicates: list[str] = []
    for chunk in chunks:
        key = chunk.strip()
        if key in seen:
            duplicates.append(chunk)
            continue
        seen.add(key)
        items.append(chunk)
    return ParsedBatch(items=items, duplicates=duplicates)


def preview(items: list[str], limit: int = 5, width: int = 60) -> str:
    """Короткий предпросмотр пачки перед добавлением на склад."""
    if not items:
        return "—"
    lines = []
    for index, item in enumerate(items[:limit], start=1):
        flat = " ⏎ ".join(part.strip() for part in item.splitlines() if part.strip())
        if len(flat) > width:
            flat = flat[: width - 1] + "…"
        lines.append(f"{index}. {flat}")
    if len(items) > limit:
        lines.append(f"…и ещё {len(items) - limit}")
    return "\n".join(lines)
