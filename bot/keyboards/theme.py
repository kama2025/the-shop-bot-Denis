"""Оформление кнопок.

Цвет кнопок появился в Bot API 9.4 (`style` у `InlineKeyboardButton`).
Доступны ровно три: `primary` — синий, `success` — зелёный, `danger` — красный,
плюс `secondary`. Произвольный цвет задать нельзя — это ограничение Telegram,
а не проекта.

Смысл цвета закреплён здесь и нигде больше не дублируется: зелёный — то, ради
чего пришли (купить, подтвердить), красный — разрушительное (отмена, удаление),
синий — навигация. Копия правила разъезжается с оригиналом, поэтому правило
одно и живёт в этом файле.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Telegram принимает ровно эти четыре значения. Проверено пробой к Bot API:
# любое другое («secondary», «gray», «accent») отклоняется целиком —
# «Bad Request: can't parse InlineKeyboardButton: invalid button style
# specified», и сообщение не отправляется вообще.
DEFAULT = "default"
PRIMARY = "primary"
SUCCESS = "success"
DANGER = "danger"

ALLOWED_STYLES = frozenset({DEFAULT, PRIMARY, SUCCESS, DANGER})

# Прежнее имя нейтрального стиля. Оставлено, чтобы не переписывать полсотни
# вызовов, но значение теперь то, которое Telegram действительно понимает.
SECONDARY = DEFAULT


class ButtonStyleError(ValueError):
    """Стиль кнопки не входит в набор, который принимает Telegram."""

# Названия и значки в одном месте: владелец меняет их правкой этого файла,
# не разыскивая строки по хендлерам.
ICON = {
    "shop": "🛍",
    "catalog": "📂",
    "stock": "📊",
    "profile": "👤",
    "info": "ℹ️",
    "search": "🔎",
    "home": "🏠",
    "back": "◀️",
    "buy": "🛒",
    "pay": "💳",
    "check": "✅",
    "cancel": "❌",
    "minus": "➖",
    "plus": "➕",
    "promo": "🎟",
    "wallet": "💼",
    "history": "🧾",
    "subscribe": "🔔",
    "admin": "🛠",
    "add": "➕",
    "edit": "✏️",
    "delete": "🗑",
    "toggle": "🔘",
    "up": "🔼",
    "down": "🔽",
    "broadcast": "📣",
    "stats": "📈",
    "orders": "📦",
    "users": "👥",
    "texts": "📝",
    "settings": "⚙️",
    "channels": "📡",
    "export": "📤",
    "refund": "↩️",
    "replace": "🔁",
    "block": "🚫",
}


def btn(
    text: str,
    *,
    callback_data: str | None = None,
    url: str | None = None,
    style: str | None = None,
) -> InlineKeyboardButton:
    """Собирает кнопку, проверяя стиль на месте.

    Неверный стиль Telegram не прощает: он отвергает всю клавиатуру, сообщение
    не уходит, и экран у покупателя просто исчезает. Ошибку лучше получить
    здесь, с указанием допустимых значений, чем разбирать «Bad Request» из
    журнала.
    """
    if style is not None and style not in ALLOWED_STYLES:
        raise ButtonStyleError(
            f"Стиль кнопки {style!r} Telegram не принимает. "
            f"Допустимы: {', '.join(sorted(ALLOWED_STYLES))}."
        )
    return InlineKeyboardButton(
        text=text, callback_data=callback_data, url=url, style=style
    )


def rows(*button_rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[row for row in button_rows if row])


def nav_row(
    back_data: str | None = None, home: bool = True, back_style: str = PRIMARY
) -> list[InlineKeyboardButton]:
    """Нижний ряд навигации. Единый во всём боте, чтобы не искать выход."""
    row: list[InlineKeyboardButton] = []
    if back_data:
        row.append(
            btn(f"{ICON['back']} Назад", callback_data=back_data, style=back_style)
        )
    if home:
        row.append(btn(f"{ICON['home']} Главное меню", callback_data="u:menu", style=PRIMARY))
    return row


def confirm_row(
    yes_data: str,
    no_data: str,
    yes_text: str = "✅ Подтвердить",
    no_text: str = "❌ Отмена",
) -> list[InlineKeyboardButton]:
    return [
        btn(yes_text, callback_data=yes_data, style=SUCCESS),
        btn(no_text, callback_data=no_data, style=DANGER),
    ]


def pager_row(prefix: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    """Строка перелистывания. Пустая, если страница одна."""
    if pages <= 1:
        return []
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(btn("‹", callback_data=f"{prefix}{page - 1}", style=SECONDARY))
    row.append(btn(f"{page + 1}/{pages}", callback_data="noop", style=SECONDARY))
    if page < pages - 1:
        row.append(btn("›", callback_data=f"{prefix}{page + 1}", style=SECONDARY))
    return row


def toggle_mark(active: bool) -> str:
    return "🟢" if active else "🔴"
