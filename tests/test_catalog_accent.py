"""Акцентный цвет категории.

Цвет — не украшение, а поле, которое из базы попадает прямо в клавиатуру.
Telegram принимает ровно четыре значения (`default`, `primary`, `success`,
`danger`) и при любом другом отвергает **всю клавиатуру целиком**: сообщение
не уходит, у покупателя экран исчезает. Проверено пробой к живому Bot API —
`accent`, `warning`, `#FF5722` отклоняются с «invalid button style».

Отсюда единственное настоящее требование к этому коду: что бы ни лежало в
колонке `categories.accent`, кнопка обязана собраться.
"""

from __future__ import annotations

import pytest

from bot.db.models import Category, CategoryAccent, Product
from bot.keyboards import admin as admin_kb
from bot.keyboards import user as user_kb
from bot.keyboards.theme import ALLOWED_STYLES

RATE_KOP = 9000


def _category(accent: str | None = None) -> Category:
    return Category(id=7, title="Подписки", accent=accent, sort_order=10, is_active=True)


def _product() -> Product:
    return Product(
        id=42, category_id=7, title="Netflix", price_usd_cents=2000, sort_order=10, is_active=True
    )


def _styles(markup) -> list[str | None]:
    return [button.style for row in markup.inline_keyboard for button in row]


# --- допустимые значения ----------------------------------------------------


def test_every_accent_is_a_style_telegram_accepts() -> None:
    """Список цветов не может разойтись со списком стилей.

    Если в CategoryAccent появится пятое значение, оно попадёт в кнопку и
    уронит экран. Тест держит оба списка вместе.
    """
    assert set(CategoryAccent.ALL) <= ALLOWED_STYLES
    assert CategoryAccent.DEFAULT in ALLOWED_STYLES


def test_every_accent_has_a_title() -> None:
    """Без названия цвет нельзя показать в админке — кнопка выйдет пустой."""
    for accent in CategoryAccent.ALL:
        assert CategoryAccent.TITLES.get(accent)


# --- мягкий откат -----------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [None, "", "accent", "warning", "#FF5722", "PRIMARY", "secondary", "gray", 0, 42],
)
def test_unknown_value_falls_back_instead_of_breaking(stored) -> None:
    """Мусор в колонке не должен ронять каталог.

    Значение могло появиться до этой правки, из ручного `UPDATE` или из
    будущей версии, откатанной назад. Категория из-за этого не имеет права
    перестать открываться, поэтому здесь именно мягкий откат к умолчанию,
    а не исключение.
    """
    assert CategoryAccent.normalize(stored) == CategoryAccent.DEFAULT


@pytest.mark.parametrize("accent", CategoryAccent.ALL)
def test_known_value_passes_through(accent: str) -> None:
    assert CategoryAccent.normalize(accent) == accent


# --- цвет доезжает до кнопок ------------------------------------------------


@pytest.mark.parametrize("accent", CategoryAccent.ALL)
def test_category_button_wears_its_accent(accent: str) -> None:
    markup = user_kb.categories([_category(accent)], 0, 1)
    assert markup.inline_keyboard[0][0].style == accent


@pytest.mark.parametrize("accent", CategoryAccent.ALL)
def test_products_inherit_the_category_accent(accent: str) -> None:
    """Товары внутри красятся цветом своей категории.

    Ради этого цвет и задаётся у категории, а не у каждого товара: список
    должен читаться как одно целое.
    """
    markup = user_kb.products([_product()], RATE_KOP, 7, 0, 1, accent)
    assert markup.inline_keyboard[0][0].style == accent


@pytest.mark.parametrize("accent", CategoryAccent.ALL)
def test_buy_button_wears_the_accent(accent: str) -> None:
    markup = user_kb.product_card(_product(), 198_000, "u:cat:7:0", accent=accent)
    assert markup.inline_keyboard[0][0].style == accent


def test_broken_accent_still_builds_every_screen() -> None:
    """Главный тест файла: с мусором в колонке все три экрана собираются.

    Проверяются именно готовые клавиатуры, а не `normalize` в отдельности:
    откат мог быть сделан в модели и забыт в одном из вызовов.
    """
    broken = "#FF5722"
    screens = [
        user_kb.categories([_category(broken)], 0, 1),
        user_kb.products([_product()], RATE_KOP, 7, 0, 1, broken),
        user_kb.product_card(_product(), 198_000, "u:cat:7:0", accent=broken),
        admin_kb.categories([_category(broken)], 0, 1),
        admin_kb.category_card(_category(broken), 3),
    ]
    for markup in screens:
        for style in _styles(markup):
            assert style is None or style in ALLOWED_STYLES


# --- палитра в админке ------------------------------------------------------


def test_picker_offers_every_accent_once() -> None:
    markup = admin_kb.accent_picker("a:cats:0", "a:cat_newaccent:")
    offered = [
        button.callback_data.rsplit(":", 1)[-1]
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("a:cat_newaccent:")
    ]
    assert sorted(offered) == sorted(CategoryAccent.ALL)


def test_picker_button_shows_the_colour_it_offers() -> None:
    """Кнопка выкрашена в тот цвет, который задаёт.

    Иначе выбор приходится проверять переходом в магазин: палитра, нарисованная
    одинаково серым, ничего не показывает.
    """
    markup = admin_kb.accent_picker("a:cats:0", "a:cat_newaccent:")
    for row in markup.inline_keyboard:
        for button in row:
            data = button.callback_data or ""
            if data.startswith("a:cat_newaccent:"):
                assert button.style == data.rsplit(":", 1)[-1]
