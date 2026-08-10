"""Сборка роутеров.

Порядок важен: админские роутеры идут раньше пользовательских, иначе
пользовательский обработчик `u:menu` перехватит переход из админки. Последним
подключается `fallback` — он ловит всё, что не разобрали остальные, и не должен
делать это раньше времени.
"""

from __future__ import annotations

from aiogram import Router

from bot.handlers import fallback
from bot.handlers.admin import (
    broadcast as admin_broadcast,
    categories as admin_categories,
    content as admin_content,
    menu as admin_menu,
    orders as admin_orders,
    people as admin_people,
    products as admin_products,
    promo as admin_promo,
    stock as admin_stock,
)
from bot.handlers.user import catalog as user_catalog
from bot.handlers.user import profile as user_profile
from bot.handlers.user import purchase as user_purchase
from bot.handlers.user import start as user_start


def build_router() -> Router:
    root = Router(name="root")
    root.include_router(admin_menu.router)
    root.include_router(admin_categories.router)
    root.include_router(admin_products.router)
    root.include_router(admin_stock.router)
    root.include_router(admin_orders.router)
    root.include_router(admin_promo.router)
    root.include_router(admin_broadcast.router)
    root.include_router(admin_content.router)
    root.include_router(admin_people.router)

    root.include_router(user_start.router)
    root.include_router(user_catalog.router)
    root.include_router(user_purchase.router)
    root.include_router(user_profile.router)

    root.include_router(fallback.router)
    return root
