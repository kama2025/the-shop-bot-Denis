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


# Кириллица → латиница. Нужна не для красоты: магазин продаёт иностранные
# сервисы русским покупателям, и «нетфликс», «чатгпт», «спотифай» — это самый
# частый вид запроса. Переключение раскладки здесь не помогает: оно переводит
# «Ytnabrc» в «нетфликс», а до «Netflix» остаётся ровно этот шаг.
#
# Таблица нарочно грубая и односторонняя. Задача — не транскрипция, а сведение
# обоих написаний к одной строке, которую дальше сравнивает нечёткий поиск;
# «netfliks» против «netflix» он разбирает уверенно.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y",
    "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def translit(text: str) -> str:
    """Сводит кириллицу к латинице. Латиница проходит насквозь.

    Применяется и к запросу, и к названию товара — тогда «нетфликс» и
    «Netflix» встречаются в одной форме независимо от того, что из них
    на каком языке написано.
    """
    if not text:
        return ""
    return "".join(_TRANSLIT.get(char, char) for char in text)


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
    for index, (item_id, title) in enumerate(items):
        title_norm = normalize(title)
        if not title_norm:
            continue

        title_forms = (title_norm,)
        title_translit = translit(title_norm)
        if title_translit != title_norm:
            title_forms += (title_translit,)

        is_exact, score = _best_score(variants, title_forms)
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
    return [row[-1] for row in ranked[:limit]]


def _query_variants(query: str) -> list[str]:
    """Нормализованный запрос и его вариант в другой раскладке.

    Пробуем оба: если покупатель забыл переключить раскладку, осмысленным
    окажется только перевёрнутый вариант, а какой именно — заранее неизвестно.
    """
    direct = normalize(query)
    if not direct:
        # Запрос из одних знаков препинания. Переворачивать его нельзя:
        # «.» в другой раскладке — это «ю», и поиск начнёт отвечать товарами
        # на запрос из одной точки.
        return []

    variants = [direct]
    switched = normalize(switch_layout(query))
    # Перевёрнутый вариант берём, только если перевод раскладки не съел букв.
    # Кириллические «ю», «б», «х», «ъ», «ж», «э», «ё» на латинской раскладке —
    # знаки препинания, и нормализация их срезает: «ютуб» превращается в
    # огрызок «ne», который оказывается подстрокой «Netflix» и забирает себе
    # честные 100 за точное вхождение. Настоящая забытая раскладка букв не
    # теряет — наоборот, добирает их из знаков препинания («gk.c» → «плюс»), —
    # поэтому такой вариант проходит.
    if switched and switched != direct and _word_length(switched) >= _word_length(direct):
        variants.append(switched)

    # Латинская форма каждого варианта: «нетфликс» → «netfliks». Ради неё же
    # латинизируется и название товара — встречаются они уже в одном алфавите.
    for variant in list(variants):
        latin = translit(variant)
        if latin and latin not in variants:
            variants.append(latin)
    return variants


def _word_length(normalized: str) -> int:
    """Сколько букв и цифр в нормализованной строке; пробелы не в счёт."""
    return len(normalized) - normalized.count(" ")


def _best_score(variants: list[str], title_forms: tuple[str, ...]) -> tuple[bool, float]:
    """Лучший вес названия по всем вариантам запроса: (точное вхождение, вес).

    Два прохода. Первый — точное вхождение запроса в название: такой товар
    обязан быть наверху, иначе короткое совпадение тонет под длинным мусором
    с похожим весом. Второй — нечёткое сравнение по множествам слов.

    `title_forms` — название в нескольких написаниях (как есть и в латинице).
    Сравниваются все пары «вариант запроса × форма названия»: заранее не
    известно, что из них на каком языке написано.
    """
    best_score = 0.0
    for variant in variants:
        for form in title_forms:
            if variant in form:
                return True, _EXACT_SCORE  # выше уже не будет, дальше не считаем
            best_score = max(best_score, _token_coverage(variant, form))
    return False, best_score


_SIGNIFICANT_TOKEN = 4
"""Слово короче этого считается служебным: «pro», «1», «мес», «на»."""


def _token_coverage(variant: str, form: str) -> float:
    """Каждое слово запроса ищет себе самое похожее слово в названии.

    Без этого прохода однословный запрос тонет в длинном названии:
    `token_set_ratio("netfliks", "netflix premium 1 mesyac")` низок не потому,
    что слова непохожи, а потому что в названии есть ещё три слова. Сравнение
    по словам эту разницу убирает — «netfliks» находит «netflix» и получает
    свои честные 93.

    Два правила, и второе важнее первого:

    * вес слова — его длина, иначе «1» и «мес» тянули бы результат наравне
      с основным словом;
    * **значимое слово, не нашедшее пары, снимает кандидата целиком.** Без
      этого «премиум нетфликс» находит Spotify Premium: слово «премиум»
      совпадает на сто, «нетфликс» — почти ни на сколько, а среднее всё равно
      проходит порог. Одно общее слово не делает товары похожими.
    """
    query_tokens = variant.split()
    title_tokens = form.split()
    if not query_tokens or not title_tokens:
        return 0.0

    weighted = 0.0
    total_weight = 0
    for token in query_tokens:
        best = max(float(fuzz.ratio(token, other)) for other in title_tokens)
        if len(token) >= _SIGNIFICANT_TOKEN and best < DEFAULT_THRESHOLD:
            return 0.0
        weighted += best * len(token)
        total_weight += len(token)
    return weighted / total_weight if total_weight else 0.0
