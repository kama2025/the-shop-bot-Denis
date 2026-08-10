"""Разбор пачки позиций склада."""

from __future__ import annotations

from bot.services.stock_input import parse_batch, preview


def test_single_line_items() -> None:
    parsed = parse_batch("ссылка-1\n\nссылка-2\n\nссылка-3")
    assert parsed.items == ["ссылка-1", "ссылка-2", "ссылка-3"]
    assert parsed.duplicates == []


def test_multiline_item_is_one_position() -> None:
    raw = "login: a@b.c\npass: qwerty\nСрок: 18 мес.\n\nссылка-2"
    parsed = parse_batch(raw)
    assert parsed.count == 2
    assert parsed.items[0].splitlines()[0] == "login: a@b.c"
    assert parsed.items[0].splitlines()[-1] == "Срок: 18 мес."


def test_duplicates_are_dropped() -> None:
    """Две одинаковые ссылки — почти всегда промах при копировании.

    Выданная дважды позиция превращается в жалобу и возврат, поэтому дубликат
    отбрасывается, а админу показывают, сколько отброшено.
    """
    parsed = parse_batch("одна\n\nодна\n\nдве")
    assert parsed.items == ["одна", "две"]
    assert len(parsed.duplicates) == 1


def test_extra_blank_lines_and_spaces() -> None:
    parsed = parse_batch("\n\n  первая  \n\n\n\n   вторая\n\n  ")
    assert parsed.items == ["первая", "вторая"]


def test_empty_input() -> None:
    assert parse_batch("").count == 0
    assert parse_batch("   \n\n  ").count == 0


def test_single_item_without_separator() -> None:
    parsed = parse_batch("одна-единственная-ссылка")
    assert parsed.items == ["одна-единственная-ссылка"]


def test_preview_is_bounded() -> None:
    items = [f"позиция-{i}" for i in range(20)]
    text = preview(items, limit=5)
    assert text.count("\n") == 5  # пять позиций плюс строка «и ещё»
    assert "и ещё 15" in text


def test_preview_flattens_multiline() -> None:
    text = preview(["login: a\npass: b"])
    assert "\n" not in text
    assert "⏎" in text
