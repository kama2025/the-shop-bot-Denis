"""Разбор цены в мастере выкладки товара.

Цену вводит живой человек в чате Telegram, и вводит как придётся: со знаком
доллара, с запятой вместо точки, с пробелом в тысячах, иногда скопировав из
браузера вместе с неразрывным пробелом. Всё это должно превратиться в целые
центы — единственный формат, в котором цена дальше живёт (`Product.price_usd_cents`).

Обратная сторона: любая ошибка разбора попадает прямо в витрину. Цена,
округлившаяся до нуля, обнуляет и наценку (процент от нуля — ноль), то есть
товар начинает продаваться бесплатно; цена, разобранная в другом порядке
величин, снимет с покупателя не ту сумму. Поэтому разбор обязан либо дать
верное число, либо отказаться с ValueError — молчаливого «примерно» тут быть
не должно.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest

from bot.handlers.admin import product_wizard
from bot.handlers.admin.product_wizard import MAX_PRICE_USD, MAX_TITLE, parse_price_usd
from bot.services.access import Actor
from bot.states.admin import ProductWizardSG

NBSP = "\u00a0"
"""Неразрывный пробел: приезжает копипастом из браузера и из самого Telegram.

Записан escape-последовательностью намеренно: в исходнике он неотличим от
обычного пробела, и любая невнимательная правка или автоформаттер превратили
бы этот случай в дубль соседнего, ничего не проверяющий.
"""


# --- то, что должно разбираться ---------------------------------------------


@pytest.mark.parametrize(
    ("raw", "cents"),
    [
        ("20", 2000),  # целое число долларов
        ("19.99", 1999),  # точка как разделитель
        ("19,99", 1999),  # запятая — русская раскладка
        ("$20", 2000),  # знак валюты перед суммой
        ("$19,99", 1999),  # знак валюты и запятая вместе
        (" 20 ", 2000),  # пробелы по краям
        ("1 000", 100_000),  # обычный пробел в разряде тысяч
        (f"1{NBSP}000", 100_000),  # неразрывный пробел там же
        ("0.01", 1),  # ровно один цент — минимально допустимая цена
        ("19.90", 1990),  # незначащий ноль в копейках
        ("100000", 10_000_000),  # верхняя граница включительно
    ],
)
def test_parses_human_input_into_cents(raw: str, cents: int) -> None:
    """Все привычные способы написать цену дают одно и то же число центов.

    Каждый вариант здесь — реальный способ ввода, и потеря любого из них
    означает, что администратор получит «не понял цену» на совершенно
    нормальной строке и пойдёт подбирать формат наугад.
    """
    assert parse_price_usd(raw) == cents


def test_result_is_plain_int() -> None:
    """Ровно int, а не Decimal и не float.

    Значение уходит в `price_usd_cents` и дальше в целочисленную арифметику
    pricing, которая на не-int бросает TypeError. Decimal, «случайно» дошедший
    до базы, обнаружится только на первой покупке.
    """
    result = parse_price_usd("19,99")
    assert type(result) is int


def test_comma_and_dot_are_interchangeable() -> None:
    """Запятая и точка — один и тот же разделитель, а не разные числа.

    Если бы запятая просто выкидывалась, «19,99» превратилось бы в 1999
    долларов вместо 19.99 — ошибка в сто раз, которую в витрине заметят
    не сразу.
    """
    assert parse_price_usd("19,99") == parse_price_usd("19.99") == 1999


def test_thousands_separator_does_not_shift_the_amount() -> None:
    """Пробел в тысячах убирается, а не превращается в другое число."""
    assert parse_price_usd("1 000") == parse_price_usd("1000") == 100_000
    assert parse_price_usd(f"12{NBSP}345,67") == 1_234_567


@pytest.mark.parametrize(
    ("raw", "cents"),
    [
        ("$ 20", 2000),  # пробел между знаком и суммой
        (f"${NBSP}20", 2000),  # он же неразрывным
        ("$ 19,99", 1999),
    ],
)
def test_currency_sign_separated_by_a_space_is_still_a_price(raw: str, cents: int) -> None:
    """`$ 20` — то же самое, что `$20`.

    Знак снимается уже после чистки пробелов, и это не случайность: человек,
    копирующий цену из браузера, приносит её вместе с пробелом после знака.
    Переставь местами две операции в одной строке — и такой ввод начнёт
    получать «не понял цену», причём остальные одиннадцать форм записи
    продолжат работать, так что причину будут искать долго.
    """
    assert parse_price_usd(raw) == cents


@pytest.mark.parametrize(
    ("raw", "cents"),
    [
        ("0.006", 1),  # шесть тысячных доллара — это уже цент
        ("0.014", 1),  # округление вниз, но не отбрасывание
        ("0.019", 2),  # ближайший цент — второй, а не первый
        ("19.999", 2000),
        ("9.996", 1000),
    ],
)
def test_third_decimal_is_rounded_to_the_nearest_cent_not_dropped(
    raw: str, cents: int
) -> None:
    """Лишние знаки после запятой округляются, а не обрезаются.

    Разница видна только на третьем знаке, поэтому её легко внести правкой
    вида «int(value * 100) короче и понятнее»: все одиннадцать обычных форм
    записи такую замену переживают. Но обрезание — это систематическое
    занижение цены в пользу покупателя, а на `0.006` оно даёт ноль центов,
    то есть ровно тот бесплатный товар, от которого защищается проверка ниже.
    """
    assert parse_price_usd(raw) == cents


# --- то, что должно отвергаться ---------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "0",  # ноль
        "0.00",  # ноль с копейками
        "0,00",
        "-5",  # отрицательное
        "-19.99",
        "-0.01",
        "",  # пустая строка
        "   ",  # только пробелы
        "$",  # только знак валюты
        "abc",  # буквы
        "двадцать",  # буквы кириллицей
        "20 долларов",
        "20$",  # доллар справа — lstrip его не снимает
        ".",  # одна точка
        "19.9.9",  # два разделителя
        "--20",
    ],
)
def test_rejects_garbage_with_value_error(raw: str) -> None:
    """Мусор и неположительные суммы — только ValueError.

    Тип исключения тут не деталь: обработчик шага цены ловит именно ValueError
    и показывает администратору понятный текст. Любое другое исключение
    вылетит наружу, мастер оборвётся посреди выкладки, и все введённые шаги
    пропадут.
    """
    with pytest.raises(ValueError):
        parse_price_usd(raw)


def test_zero_price_is_refused_because_percent_markup_dies_on_it() -> None:
    """Ноль нельзя пропускать отдельным пунктом: наценка процентом от нуля — ноль.

    Товар с нулевой ценой не «дешёвый», а бесплатный при любой наценке и любом
    курсе, то есть магазин отдаёт работу даром и узнаёт об этом от покупателя.
    """
    with pytest.raises(ValueError):
        parse_price_usd("0")


@pytest.mark.parametrize("raw", ["0.001", "0.004", "0.0001"])
def test_sub_cent_amount_is_refused_not_silently_zeroed(raw: str) -> None:
    """Меньше цента — ошибка, а не тихий ноль.

    Округление до центов само по себе честное, но результат этого округления
    здесь равен нулю, а нулевая цена ломает расчёт наценки ровно так же, как
    введённый вручную ноль. Разница в том, что администратор ввёл «0.001» и
    уверен, что цена есть: если функция вернёт 0, товар молча уедет в каталог
    бесплатным. Поэтому проверка нуля обязана стоять ПОСЛЕ округления, а не
    только до него.
    """
    with pytest.raises(ValueError):
        parse_price_usd(raw)


@pytest.mark.parametrize("raw", ["100000.01", "100001", "999999", "1e9"])
def test_rejects_amount_above_limit(raw: str) -> None:
    """Выше MAX_PRICE_USD — отказ.

    Потолок ловит опечатку в разрядах (лишние нули при вводе), которая иначе
    доедет до заказа как настоящая цена и превратится в счёт на миллионы
    рублей после умножения на курс.
    """
    with pytest.raises(ValueError):
        parse_price_usd(raw)


def test_limit_boundary_is_inclusive() -> None:
    """Ровно потолок ещё принимается — граница не должна съезжать на цент."""
    assert parse_price_usd(str(MAX_PRICE_USD)) == int(MAX_PRICE_USD) * 100
    with pytest.raises(ValueError):
        parse_price_usd(str(MAX_PRICE_USD + Decimal("0.01")))


def test_error_message_is_not_empty() -> None:
    """У отказа должен быть текст: обработчик подставляет его в ответ админу.

    Сообщение вида «Не понял цену: .» ничего не объясняет, и человек начинает
    гадать, что именно не так с его вводом.
    """
    for raw in ("", "abc", "0", "1000000"):
        with pytest.raises(ValueError) as info:
            parse_price_usd(raw)
        assert str(info.value).strip()


# «nan» и «inf» — законные значения для Decimal, и сравнение NaN с нулём
# возбуждает InvalidOperation, а не ValueError. Хендлер ловит только ValueError,
# поэтому без явной отсечки мастер падал на шаге цены с трассировкой.
@pytest.mark.parametrize("raw", ["nan", "NaN", "-nan", "snan", "inf", "-inf", "Infinity"])
def test_nan_is_refused_like_any_other_word(raw: str) -> None:
    """«nan» — такой же набор букв для человека, как «abc», и отказ должен быть таким же."""
    with pytest.raises(ValueError):
        parse_price_usd(raw)


# --- сами шаги мастера ------------------------------------------------------
#
# Разбор цены — только один шаг из шести, и проверка одной чистой функции не
# держит главное обещание модуля: товар создаётся ТОЛЬКО на последнем шаге и
# только целиком. Половина товара в каталоге — это карточка без цены или без
# картинки, которую увидит покупатель, и починить её можно лишь удалением.
# Ниже — минимальные заглушки Telegram: настоящие Message и FSMContext сюда
# не нужны, хендлеру от них требуются четыре метода.


@dataclass
class FakeState:
    """FSM-состояние мастера: помнит данные и текущий шаг."""

    data: dict[str, Any] = field(default_factory=dict)
    state: Any = None
    cleared: int = 0

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)

    async def update_data(self, **values: Any) -> dict[str, Any]:
        self.data.update(values)
        return dict(self.data)

    async def set_state(self, state: Any = None) -> None:
        self.state = state

    async def clear(self) -> None:
        self.data.clear()
        self.state = None
        self.cleared += 1


@dataclass
class FakeMessage:
    """Сообщение от администратора и всё, что бот на него ответил."""

    text: str | None = None
    answers: list[str] = field(default_factory=list)

    async def answer(self, text: str, *_: Any, **__: Any) -> None:
        self.answers.append(text)

    async def answer_photo(self, file_id: str, caption: str = "", **__: Any) -> None:
        self.answers.append(caption)


@dataclass
class FakeCall:
    """Нажатие кнопки. `answer` без текста — обычное «часики убрать»."""

    data: str = "a:prod_save"
    answers: list[str] = field(default_factory=list)

    async def answer(self, text: str = "", *_: Any, **__: Any) -> None:
        self.answers.append(text)


FULL_WIZARD = {
    "wizard_category_id": 7,
    "wizard_title": "Прокачка аккаунта",
    "wizard_image_file_id": "file-42",
    "wizard_price_cents": 1999,
    "wizard_description": "Описание",
}

ADMIN = Actor(user_id=1, is_admin=True)
STRANGER = Actor(user_id=999, is_admin=False)


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Перехватывает создание товара: тест смотрит, что именно ушло в каталог."""
    calls: list[dict[str, Any]] = []

    async def fake_create(session: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return type(
            "Product",
            (),
            {
                "id": 101,
                "title": kwargs["title"],
                "category_id": kwargs["category_id"],
                "price_usd_cents": kwargs["price_usd_cents"],
            },
        )()

    async def fake_audit(*_: Any, **__: Any) -> None:
        return None

    async def fake_show(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(product_wizard.catalog_repo, "create_product", fake_create)
    # Подменяется именно `record` — то имя, которое в `bot/repo/audit.py` есть.
    # Заводить здесь несуществующий `write` нельзя: заглушка закрыла бы собой
    # ровно тот вызов, из-за которого раньше падало сохранение.
    monkeypatch.setattr(product_wizard.audit_repo, "record", fake_audit)
    monkeypatch.setattr(product_wizard, "show", fake_show)
    return calls


async def test_empty_title_does_not_move_the_wizard_forward() -> None:
    """Пустое название — отказ на месте, а не пустая карточка в каталоге.

    Название приходит из `message.text`, и у пересланного стикера или фото
    его нет вовсе. Пропусти шаг такой ввод — товар доедет до сохранения с
    пустым заголовком, и в списке категории появится строка ни о чём.
    """
    message = FakeMessage(text="   ")
    state = FakeState()

    await product_wizard.take_title(message, actor=ADMIN, state=state)

    assert state.data == {}
    assert state.state is None
    assert message.answers, "администратор обязан узнать, что название не принято"


async def test_overlong_title_is_refused_before_it_reaches_the_column() -> None:
    """Длиннее MAX_TITLE — отказ здесь, а не ошибка базы на последнем шаге.

    В колонке `Product.title` 255 символов. Без этой проверки мастер молча
    доводит человека до шестого шага и падает там на вставке — вместе со
    всем, что он ввёл до этого.
    """
    message = FakeMessage(text="я" * (MAX_TITLE + 1))
    state = FakeState()

    await product_wizard.take_title(message, actor=ADMIN, state=state)

    assert "wizard_title" not in state.data
    assert state.state is None
    assert message.answers

    ok = FakeMessage(text="я" * MAX_TITLE)
    await product_wizard.take_title(ok, actor=ADMIN, state=state)
    assert state.data["wizard_title"] == "я" * MAX_TITLE
    assert state.state == ProductWizardSG.image


async def test_price_step_ignores_everyone_except_admins() -> None:
    """Шаг цены не отвечает постороннему и не пишет ничего в его состояние.

    Проверка прав стоит в каждом хендлере отдельно — это решение записано в
    `handlers/admin/common`. Значит, и держать её нужно в каждом: снятая
    здесь, она не всплывёт нигде больше, а мастер начнёт принимать ввод от
    того, кого в админку не пускали.
    """
    message = FakeMessage(text="20")
    state = FakeState()

    await product_wizard.take_price(message, actor=STRANGER, state=state)

    assert state.data == {}
    assert state.state is None
    assert message.answers == []


async def test_price_step_keeps_the_wizard_open_on_a_bad_number() -> None:
    """Кривая цена не рушит мастер: шаг остаётся, введённое раньше цело.

    Здесь проверяется связка `parse_price_usd` + обработчик: любое исключение,
    кроме ValueError, пролетит мимо `except` и оборвёт мастер вместе со всеми
    четырьмя шагами, которые человек уже прошёл.
    """
    state = FakeState(data={"wizard_title": "Прокачка"}, state=ProductWizardSG.price)
    message = FakeMessage(text="двадцать")

    await product_wizard.take_price(message, actor=ADMIN, state=state)

    assert "wizard_price_cents" not in state.data
    assert state.data["wizard_title"] == "Прокачка"
    assert state.state == ProductWizardSG.price
    assert message.answers and message.answers[0].startswith("Не понял цену")


async def test_half_filled_wizard_creates_nothing(created: list[dict[str, Any]]) -> None:
    """Потерянное состояние — отказ, а не товар из того, что уцелело.

    Между шагами бот может перезапуститься, и в состоянии останется, скажем,
    только название. Собрать из него товар — значит выложить в каталог
    карточку без цены и без картинки: покупатель увидит её раньше, чем
    администратор поймёт, что мастер не дошёл до конца.
    """
    call = FakeCall()
    state = FakeState(
        data={"wizard_category_id": 7, "wizard_title": "Прокачка"},
        state=ProductWizardSG.confirm,
    )

    await product_wizard.save_product(call, session=None, actor=ADMIN, state=state)

    assert created == [], "товар не должен создаваться из половины данных"
    assert call.answers, "администратор обязан узнать, что данные потерялись"
    assert state.cleared == 1, "битое состояние должно сбрасываться"


async def test_only_admins_can_press_save(created: list[dict[str, Any]]) -> None:
    """Кнопку «Сохранить» может нажать кто угодно — callback_data не секрет."""
    call = FakeCall()
    state = FakeState(data=dict(FULL_WIZARD), state=ProductWizardSG.confirm)

    await product_wizard.save_product(call, session=None, actor=STRANGER, state=state)

    assert created == []
    assert state.data == FULL_WIZARD, "чужое нажатие не должно трогать состояние"


async def test_saved_product_carries_every_answer_the_wizard_collected(
    created: list[dict[str, Any]],
) -> None:
    """В каталог уходит ровно то, что ввёл человек, и в правильных полях.

    Цена уходит в центах и целым числом: делённая на сто «для читаемости» или
    попавшая не в то поле, она обнаружится не здесь, а в витрине у покупателя,
    который увидит $19.99 как $0.19 и купит работу за копейки.

    Известный баг с `audit_repo.write` (см. следующий тест) случается уже
    ПОСЛЕ создания товара, поэтому проверить содержимое вызова он не мешает —
    и глушится тут точечно, чтобы после починки этот тест не пришлось трогать.
    """
    call = FakeCall()
    state = FakeState(data=dict(FULL_WIZARD), state=ProductWizardSG.confirm)

    try:
        await product_wizard.save_product(call, session=None, actor=ADMIN, state=state)
    except AttributeError:
        pass

    assert len(created) == 1
    assert created[0] == {
        "category_id": 7,
        "title": "Прокачка аккаунта",
        "description": "Описание",
        "price_usd_cents": 1999,
        "image_file_id": "file-42",
    }
    assert type(created[0]["price_usd_cents"]) is int


async def test_save_confirms_and_closes_the_wizard(created: list[dict[str, Any]]) -> None:
    """Товар создан — значит мастер отвечает администратору и закрывается.

    Незакрытый мастер опаснее, чем кажется: состояние остаётся на шаге
    подтверждения со всеми данными, и следующее нажатие той же кнопки создаёт
    дубль товара. Поэтому сброс состояния — часть успешного сохранения, а не
    косметика.
    """
    call = FakeCall()
    state = FakeState(data=dict(FULL_WIZARD), state=ProductWizardSG.confirm)

    await product_wizard.save_product(call, session=None, actor=ADMIN, state=state)

    assert len(created) == 1
    assert call.answers, "администратор обязан увидеть подтверждение"
    assert state.cleared == 1, "после сохранения мастер обязан закрыться"
    assert state.data == {}


async def test_price_step_does_not_disguise_a_bug_as_a_bad_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ловится ровно ValueError, а не «любое исключение».

    Соблазн написать `except Exception` велик — мастер перестанет падать. Но
    падать он перестанет и на настоящей поломке: администратор увидит «Не
    понял цену: module has no attribute …» и будет переписывать совершенно
    верное число, пока не сдастся, а в логах вместо ошибки останется тишина.
    Поломка обязана дойти до обработчика ошибок, а не притвориться опечаткой
    человека.
    """

    def boom(_: str) -> int:
        raise TypeError("внутренняя поломка, не ввод человека")

    monkeypatch.setattr(product_wizard, "parse_price_usd", boom)
    message = FakeMessage(text="19.99")
    state = FakeState(state=ProductWizardSG.price)

    with pytest.raises(TypeError):
        await product_wizard.take_price(message, actor=ADMIN, state=state)

    assert message.answers == [], "поломку нельзя показывать как «не понял цену»"
