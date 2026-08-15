"""Токен заказа: форма, алфавит без похожих букв, разбор ввода, уникальность.

Токен — единственное, что связывает покупателя, его переписку и работу
администратора. Его диктуют голосом, пересылают скриншотом и набирают руками
на телефоне. Поэтому здесь проверяется не «функция что-то вернула», а три
свойства, на которых держится вся выдача:

1. токен нельзя перепутать при диктовке (алфавит без 0/O/1/I/L);
2. кривой ввод покупателя всё равно доезжает до канонического вида;
3. занятый токен никогда не выдаётся второму заказу, и попытка подобрать
   свободный не может подвесить бота.
"""

from __future__ import annotations

import random
import re

import pytest

from bot.services.tokens import (
    ALPHABET,
    GROUP,
    GROUPS,
    LENGTH,
    TokenCollision,
    generate,
    generate_unique,
    is_valid,
    normalize,
)

# --- алфавит ----------------------------------------------------------------

CONFUSABLE = ("0", "O", "1", "I", "L")
"""Ноль/«О» и единица/«I»/«L» — классическая пара ошибок при диктовке и наборе."""


@pytest.mark.parametrize("char", CONFUSABLE)
def test_alphabet_has_no_confusable_characters(char: str) -> None:
    """Каждый из этих символов обязан отсутствовать в алфавите поимённо.

    Проверка именно посимвольная, а не «длина алфавита 31»: длину легко
    сохранить, случайно вернув «O» и выкинув что-то другое. Токен,
    продиктованный по телефону, должен доехать без переспрашиваний, поэтому
    любой возврат этой пятёрки — регресс, который нужно заметить сразу.
    """
    assert char not in ALPHABET


def test_alphabet_has_no_duplicates() -> None:
    """Дубль в алфавите молча перекашивает распределение случайных символов."""
    assert len(set(ALPHABET)) == len(ALPHABET)


def test_alphabet_is_upper_case_and_alphanumeric() -> None:
    """Регистр и отсутствие знаков препинания — то, на что опирается normalize.

    Появись в алфавите дефис или пробел, разбор `XXXX-XXXX` перестал бы быть
    однозначным, а нижний регистр сломал бы приведение через .upper().
    """
    assert ALPHABET == ALPHABET.upper()
    assert all(char.isalnum() for char in ALPHABET)


# --- форма ------------------------------------------------------------------


def test_generate_has_grouped_shape() -> None:
    """Форма XXXX-XXXX: дефис ровно посередине и ничего лишнего по краям."""
    token = generate()
    assert len(token) == LENGTH + 1
    head, sep, tail = token[:GROUP], token[GROUP], token[GROUP + 1 :]
    assert sep == "-"
    assert len(head) == GROUP and len(tail) == GROUP
    assert token.count("-") == GROUPS - 1


def test_generate_uses_only_alphabet() -> None:
    """Ни одного символа вне алфавита — иначе весь смысл алфавита пропадает."""
    for _ in range(200):
        assert set(generate().replace("-", "")) <= set(ALPHABET)


TOKEN_SHAPE = re.compile(r"[0-9A-Z]{4}-[0-9A-Z]{4}")
"""Форма записана буквально, а не через GROUP и LENGTH.

Тест, выраженный только в константах модуля, тавтологичен: поменяй `GROUP` на
3 — и «len(token) == LENGTH + 1» останется верным для токена `XXX-XXX`, потому
что вместе с токеном поедет и эталон. Здесь эталон зафиксирован отдельно от
реализации: девять символов, две группы по четыре, именно так токен попадает
в карточку заказа и в сообщение покупателю.
"""


def test_generated_token_matches_the_printed_shape() -> None:
    """Восемь символов двумя группами — ровно то, что видит покупатель."""
    token = generate()
    assert TOKEN_SHAPE.fullmatch(token), token
    assert len(token) == 9


def test_token_space_is_large_enough_for_five_attempts_to_be_enough() -> None:
    """Пространство токенов должно быть несопоставимо больше числа заказов.

    `generate_unique` делает всего пять попыток и после этого роняет выдачу
    исключением. Такой бюджет держится ровно на том, что занятых токенов
    исчезающе мало по сравнению со всеми возможными. Урежь алфавит или длину —
    и коллизии станут регулярными: сначала редкие TokenCollision в момент
    оплаты, потом отказ уникального индекса `uq_orders_token`.
    """
    assert len(set(ALPHABET)) ** LENGTH > 10**11


def test_seeding_random_does_not_make_the_next_token_predictable() -> None:
    """Токен берётся из криптостойкого источника, а не из random.

    Подмена `secrets.choice` на `random.choice` не ломает ни одну проверку
    формы: токены остаются валидными и разными. Но `random` засевается
    предсказуемо (временем, а в тестах и в скриптах — руками), и тогда
    последовательность выданных токенов воспроизводится со стороны. Токен —
    это то, что покупатель диктует администратору вместо номера заказа;
    угадав чужой, можно попросить работу по чужой оплате.
    """
    try:
        random.seed(20240517)
        first = [generate() for _ in range(4)]
        random.seed(20240517)
        second = [generate() for _ in range(4)]
    finally:
        # Возвращаем модулю системную энтропию: соседние тесты не должны
        # унаследовать зерно, выставленное здесь.
        random.seed()

    assert first != second


def test_every_position_of_the_token_uses_the_whole_alphabet() -> None:
    """Каждая позиция случайна по всему алфавиту, а не по его куску.

    «Все символы внутри алфавита» — условие слишком слабое: генератор,
    берущий половину алфавита или ставящий постоянный префикс, его проходит,
    а энтропия падает в разы. Именно на энтропию опираются и пять попыток
    подбора, и невозможность угадать чужой токен, поэтому проверяется
    посимвольно и по каждой позиции отдельно.
    """
    seen: dict[int, set[str]] = {position: set() for position in range(LENGTH)}
    for _ in range(1500):
        raw = generate().replace("-", "")
        for position, char in enumerate(raw):
            seen[position].add(char)

    for position, chars in seen.items():
        missing = sorted(set(ALPHABET) - chars)
        assert not missing, f"позиция {position} никогда не принимает {missing}"


def test_two_thousand_tokens_are_all_distinct_and_valid() -> None:
    """Массовая выдача: две тысячи токенов подряд — все разные и все валидные.

    Это ловит две ошибки, которые поодиночке выглядят безобидно: генератор,
    засеянный одним зерном (тогда пойдут повторы) и рассинхрон между generate
    и is_valid (тогда собственный токен не пройдёт собственную же проверку и
    покупатель не сможет найти по нему заказ).
    """
    tokens = [generate() for _ in range(2000)]
    assert len(set(tokens)) == 2000
    assert all(is_valid(token) for token in tokens)


# --- is_valid ---------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",  # пустой ввод
        "   ",  # только пробелы
        "23ABCDEF",  # без дефиса
        "23-AB-CDEF",  # лишний дефис
        "23AB-CDEF-",  # хвостовой дефис даёт пустую третью группу
        "23AB-CDE",  # короткая вторая группа
        "23AB-CDEFG",  # длинная вторая группа
        "23A-BCDEF",  # дефис не посередине
        "23AB-CDE0",  # ноль — символ вне алфавита
        "23AB-CDEO",  # «O» — символ вне алфавита
        "23AB-CDE1",  # единица — символ вне алфавита
        "23AB-CD F",  # пробел внутри группы
        "23AB_CDEF",  # подчёркивание вместо дефиса
    ],
)
def test_is_valid_rejects_malformed(bad: str) -> None:
    """Всё, что не `XXXX-XXXX` из разрешённых символов, — не токен.

    is_valid стоит перед походом в базу: пропущенный сюда мусор превращается
    в лишний запрос и в «заказ не найден» вместо честного «это не токен».
    """
    assert is_valid(bad) is False


@pytest.mark.parametrize(
    "spelling",
    [
        "23 AB-CD EF",  # пробелы внутри групп
        "23AB - CDEF",  # пробелы вокруг дефиса
        "2 3AB-CDEF",
        "23AB-CD EF",
    ],
)
def test_is_valid_rejects_spellings_that_differ_from_their_canonical_form(
    spelling: str,
) -> None:
    """is_valid не должен одобрять то, что normalize ещё только собирается починить.

    Эти строки `normalize` приводит к `23AB-CDEF`, и соблазн научить is_valid
    тоже выкидывать пробелы велик. Делать этого нельзя: is_valid отвечает на
    вопрос «эта строка уже канонический токен?», и «да» на строку с пробелами
    означает поиск в базе по строке, которой там нет, — покупателю покажут
    «заказ не найден» вместо «поправьте ввод». Чинить ввод — работа normalize,
    и её результат обязан проходить is_valid уже без оговорок.
    """
    assert is_valid(spelling) is False
    assert is_valid(normalize(spelling)) is True


def test_is_valid_accepts_generated_token() -> None:
    assert is_valid(generate()) is True


def test_is_valid_ignores_surrounding_whitespace() -> None:
    """Пробелы по краям приезжают из копипаста и не должны ничего ломать."""
    assert is_valid("  23AB-CDEF  ") is True


def test_is_valid_accepts_lower_case() -> None:
    """Нижний регистр принимается намеренно — люди набирают токен как удобно.

    is_valid приводит ввод к верхнему регистру сам. Убрать это приведение —
    значит начать отказывать покупателю, который набрал свой же токен строчными
    буквами; такой отказ выглядит как «токен неправильный» и гонит человека в
    поддержку. Тест держит именно эту снисходительность.
    """
    assert is_valid("23ab-cdef") is True
    assert is_valid("23Ab-CdEf") is True


# --- normalize --------------------------------------------------------------


@pytest.mark.parametrize(
    "written",
    [
        "23AB-CDEF",  # уже канон
        "23ab-cdef",  # строчными
        "23ABCDEF",  # без дефиса
        "23abcdef",  # без дефиса и строчными
        "23AB CDEF",  # пробел вместо дефиса
        "23ab cdef",  # пробел и строчные
        "  23ab-cdef  ",  # с копипастными пробелами по краям
        "23 AB CD EF",  # разбито пробелами как попало
    ],
)
def test_normalize_brings_everything_to_one_canonical_form(written: str) -> None:
    """Все способы записать один токен обязаны сойтись в одну строку.

    По нормализованному значению ищется заказ. Разойдись формы — покупатель
    с тем же самым токеном получит «не найдено» просто потому, что поставил
    пробел вместо дефиса.
    """
    assert normalize(written) == "23AB-CDEF"


def test_normalize_is_idempotent() -> None:
    """Повторная нормализация не должна ничего добавлять или отрезать.

    Нормализуют часто в двух местах подряд (при вводе и перед запросом);
    если функция не идемпотентна, второй проход тихо испортит токен.
    """
    once = normalize("23ab cdef")
    assert normalize(once) == once


def test_normalize_leaves_wrong_length_alone_instead_of_guessing() -> None:
    """Строку не той длины normalize не режет и не дополняет — только регистр.

    Домысливать чужой ввод опаснее, чем вернуть его как есть: обрезанные до
    восьми символов «почти токены» дали бы совпадение с чужим заказом.
    Отбраковка — работа is_valid, и она должна оставаться возможной.
    """
    assert normalize("23ab") == "23AB"
    assert normalize("23ab-cdef-ghjk") == "23AB-CDEF-GHJK"
    assert is_valid(normalize("23ab")) is False


def test_normalized_generated_token_stays_itself() -> None:
    """Свой же токен нормализация обязана оставить нетронутым."""
    token = generate()
    assert normalize(token) == token


# --- generate_unique --------------------------------------------------------


class _Registry:
    """Заглушка «занятых» токенов: считает вызовы и помнит, что спрашивали."""

    def __init__(self, answers: list[bool]) -> None:
        self._answers = answers
        self.asked: list[str] = []

    async def exists(self, token: str) -> bool:
        self.asked.append(token)
        # Список ответов может кончиться раньше — тогда считаем токен свободным.
        index = len(self.asked) - 1
        return self._answers[index] if index < len(self._answers) else False


async def test_generate_unique_returns_first_free_token() -> None:
    """Свободный с первой попытки — значит ровно один поход в базу.

    Лишние проверки — это лишние запросы на каждой выдаче токена, а выдача
    происходит внутри транзакции оплаты, где каждая миллисекунда держит
    блокировку.
    """
    registry = _Registry([False])
    token = await generate_unique(registry.exists)

    assert len(registry.asked) == 1
    assert registry.asked[0] == token
    assert is_valid(token) is True


async def test_generate_unique_skips_taken_tokens() -> None:
    """Два занятых подряд — отдаётся третий, а не первый попавшийся.

    Главное здесь: возвращается именно тот кандидат, про который база сказала
    «свободен». Верни функция первый сгенерированный — два заказа получили бы
    один номер, и работу сделали бы не тому человеку.
    """
    registry = _Registry([True, True, False])
    token = await generate_unique(registry.exists)

    assert len(registry.asked) == 3
    assert token == registry.asked[2]
    assert token not in registry.asked[:2]


async def test_generate_unique_gives_up_instead_of_looping_forever() -> None:
    """Когда свободных нет, функция сдаётся ровно после attempts попыток.

    Проверяется не только исключение, но и число обращений: цикл без
    ограничения при переполненном пространстве токенов повесил бы обработчик
    оплаты навсегда, а деньги у покупателя уже списаны.
    """
    registry = _Registry([True] * 100)

    with pytest.raises(TokenCollision):
        await generate_unique(registry.exists, attempts=3)

    assert len(registry.asked) == 3


async def test_generate_unique_default_attempts_are_bounded() -> None:
    """Без явного attempts число попыток тоже конечно."""
    registry = _Registry([True] * 100)

    with pytest.raises(TokenCollision):
        await generate_unique(registry.exists)

    assert len(registry.asked) == 5


async def test_generate_unique_tries_a_new_token_each_attempt() -> None:
    """Каждая попытка — новый кандидат, а не повтор одного и того же.

    Перегенерируй функция один и тот же токен, «пять попыток» превратились бы
    в одну, и коллизия стала бы неразрешимой при живых свободных вариантах.
    """
    registry = _Registry([True] * 100)

    with pytest.raises(TokenCollision):
        await generate_unique(registry.exists, attempts=5)

    assert len(set(registry.asked)) == 5
    assert all(is_valid(candidate) for candidate in registry.asked)
