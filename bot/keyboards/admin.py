"""Клавиатуры админ-панели."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.db.models import (
    Admin,
    Broadcast,
    Category,
    Channel,
    Order,
    OrderStatus,
    Product,
    PromoCode,
    SettingEntry,
    TextEntry,
)
from bot.keyboards.theme import (
    DANGER,
    ICON,
    PRIMARY,
    SECONDARY,
    SUCCESS,
    btn,
    pager_row,
    toggle_mark,
)
from bot.services.pricing import format_usd
from bot.utils.money import format_kop


def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    """Собирает клавиатуру, отбрасывая пустые ряды."""
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


_kb = kb  # короткое имя для внутренних вызовов в этом модуле


def back_row(back_data: str) -> list[InlineKeyboardButton]:
    return [
        btn(f"{ICON['back']} Назад", callback_data=back_data, style=PRIMARY),
        btn(f"{ICON['home']} В магазин", callback_data="u:menu", style=SECONDARY),
    ]


def menu() -> InlineKeyboardMarkup:
    """Меню админ-панели.

    Ролей нет — все администраторы видят одно и то же.
    """
    rows = [
        [
            btn(f"{ICON['catalog']} Категории", callback_data="a:cats:0", style=PRIMARY),
            btn(f"{ICON['orders']} Заказы", callback_data="a:orders:0", style=PRIMARY),
        ],
        [
            btn(f"{ICON['stats']} Статистика", callback_data="a:stats", style=PRIMARY),
            btn(f"{ICON['broadcast']} Рассылки", callback_data="a:bc", style=PRIMARY),
        ],
        [
            btn(f"{ICON['users']} Пользователи", callback_data="a:users", style=PRIMARY),
            btn(f"{ICON['export']} Выгрузка", callback_data="a:export", style=PRIMARY),
        ],
        [
            btn(f"{ICON['promo']} Промокоды", callback_data="a:promos:0", style=SUCCESS),
            btn(f"{ICON['texts']} Тексты", callback_data="a:texts:0", style=SUCCESS),
        ],
        [
            btn(f"{ICON['settings']} Настройки", callback_data="a:settings:0", style=SUCCESS),
            btn(f"{ICON['channels']} Каналы", callback_data="a:channels", style=SUCCESS),
        ],
        [
            btn("👑 Администраторы", callback_data="a:admins", style=DANGER),
            btn("📜 Журнал", callback_data="a:audit", style=SECONDARY),
        ],
        [btn(f"{ICON['home']} В магазин", callback_data="u:menu", style=SECONDARY)],
    ]
    return _kb(rows)


# --- категории --------------------------------------------------------------


def categories(items: list[Category], page: int, pages: int) -> InlineKeyboardMarkup:
    rows = [
        [
            btn(
                f"{toggle_mark(category.is_active)} {category.title}",
                callback_data=f"a:cat:{category.id}",
                style=SECONDARY,
            )
        ]
        for category in items
    ]
    rows.append(pager_row("a:cats:", page, pages))
    rows.append([btn(f"{ICON['add']} Новая категория", callback_data="a:cat_add", style=SUCCESS)])
    rows.append(back_row("a:menu"))
    return _kb(rows)


def category_card(category: Category, products_count: int) -> InlineKeyboardMarkup:
    return _kb(
        [
            [
                btn(
                    f"{ICON['orders']} Товары ({products_count})",
                    callback_data=f"a:prods:{category.id}:0",
                    style=PRIMARY,
                )
            ],
            [
                btn(f"{ICON['edit']} Название", callback_data=f"a:cat_edit:{category.id}:title", style=SECONDARY),
                btn(f"{ICON['edit']} Описание", callback_data=f"a:cat_edit:{category.id}:desc", style=SECONDARY),
            ],
            [
                btn(f"{ICON['up']} Выше", callback_data=f"a:cat_move:{category.id}:-1", style=SECONDARY),
                btn(f"{ICON['down']} Ниже", callback_data=f"a:cat_move:{category.id}:1", style=SECONDARY),
            ],
            [
                btn(
                    f"{ICON['toggle']} {'Выключить' if category.is_active else 'Включить'}",
                    callback_data=f"a:cat_toggle:{category.id}",
                    style=SUCCESS if not category.is_active else SECONDARY,
                )
            ],
            [btn(f"{ICON['delete']} Удалить", callback_data=f"a:cat_del:{category.id}", style=DANGER)],
            back_row("a:cats:0"),
        ]
    )


# --- товары -----------------------------------------------------------------


def products(
    items: list[Product], category_id: int, page: int, pages: int
) -> InlineKeyboardMarkup:
    rows = []
    for product in items:
        rows.append(
            [
                btn(
                    f"{toggle_mark(product.is_active)} {product.title} · "
                    f"{format_usd(product.price_usd_cents)}",
                    callback_data=f"a:prod:{product.id}",
                    style=SECONDARY,
                )
            ]
        )
    rows.append(pager_row(f"a:prods:{category_id}:", page, pages))
    rows.append(
        [btn(f"{ICON['add']} Выложить товар", callback_data=f"a:prod_add:{category_id}", style=SUCCESS)]
    )
    rows.append(back_row(f"a:cat:{category_id}"))
    return _kb(rows)


def product_card(product: Product) -> InlineKeyboardMarkup:
    return _kb(
        [
            [
                btn(f"{ICON['edit']} Название", callback_data=f"a:prod_edit:{product.id}:title", style=SECONDARY),
                btn(f"{ICON['edit']} Цена $", callback_data=f"a:prod_edit:{product.id}:price", style=SECONDARY),
            ],
            [
                btn(f"{ICON['edit']} Описание", callback_data=f"a:prod_edit:{product.id}:desc", style=SECONDARY),
                btn(f"{ICON['edit']} Картинка", callback_data=f"a:prod_edit:{product.id}:image", style=SECONDARY),
            ],
            [
                btn("📂 Сменить категорию", callback_data=f"a:prod_cat:{product.id}", style=SECONDARY),
            ],
            [
                btn(f"{ICON['up']} Выше", callback_data=f"a:prod_move:{product.id}:-1", style=SECONDARY),
                btn(f"{ICON['down']} Ниже", callback_data=f"a:prod_move:{product.id}:1", style=SECONDARY),
            ],
            [
                btn(
                    f"{ICON['toggle']} {'Выключить' if product.is_active else 'Включить'}",
                    callback_data=f"a:prod_toggle:{product.id}",
                    style=SUCCESS if not product.is_active else SECONDARY,
                )
            ],
            [btn(f"{ICON['delete']} Удалить", callback_data=f"a:prod_del:{product.id}", style=DANGER)],
            back_row(f"a:prods:{product.category_id}:0"),
        ]
    )


def category_picker(items: list[Category], product_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            btn(
                category.title,
                callback_data=f"a:prod_setcat:{product_id}:{category.id}",
                style=SUCCESS,
            )
        ]
        for category in items
    ]
    rows.append(back_row(f"a:prod:{product_id}"))
    return _kb(rows)


# --- заказы -----------------------------------------------------------------


def orders(items: list[Order], page: int, pages: int, status: str | None) -> InlineKeyboardMarkup:
    rows = [
        [
            btn(
                f"#{order.id} · {format_kop(order.total_kop)} · "
                f"{OrderStatus.TITLES.get(order.status, order.status)}",
                callback_data=f"a:order:{order.id}",
                style=SECONDARY,
            )
        ]
        for order in items
    ]
    rows.append(pager_row("a:orders:", page, pages))
    rows.append(
        [
            btn("🔎 Найти заказ", callback_data="a:order_search", style=PRIMARY),
            btn("🔁 Все статусы" if status else "✅ Только выданные",
                callback_data="a:orders_filter", style=SECONDARY),
        ]
    )
    rows.append(back_row("a:menu"))
    return _kb(rows)


def order_card(
    order: Order,
    can_refund: bool,
    can_confirm: bool = False,
    buyer_username: str | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if can_confirm:
        rows.append(
            [
                btn(
                    "✅ Подтвердить выполнение",
                    callback_data=f"a:done:{order.id}",
                    style=SUCCESS,
                )
            ]
        )
    if buyer_username:
        rows.append(
            [
                btn(
                    "💬 Связаться с покупателем",
                    url=f"https://t.me/{buyer_username}",
                    style=PRIMARY,
                )
            ]
        )
    if can_refund:
        rows.append(
            [btn(f"{ICON['refund']} Вернуть деньги", callback_data=f"a:order_refund:{order.id}", style=DANGER)]
        )
    rows.append(
        [btn("💳 Платежи", callback_data=f"a:order_pays:{order.id}", style=SECONDARY)]
    )
    rows.append(
        [btn(f"{ICON['block']} Заблокировать покупателя", callback_data=f"a:order_block:{order.id}", style=DANGER)]
    )
    rows.append(back_row("a:orders:0"))
    return _kb(rows)


# --- промокоды --------------------------------------------------------------


def promos(items: list[PromoCode], page: int, pages: int) -> InlineKeyboardMarkup:
    rows = [
        [
            btn(
                f"{toggle_mark(promo.is_active)} {promo.code} · {promo.used_count}"
                f"{'/' + str(promo.usage_limit) if promo.usage_limit else ''}",
                callback_data=f"a:promo:{promo.id}",
                style=SECONDARY,
            )
        ]
        for promo in items
    ]
    rows.append(pager_row("a:promos:", page, pages))
    rows.append([btn(f"{ICON['add']} Новый промокод", callback_data="a:promo_add", style=SUCCESS)])
    rows.append(back_row("a:menu"))
    return _kb(rows)


def promo_card(promo: PromoCode) -> InlineKeyboardMarkup:
    return _kb(
        [
            [
                btn(f"{ICON['edit']} Размер", callback_data=f"a:promo_edit:{promo.id}:value", style=SECONDARY),
                btn(f"{ICON['edit']} Лимит", callback_data=f"a:promo_edit:{promo.id}:limit", style=SECONDARY),
            ],
            [
                btn(f"{ICON['edit']} На пользователя", callback_data=f"a:promo_edit:{promo.id}:per_user", style=SECONDARY),
                btn(f"{ICON['edit']} Мин. сумма", callback_data=f"a:promo_edit:{promo.id}:min_order", style=SECONDARY),
            ],
            [
                btn(f"{ICON['edit']} Срок", callback_data=f"a:promo_edit:{promo.id}:until", style=SECONDARY),
                btn("🎯 Область", callback_data=f"a:promo_scope:{promo.id}", style=SECONDARY),
            ],
            [btn("📈 Статистика", callback_data=f"a:promo_stats:{promo.id}", style=PRIMARY)],
            [
                btn(
                    f"{ICON['toggle']} {'Выключить' if promo.is_active else 'Включить'}",
                    callback_data=f"a:promo_toggle:{promo.id}",
                    style=SUCCESS if not promo.is_active else SECONDARY,
                )
            ],
            [btn(f"{ICON['delete']} Удалить", callback_data=f"a:promo_del:{promo.id}", style=DANGER)],
            back_row("a:promos:0"),
        ]
    )


def promo_scope(promo_id: int, categories_list: list[Category]) -> InlineKeyboardMarkup:
    rows = [
        [btn("🌍 Весь магазин", callback_data=f"a:promo_scope_all:{promo_id}", style=SUCCESS)]
    ]
    for category in categories_list:
        rows.append(
            [
                btn(
                    f"📂 {category.title}",
                    callback_data=f"a:promo_scope_cat:{promo_id}:{category.id}",
                    style=SECONDARY,
                )
            ]
        )
    rows.append(back_row(f"a:promo:{promo_id}"))
    return _kb(rows)


# --- рассылки ---------------------------------------------------------------


def broadcast_menu(recent: list[Broadcast]) -> InlineKeyboardMarkup:
    rows = [[btn(f"{ICON['add']} Новая рассылка", callback_data="a:bc_new", style=SUCCESS)]]
    for item in recent[:6]:
        rows.append(
            [
                btn(
                    f"#{item.id} · {item.status} · {item.sent}/{item.total}",
                    callback_data=f"a:bc_view:{item.id}",
                    style=SECONDARY,
                )
            ]
        )
    rows.append(back_row("a:menu"))
    return _kb(rows)


def broadcast_confirm(broadcast_id: int, total: int) -> InlineKeyboardMarkup:
    return _kb(
        [
            [
                btn(
                    f"✅ Отправить {total} пользователям",
                    callback_data=f"a:bc_send:{broadcast_id}",
                    style=SUCCESS,
                )
            ],
            [btn("❌ Отмена", callback_data=f"a:bc_cancel:{broadcast_id}", style=DANGER)],
        ]
    )


def broadcast_running(broadcast_id: int) -> InlineKeyboardMarkup:
    return _kb(
        [
            [btn("🔄 Обновить", callback_data=f"a:bc_view:{broadcast_id}", style=PRIMARY)],
            [btn("⏹ Остановить", callback_data=f"a:bc_stop:{broadcast_id}", style=DANGER)],
            back_row("a:bc"),
        ]
    )


# --- тексты и настройки -----------------------------------------------------


def texts(items: list[TextEntry], page: int, pages: int) -> InlineKeyboardMarkup:
    rows = [
        [btn(entry.title, callback_data=f"a:text:{entry.key}", style=SECONDARY)] for entry in items
    ]
    rows.append(pager_row("a:texts:", page, pages))
    rows.append(back_row("a:menu"))
    return _kb(rows)


def settings(items: list[SettingEntry], page: int, pages: int) -> InlineKeyboardMarkup:
    rows = [
        [
            btn(
                f"{entry.title}: {(entry.value or '—')[:20]}",
                callback_data=f"a:set:{entry.key}",
                style=SECONDARY,
            )
        ]
        for entry in items
    ]
    rows.append(pager_row("a:settings:", page, pages))
    rows.append(
        [btn("🖼 Загрузить шапку", callback_data="a:set_header", style=SUCCESS)]
    )
    rows.append(back_row("a:menu"))
    return _kb(rows)


def channels(items: list[Channel]) -> InlineKeyboardMarkup:
    rows = []
    for channel in items:
        mark = toggle_mark(channel.is_active)
        error = " ⚠️" if channel.last_error else ""
        rows.append(
            [
                btn(f"{mark} {channel.title}{error}", callback_data=f"a:ch_toggle:{channel.id}", style=SECONDARY),
                btn(ICON["delete"], callback_data=f"a:ch_del:{channel.id}", style=DANGER),
            ]
        )
    rows.append([btn(f"{ICON['add']} Добавить канал", callback_data="a:ch_add", style=SUCCESS)])
    rows.append(back_row("a:menu"))
    return _kb(rows)


def admins(items: list[Admin], owner_ids: list[int]) -> InlineKeyboardMarkup:
    rows = []
    for admin in items:
        title = f"🛠 {admin.user_id}"
        row = [btn(title, callback_data="noop", style=SECONDARY)]
        # Администратора из окружения удалить нельзя — иначе магазин остаётся
        # без доступа, и починить его можно только правкой базы руками.
        if admin.user_id not in owner_ids:
            row.append(btn(ICON["delete"], callback_data=f"a:admin_del:{admin.user_id}", style=DANGER))
        rows.append(row)
    rows.append([btn(f"{ICON['add']} Добавить админа", callback_data="a:admin_add", style=SUCCESS)])
    rows.append(back_row("a:menu"))
    return _kb(rows)


def export_menu() -> InlineKeyboardMarkup:
    return _kb(
        [
            [btn("📤 Все заказы", callback_data="a:export_all", style=PRIMARY)],
            [btn("📤 За 30 дней", callback_data="a:export_month", style=PRIMARY)],
            [btn("📤 За сегодня", callback_data="a:export_today", style=PRIMARY)],
            back_row("a:menu"),
        ]
    )


def confirm(yes: str, no: str, yes_text: str = "✅ Да, подтверждаю") -> InlineKeyboardMarkup:
    return _kb([[btn(yes_text, callback_data=yes, style=DANGER)], [btn("❌ Отмена", callback_data=no, style=PRIMARY)]])


def user_card(user_id: int, is_blocked: bool) -> InlineKeyboardMarkup:
    return _kb(
        [
            [btn("💼 Изменить баланс", callback_data=f"a:user_balance:{user_id}", style=SUCCESS)],
            [
                btn(
                    "✅ Разблокировать" if is_blocked else f"{ICON['block']} Заблокировать",
                    callback_data=f"a:user_block:{user_id}",
                    style=SUCCESS if is_blocked else DANGER,
                )
            ],
            [btn("🧾 Заказы", callback_data=f"a:user_orders:{user_id}", style=PRIMARY)],
            back_row("a:users"),
        ]
    )


def users_menu() -> InlineKeyboardMarkup:
    return _kb(
        [
            [btn("🔎 Найти пользователя", callback_data="a:user_search", style=PRIMARY)],
            [btn("🧮 Сверить балансы", callback_data="a:balance_audit", style=SECONDARY)],
            back_row("a:menu"),
        ]
    )


# --- уведомление о заказе в работе ------------------------------------------


def fulfillment_card(order_id: int, buyer_username: str | None) -> InlineKeyboardMarkup:
    """Кнопки под сообщением администратору о новом заказе.

    Кнопка «Связаться» ставится только при наличии юзернейма. Для покупателя
    без него Bot API допускает `tg://user?id=`, но открывается такая ссылка
    лишь если это разрешено настройками приватности, — а кнопка, которая
    молча не срабатывает, читается как поломка бота. Вместо неё в тексте
    сообщения стоит упоминание.
    """
    rows = [
        [
            btn(
                "✅ Подтвердить выполнение",
                callback_data=f"a:done:{order_id}",
                style=SUCCESS,
            )
        ]
    ]
    if buyer_username:
        rows.append(
            [
                btn(
                    "💬 Связаться с покупателем",
                    url=f"https://t.me/{buyer_username}",
                    style=PRIMARY,
                )
            ]
        )
    rows.append(
        [btn(f"{ICON['orders']} Открыть заказ", callback_data=f"a:order:{order_id}", style=SECONDARY)]
    )
    return _kb(rows)
