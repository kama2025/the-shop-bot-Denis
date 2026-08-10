"""Клавиатуры: стили кнопок и длина callback_data.

Оба свойства Telegram проверяет на своей стороне и при нарушении отвергает
**всю клавиатуру целиком**. Сообщение не уходит вообще, и у покупателя экран
просто исчезает. Поймать это в тесте дешевле, чем разбирать «Bad Request»
из журнала.

Набор допустимых стилей получен пробой к живому Bot API: `default`, `primary`,
`success`, `danger`. Значения вроде `secondary`, `gray`, `accent` отвергаются.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from aiogram.types import InlineKeyboardMarkup

from bot.db.models import (
    Admin,
    AdminRole,
    Broadcast,
    Category,
    Channel,
    Order,
    OrderStatus,
    Product,
    PromoCode,
    SettingEntry,
    StockBatch,
    TextEntry,
    User,
)
from bot.keyboards import admin as admin_kb
from bot.keyboards import user as user_kb
from bot.keyboards.theme import ALLOWED_STYLES, ButtonStyleError, btn
from bot.payments.base import PaymentMethod
from bot.utils.money import DISCOUNT_PERCENT

CALLBACK_LIMIT = 64
NOW = datetime(2026, 8, 10, 12, 0)


# --- проверка самого btn() --------------------------------------------------


@pytest.mark.parametrize("style", sorted(ALLOWED_STYLES))
def test_btn_accepts_documented_styles(style: str) -> None:
    assert btn("текст", callback_data="noop", style=style).style == style


def test_btn_accepts_no_style() -> None:
    assert btn("текст", callback_data="noop").style is None


@pytest.mark.parametrize("style", ["secondary", "gray", "accent", "warning", "PRIMARY", ""])
def test_btn_rejects_unknown_styles(style: str) -> None:
    """Именно `secondary` однажды уронил экран профиля в проде."""
    with pytest.raises(ButtonStyleError):
        btn("текст", callback_data="noop", style=style)


# --- заготовки --------------------------------------------------------------


def _category(id_: int = 1) -> Category:
    return Category(id=id_, title="Категория", sort_order=10, is_active=True)


def _product(id_: int = 1) -> Product:
    return Product(
        id=id_, category_id=1, title="Товар", price_kop=9000, sort_order=10, is_active=True
    )


def _order(id_: int = 10001) -> Order:
    return Order(
        id=id_,
        user_id=878351372,
        product_id=1,
        product_title="Товар",
        qty=2,
        unit_price_kop=9000,
        subtotal_kop=18000,
        discount_kop=1800,
        total_kop=16200,
        status=OrderStatus.PENDING,
        pay_url="https://pay.example/x",
        created_at=NOW,
    )


def _promo() -> PromoCode:
    return PromoCode(
        id=1, code="DEMO10", discount_type=DISCOUNT_PERCENT, discount_value=10,
        used_count=3, usage_limit=10, is_active=True,
    )


def _channel() -> Channel:
    return Channel(
        id=1, chat_ref="@demo", title="Канал", invite_url="https://t.me/demo",
        sort_order=10, is_active=True,
    )


def _methods() -> list[PaymentMethod]:
    return [
        PaymentMethod(provider="platega", code="platega:2", title="СБП", emoji="🏦"),
        PaymentMethod(provider="platega", code="platega:11", title="Карта", emoji="💳"),
        PaymentMethod(provider="cryptobot", code="cryptobot:crypto", title="CryptoBot", emoji="🪙"),
    ]


def _all_keyboards() -> list[tuple[str, InlineKeyboardMarkup]]:
    """Каждая клавиатура бота, собранная на правдоподобных данных."""
    category, product, order = _category(), _product(), _order()
    stock = {product.id: 5}
    batch = StockBatch(id=1, product_id=1, items_count=7, created_at=NOW)

    return [
        ("user.subscription", user_kb.subscription([_channel()])),
        ("user.main_menu(admin)", user_kb.main_menu(is_admin=True)),
        ("user.main_menu(guest)", user_kb.main_menu(is_admin=False)),
        ("user.categories", user_kb.categories([category], 0, 3)),
        ("user.products", user_kb.products([product], stock, 1, 1, 3)),
        ("user.products(empty stock)", user_kb.products([product], {1: 0}, 1, 0, 1)),
        ("user.product_card", user_kb.product_card(product, 2, 18000, 5, 10, "u:cat:1:0")),
        ("user.product_card(min)", user_kb.product_card(product, 1, 9000, 1, 10, "u:cat:1:0")),
        ("user.payment_methods", user_kb.payment_methods(order, _methods(), 50000)),
        ("user.payment_methods(no balance)", user_kb.payment_methods(order, _methods(), None)),
        ("user.payment_link", user_kb.payment_link(order)),
        ("user.profile", user_kb.profile(has_promo=True, topup_enabled=True)),
        ("user.profile(bare)", user_kb.profile(has_promo=False, topup_enabled=False)),
        ("user.purchases", user_kb.purchases([order], 1, 5)),
        ("user.simple_back", user_kb.simple_back()),
        ("admin.menu(owner)", admin_kb.menu(is_owner=True)),
        ("admin.menu(admin)", admin_kb.menu(is_owner=False)),
        ("admin.categories", admin_kb.categories([category], 0, 2)),
        ("admin.category_card", admin_kb.category_card(category, 4)),
        ("admin.products", admin_kb.products([product], stock, 1, 0, 2)),
        ("admin.product_card", admin_kb.product_card(product)),
        ("admin.category_picker", admin_kb.category_picker([category], 1)),
        ("admin.stock_card", admin_kb.stock_card(product, [batch])),
        ("admin.batch_card", admin_kb.batch_card(batch)),
        ("admin.orders", admin_kb.orders([order], 0, 2, None)),
        ("admin.orders(filtered)", admin_kb.orders([order], 0, 2, OrderStatus.DELIVERED)),
        ("admin.order_card", admin_kb.order_card(order, True, True)),
        ("admin.order_card(closed)", admin_kb.order_card(order, False, False)),
        ("admin.promos", admin_kb.promos([_promo()], 0, 1)),
        ("admin.promo_card", admin_kb.promo_card(_promo())),
        ("admin.promo_scope", admin_kb.promo_scope(1, [category])),
        ("admin.broadcast_menu", admin_kb.broadcast_menu([Broadcast(id=1, admin_id=1, status="done", sent=5, total=5)])),
        ("admin.broadcast_confirm", admin_kb.broadcast_confirm(1, 1543)),
        ("admin.broadcast_running", admin_kb.broadcast_running(1)),
        ("admin.texts", admin_kb.texts([TextEntry(key="welcome", value="x", title="Приветствие")], 0, 6)),
        ("admin.settings", admin_kb.settings([SettingEntry(key="shop_name", value="Shop", title="Название")], 0, 2)),
        ("admin.channels", admin_kb.channels([_channel()])),
        ("admin.admins", admin_kb.admins([Admin(id=1, user_id=878351372, role=AdminRole.OWNER)], [878351372])),
        ("admin.admins(removable)", admin_kb.admins([Admin(id=2, user_id=111, role=AdminRole.ADMIN)], [878351372])),
        ("admin.export_menu", admin_kb.export_menu()),
        ("admin.confirm", admin_kb.confirm("a:x", "a:y")),
        ("admin.user_card", admin_kb.user_card(878351372, is_blocked=False)),
        ("admin.user_card(blocked)", admin_kb.user_card(878351372, is_blocked=True)),
        ("admin.users_menu", admin_kb.users_menu()),
    ]


# --- сами проверки ----------------------------------------------------------


def test_every_keyboard_builds() -> None:
    keyboards = _all_keyboards()
    assert len(keyboards) > 30, "проверяем не все экраны"


@pytest.mark.parametrize(("name", "markup"), _all_keyboards(), ids=lambda v: v if isinstance(v, str) else "")
def test_styles_are_accepted_by_telegram(name: str, markup: InlineKeyboardMarkup) -> None:
    for row in markup.inline_keyboard:
        for button in row:
            assert button.style is None or button.style in ALLOWED_STYLES, (
                f"{name}: кнопка {button.text!r} со стилем {button.style!r} — "
                "Telegram отвергнет всю клавиатуру"
            )


@pytest.mark.parametrize(("name", "markup"), _all_keyboards(), ids=lambda v: v if isinstance(v, str) else "")
def test_callback_data_fits_the_limit(name: str, markup: InlineKeyboardMarkup) -> None:
    """callback_data длиннее 64 байт Telegram тоже не принимает."""
    for row in markup.inline_keyboard:
        for button in row:
            if button.callback_data is None:
                continue
            size = len(button.callback_data.encode())
            assert size <= CALLBACK_LIMIT, (
                f"{name}: callback_data {button.callback_data!r} занимает {size} байт"
            )


@pytest.mark.parametrize(("name", "markup"), _all_keyboards(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_button_leads_somewhere(name: str, markup: InlineKeyboardMarkup) -> None:
    """Кнопка без действия читается как поломка."""
    for row in markup.inline_keyboard:
        for button in row:
            assert button.callback_data or button.url, (
                f"{name}: кнопка {button.text!r} ничего не делает"
            )
