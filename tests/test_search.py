"""Поиск товаров по названию: опечатки, перестановка слов, забытая раскладка.

Проверяется наблюдаемое поведение — какие id и в каком порядке вернулись, —
а не устройство весов. Конкретные числа rapidfuzz намеренно нигде не
зашиты: подтянут библиотеку, веса поедут, а обещания магазину останутся те же.
"""

from __future__ import annotations

import pytest

from bot.services.search import DEFAULT_THRESHOLD, normalize, rank, switch_layout

# Каталог подписочного магазина: русские и латинские названия вперемешку —
# так он и выглядит у заказчика.
CATALOG: list[tuple[int, str]] = [
    (1, "Нетфликс Премиум 12 месяцев"),
    (2, "Нетфликс"),
    (3, "Spotify Premium"),
    (4, "YouTube Premium 6 месяцев"),
]


# --- нормализация ---------------------------------------------------------


def test_normalize_strips_case_punctuation_and_extra_spaces() -> None:
    assert normalize("  Netflix —  12 мес.  ") == "netflix 12 мес"


def test_normalize_maps_yo_to_e() -> None:
    """«Ёжик» и «ежик» покупатель набирает как попало."""
    assert normalize("ЁЖИК") == normalize("ежик") == "ежик"


# --- раскладка ------------------------------------------------------------


def test_switch_layout_latin_to_cyrillic() -> None:
    assert switch_layout("Ytnabrc") == "Нетфикс"


def test_switch_layout_cyrillic_to_latin() -> None:
    assert switch_layout("туеадшч") == "netflix"


def test_switch_layout_is_reversible() -> None:
    """Таблица работает в обе стороны, значит два прогона дают исходную строку."""
    assert switch_layout(switch_layout("нетфликс")) == "нетфликс"
    assert switch_layout(switch_layout("Netflix")) == "Netflix"


# --- ранжирование ---------------------------------------------------------


def test_typo_still_finds_product() -> None:
    """«нетфликc» — латинская c вместо кириллической, глазом не отличить."""
    items = [(10, "Spotify Premium"), (11, "Нетфликс")]
    assert rank("нетфликc", items) == [11]


def test_swapped_words_still_find_product() -> None:
    """Покупатель называет товар своими словами, порядок слов не совпадает."""
    items = [(10, "Spotify Premium"), (11, "Нетфликс Премиум")]
    assert rank("премиум нетфликс", items) == [11]


def test_forgotten_layout_latin_keys() -> None:
    """«Ytnabrc» — это «Нетфикс», набранный при английской раскладке."""
    items = [(10, "Spotify Premium"), (11, "Нетфликс")]
    assert rank("Ytnabrc", items) == [11]


def test_forgotten_layout_cyrillic_keys() -> None:
    """Обратная сторона: «туеадшч» — это «netflix», набранный в ЙЦУКЕН."""
    items = [(10, "Spotify Premium"), (11, "Netflix Premium 12 месяцев")]
    assert rank("туеадшч", items) == [11]


def test_case_and_yo_are_ignored() -> None:
    items = [(7, "ежик")]
    assert rank("ЁЖИК", items) == [7]


def test_alien_query_returns_nothing() -> None:
    """Чужой запрос лучше вернуть пустым, чем случайным товаром."""
    assert rank("холодильник", CATALOG) == []


@pytest.mark.parametrize("query", ["", "   ", "!!!", "—"])
def test_empty_query_returns_nothing(query: str) -> None:
    """Пустой запрос — это не «показать всё»."""
    assert rank(query, CATALOG) == []


def test_exact_match_outranks_longer_similar_name() -> None:
    """Короткое точное совпадение обязано быть выше длинного похожего.

    По token_set_ratio «Netflix Premium 12 месяцев» получает те же 100, что и
    «Netflix», и при устойчивой сортировке всплыло бы первым просто потому,
    что раньше стоит в списке. Искали именно «Netflix».
    """
    items = [(1, "Netflix Premium 12 месяцев"), (2, "Netflix")]
    assert rank("Netflix", items)[0] == 2


def test_exact_match_outranks_perfect_fuzzy_match() -> None:
    """Первый проход обязан обгонять второй, а не только совпадать с ним.

    «Премиум Нетфликс» — та же перестановка слов, token_set_ratio даёт ей
    честные 100, столько же, сколько точному вхождению. Названия одной длины,
    перестановка идёт в списке первой — если проходы не разделены, наверх
    всплывёт она, а искали «Нетфликс Премиум» дословно.
    """
    items = [(1, "Премиум Нетфликс"), (2, "Нетфликс Премиум")]
    assert rank("нетфликс премиум", items)[0] == 2


def test_limit_caps_result_length() -> None:
    items = [(index, f"Нетфликс Премиум {index}") for index in range(1, 11)]
    assert len(rank("нетфликс", items, limit=3)) == 3


def test_equal_weight_keeps_input_order() -> None:
    """При равном весе порядок items не перемешивается."""
    items = [(1, "Нетфликс Премиум"), (2, "Нетфликс Премиум")]
    assert rank("нетфликс премиум", items) == [1, 2]


def test_threshold_is_what_keeps_the_catalog_out() -> None:
    """Отдельная защита: выдачу режет именно порог, а не везение с весами.

    Если порог однажды снимут или уронят в ноль, поиск начнёт возвращать весь
    каталог на любой запрос — «Холодильник» в ответ на «нетфликс» никто не
    заметит на глаз, а конверсия просядет. Тест фиксирует оба состояния:
    при threshold=0 чужой товар в выдаче есть, при штатном пороге — нет.
    """
    items = [(2, "Нетфликс"), (99, "Холодильник Атлант двухкамерный")]

    assert rank("нетфликс", items, threshold=0) == [2, 99]
    assert rank("нетфликс", items, threshold=DEFAULT_THRESHOLD) == [2]
