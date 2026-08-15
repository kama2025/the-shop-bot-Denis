"""Поиск товаров по названию: ранжирование по похожести на запрос.

Покупатель ищет «нетфликc» с латинской c, «премиум нетфликс» задом наперёд
или вовсе «Ytnabrc», забыв переключить раскладку. Поиск по LIKE находит
только того, кто набрал название точь-в-точь, поэтому сравниваем нечётко.

Работы с базой здесь нет: на вход приходит готовый список (id, название), на
выход — id по убыванию похожести. Так модуль проверяется тестами без СУБД, а
вызывающий сам решает, чем и откуда достать каталог.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

DEFAULT_THRESHOLD = 60

# Вес точного вхождения. Ровно потолок token_set_ratio: выше подниматься
# некуда, поэтому проходы разводятся отдельным ключом сортировки, а не весом.
_EXACT_SCORE = 100.0

# Всё, что не буква и не цифра, — в пробел; повторы схлопываются сразу.
_NON_WORD = re.compile(r"[\W_]+")

# Клавиша к клавише: qwerty ↔ йцукен. Пара «/» ↔ «.» сознательно опущена —
# она столкнулась бы с «.» ↔ «ю» в общей таблице, а знаки препинания
# нормализация всё равно превращает в пробелы.
_QWERTY = "qwertyuiop[]asdfghjkl;'zxcvbnm,.`"
_JCUKEN = "йцукенгшщзхъфывапролджэячсмитьбюё"

_LAYOUT: dict[str, str] = {}
for _lat, _cyr in zip(_QWERTY, _JCUKEN):
    _LAYOUT[_lat] = _cyr
    _LAYOUT[_cyr] = _lat
    # Регистр добавляем только там, где он есть: «[».upper() — это «[», и
    # такая запись затёрла бы строчную «х» прописной «Х».
    if _lat.upper() != _lat:
        _LAYOUT[_lat.upper()] = _cyr.upper()
    if _cyr.upper() != _cyr:
        _LAYOUT[_cyr.upper()] = _lat.upper()


def normalize(text: str) -> str:
    """Приводит строку к виду для сравнения.

    Нижний регистр, ё → е, знаки препинания → пробел, лишние пробелы убраны.
    «Ё» и «е» покупатель набирает как попало, а тире в «Netflix — 12 мес.»
    не должно мешать совпадению.
    """
    if not text:
        return ""
    lowered = text.lower().replace("ё", "е")
    return _NON_WORD.sub(" ", lowered).strip()


def switch_layout(text: str) -> str:
    """Переводит строку между русской и английской раскладками."""
    if not text:
        return ""
    return "".join(_LAYOUT.get(char, char) for char in text)


def rank(
    query: str,
    items: list[tuple[int, str]],
    threshold: int = DEFAULT_THRESHOLD,
    limit: int = 30,
) -> list[int]:
    """Возвращает id товаров по убыванию похожести. Порядок items при равном
    весе сохраняется (устойчивая сортировка)."""
    variants = _query_variants(query)
    if not variants or limit <= 0:
        return []

    ranked: list[tuple[float, int, int, int, int]] = []
    for index, (item_id, title) in enumerate(items[:limit]):
        title_norm = normalize(title)
        if not title_norm:
            continue

        is_exact, score = _best_score(variants, title_norm)
        if score < threshold:
            continue

        # Точное вхождение выше нечёткого даже при равном весе: token_set_ratio
        # отдаёт те же 100 за перестановку слов и за название, целиком
        # накрывающее запрос.
        pass_rank = 0 if is_exact else 1
        # Среди точных вхождений короткое название ближе к запросу: «Netflix» —
        # ровно то, что искали, «Netflix Premium 12 месяцев» — уже больше.
        # Для нечётких проходов ключ постоянный, там решает порядок items.
        length_rank = len(title_norm) if is_exact else 0
        ranked.append((-score, pass_rank, length_rank, index, item_id))

    # index в ключе стоит перед item_id, поэтому при полном равенстве порядок
    # берётся из items, а не из значений id.
    ranked.sort()
    return [row[-1] for row in ranked]


def _query_variants(query: str) -> list[str]:
    """Нормализованный запрос и его вариант в другой раскладке.

    Пробуем оба: если покупатель забыл переключить раскладку, осмысленным
    окажется только перевёрнутый вариант, а какой именно — заранее неизвестно.
    """
    variants: list[str] = []
    for candidate in (normalize(query), normalize(switch_layout(query))):
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


def _best_score(variants: list[str], title_norm: str) -> tuple[bool, float]:
    """Лучший вес названия по всем вариантам запроса: (точное вхождение, вес).

    Два прохода. Первый — точное вхождение запроса в название: такой товар
    обязан быть наверху, иначе короткое совпадение тонет под длинным мусором
    с похожим весом. Второй — нечёткое сравнение по множествам слов.
    """
    best_score = 0.0
    for variant in variants:
        if variant in title_norm:
            return True, _EXACT_SCORE  # выше уже не будет, дальше не считаем
        best_score = max(best_score, float(fuzz.token_set_ratio(variant, title_norm)))
    return False, best_score
