"""Клавиатуры пользовательской части."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.db.models import Category, CategoryAccent, Channel, Order, OrderStatus, Product
from bot.keyboards.theme import (
    DANGER,
    ICON,
    PRIMARY,
    SECONDARY,
    SUCCESS,
    btn,
    nav_row,
    pager_row,
    rows,
)
from bot.payments.base import PaymentMethod
from bot.services.pricing import base_kop, format_usd
from bot.utils.money import format_kop


def subscription(missing: list[Channel]) -> InlineKeyboardMarkup:
    keyboard: list[list[InlineKeyboardButton]] = [
        [btn(f"{ICON['subscribe']} {channel.title}", url=channel.invite_url, style=SUCCESS)]
        for channel in missing
    ]
    keyboard.append(
        [btn("✅ Проверить подписку", callback_data="u:sub_check", style=PRIMARY)]
    )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    keyboard = [
        [
            btn(f"{ICON['catalog']} Каталог", callback_data="u:cats:0", style=PRIMARY),
            btn(f"{ICON['search']} Поиск", callback_data="u:search", style=PRIMARY),
        ],
        [
            btn(f"{ICON['profile']} Профиль", callback_data="u:profile", style=PRIMARY),
        ],
        [btn(f"{ICON['info']} Информация", callback_data="u:info", style=PRIMARY)],
    ]
    if is_admin:
        keyboard.append(
            [btn(f"{ICON['admin']} Админ-панель", callback_data="a:menu", style=DANGER)]
        )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def categories(items: list[Category], page: int, pages: int) -> InlineKeyboardMarkup:
    keyboard = [
        [
            btn(
                category.title,
                callback_data=f"u:cat:{category.id}:0",
                style=CategoryAccent.normalize(category.accent),
            )
        ]
        for category in items
    ]
    pager = pager_row("u:cats:", page, pages)
    if pager:
        keyboard.append(pager)
    keyboard.append(nav_row(back_data=None))
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def products(
    items: list[Product],
    rate_kop: int,
    category_id: int,
    page: int,
    pages: int,
    accent: str | None = None,
) -> InlineKeyboardMarkup:
    """Список товаров категории.

    Цена показывается в долларах, рубли считаются по текущему курсу прямо
    здесь. Если курса нет, рублёвая часть просто не выводится — врать числом
    хуже, чем не показать его.
    """
    style = CategoryAccent.normalize(accent)
    keyboard: list[list[InlineKeyboardButton]] = []
    for product in items:
        price = format_usd(product.price_usd_cents)
        if rate_kop > 0:
            price += f" ({format_kop(base_kop(product.price_usd_cents, rate_kop))})"
        keyboard.append(
            [
                btn(
                    f"{product.title} — {price}",
                    callback_data=f"u:prod:{product.id}",
                    style=style,
                )
            ]
        )
    pager = pager_row(f"u:cat:{category_id}:", page, pages)
    if pager:
        keyboard.append(pager)
    keyboard.append(nav_row(back_data="u:cats:0"))
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def product_card(
    product: Product,
    total_kop: int | None,
    back_data: str,
    accent: str | None = None,
) -> InlineKeyboardMarkup:
    """Карточка товара.

    Количества нет: один заказ — один аккаунт. `total_kop` равен None, когда
    курс недоступен, — тогда кнопки покупки нет вовсе. Кнопка, после которой
    приходит отказ, читается как поломка.
    """
    keyboard: list[list[InlineKeyboardButton]] = []
    if total_kop is not None:
        keyboard.append(
            [
                btn(
                    f"{ICON['buy']} Купить • {format_kop(total_kop)}",
                    callback_data=f"u:buy:{product.id}",
                    style=CategoryAccent.normalize(accent),
                )
            ]
        )
    keyboard.append(nav_row(back_data=back_data, back_style=DANGER))
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def payment_methods(
    order: Order, methods: list[PaymentMethod]
) -> InlineKeyboardMarkup:
    keyboard: list[list[InlineKeyboardButton]] = []
    for method in methods:
        keyboard.append(
            [
                btn(
                    f"{method.emoji} {method.title}",
                    callback_data=f"u:pay:{order.id}:{method.code}",
                    style=method.style,
                )
            ]
        )
    keyboard.append(
        [btn(f"{ICON['cancel']} Отмена", callback_data=f"u:cancel:{order.id}", style=DANGER)]
    )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def payment_link(order: Order) -> InlineKeyboardMarkup:
    keyboard: list[list[InlineKeyboardButton]] = []
    if order.pay_url:
        keyboard.append([btn(f"{ICON['pay']} Оплатить", url=order.pay_url, style=SUCCESS)])
    keyboard.append(
        [btn(f"{ICON['check']} Проверить оплату", callback_data=f"u:check:{order.id}", style=PRIMARY)]
    )
    keyboard.append(
        [btn(f"{ICON['cancel']} Отменить заказ", callback_data=f"u:cancel:{order.id}", style=DANGER)]
    )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def profile(has_promo: bool) -> InlineKeyboardMarkup:
    keyboard = [
        [btn(f"{ICON['history']} Мои покупки", callback_data="u:purchases:0", style=PRIMARY)],
    ]
    promo_row = [btn(f"{ICON['promo']} Ввести промокод", callback_data="u:promo", style=SUCCESS)]
    if has_promo:
        promo_row.append(btn("🗑 Снять", callback_data="u:promo_clear", style=DANGER))
    keyboard.append(promo_row)
    keyboard.append(nav_row(back_data=None))
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def send_credentials(order_id: int) -> InlineKeyboardMarkup:
    """Кнопка возврата к вводу реквизитов.

    Покупатель мог уйти из состояния — нажать «Назад», перезапустить бота,
    просто закрыть чат. Без этой кнопки оплаченный заказ становится тупиком.
    """
    return rows(
        [btn(f"{ICON['profile']} Отправить логин и пароль", callback_data=f"u:creds:{order_id}", style=SUCCESS)],
        nav_row(back_data="u:profile"),
    )


def purchases(items: list[Order], page: int, pages: int) -> InlineKeyboardMarkup:
    keyboard = [
        [
            btn(
                f"#{order.id} · {order.product_title[:24]} · "
                f"{OrderStatus.TITLES.get(order.status, order.status)}",
                callback_data=f"u:purchase:{order.id}",
                style=SECONDARY,
            )
        ]
        for order in items
    ]
    pager = pager_row("u:purchases:", page, pages)
    if pager:
        keyboard.append(pager)
    keyboard.append(nav_row(back_data="u:profile"))
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def simple_back(back_data: str = "u:menu") -> InlineKeyboardMarkup:
    return rows(nav_row(back_data=back_data if back_data != "u:menu" else None))
