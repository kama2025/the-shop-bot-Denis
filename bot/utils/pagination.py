"""Постраничный вывод списков."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Page[T]:
    items: list[T]
    page: int
    pages: int
    total: int

    @property
    def is_empty(self) -> bool:
        return not self.items


def paginate(items: list[T], page: int, per_page: int = 8) -> Page[T]:
    total = len(items)
    pages = max(1, (total + per_page - 1) // per_page)
    # Страницу из середины могли открыть, а потом удалить половину списка —
    # тогда номер выходит за границы, и без зажима пользователь видит пустоту
    # вместо последней страницы.
    page = max(0, min(page, pages - 1))
    start = page * per_page
    return Page(items=items[start : start + per_page], page=page, pages=pages, total=total)
