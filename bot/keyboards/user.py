"""Клавиатуры пользовательской части."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.db.models import Category, Channel, DeliveryType, Order, OrderStatus, Product
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
            btn(f"{ICON['stock']} Наличие", callback_data="u:avail", style=PRIMARY),
        ],
        [
            btn(f"{ICON['search']} Поиск", callback_data="u:search", style=PRIMARY),
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
        [btn(category.title, callback_data=f"u:cat:{category.id}:0", style=SUCCESS)]
        for category in items
    ]
    pager = pager_row("u:cats:", page, pages)
    if pager:
        keyboard.append(pager)
    keyboard.append(nav_row(back_data=None))
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def products(
    items: list[Product],
    stock: dict[int, int],
    category_id: int,
    page: int,
    pages: int,
) -> InlineKeyboardMarkup:
    keyboard: list[list[InlineKeyboardButton]] = []
    for product in items:
        left = stock.get(product.id, 0)
        if product.delivery_type == DeliveryType.MANUAL:
            mark = ""
            left = 1  # «есть», без числа: склада у такого товара нет
        else:
            mark = "" if left else " (нет)"
        keyboard.append(
            [
                btn(
                    f"{product.title} — {format_kop(product.price_kop)}{mark}",
                    callback_data=f"u:prod:{product.id}:1",
                    style=SUCCESS if left else SECONDARY,
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
    qty: int,
    total_kop: int,
    in_stock: int,
    max_qty: int,
    back_data: str,
) -> InlineKeyboardMarkup:
    keyboard: list[list[InlineKeyboardButton]] = []

    if in_stock > 0:
        limit = min(max_qty, in_stock)
        keyboard.append(
            [
                btn(
                    ICON["minus"] if qty > 1 else "⛔",
                    callback_data=f"u:prod:{product.id}:{max(1, qty - 1)}",
                    style=DANGER,
                ),
                btn(
                    ICON["plus"] if qty < limit else "⛔",
                    callback_data=f"u:prod:{product.id}:{min(limit, qty + 1)}",
                    style=SUCCESS,
                ),
            ]
        )
        keyboard.append(
            [
                btn(
                    f"{ICON['buy']} Купить • {qty} шт • {format_kop(total_kop)}",
                    callback_data=f"u:buy:{product.id}:{qty}",
                    style=SUCCESS,
                )
            ]
        )
    keyboard.append(nav_row(back_data=back_data, back_style=DANGER))
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def payment_methods(
    order: Order,
    methods: list[PaymentMethod],
    balance_kop: int | None,
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
    if balance_kop is not None:
        enough = balance_kop >= order.total_kop
        keyboard.append(
            [
                btn(
                    f"{ICON['wallet']} С баланса ({format_kop(balance_kop)})",
                    callback_data=f"u:pay:{order.id}:balance",
                    style=SUCCESS if enough else SECONDARY,
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


def profile(has_promo: bool, topup_enabled: bool) -> InlineKeyboardMarkup:
    keyboard = [
        [btn(f"{ICON['history']} Мои покупки", callback_data="u:purchases:0", style=PRIMARY)],
    ]
    promo_row = [btn(f"{ICON['promo']} Ввести промокод", callback_data="u:promo", style=SUCCESS)]
    if has_promo:
        promo_row.append(btn("🗑 Снять", callback_data="u:promo_clear", style=DANGER))
    keyboard.append(promo_row)
    if topup_enabled:
        keyboard.append(
            [btn(f"{ICON['wallet']} Пополнить баланс", callback_data="u:topup", style=SUCCESS)]
        )
    keyboard.append([btn("📜 История баланса", callback_data="u:balance:0", style=SECONDARY)])
    keyboard.append(nav_row(back_data=None))
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


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
