from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class CategorySG(StatesGroup):
    title = State()
    description = State()
    edit_title = State()
    edit_description = State()


class ProductSG(StatesGroup):
    delivery_type = State()
    title = State()
    description = State()
    price = State()
    image = State()
    edit_title = State()
    edit_description = State()
    edit_price = State()
    edit_image = State()


class StockSG(StatesGroup):
    items = State()
    files = State()
    confirm = State()
    reject_reason = State()


class OrderSG(StatesGroup):
    search = State()
    refund_comment = State()
    block_reason = State()


class ProductWizardSG(StatesGroup):
    """Выкладка товара: шаги идут подряд, товар создаётся только в конце.

    Обрыв на любом шаге не оставляет следов — наполовину созданный товар
    в каталоге хуже, чем его отсутствие.
    """

    category = State()
    title = State()
    image = State()
    price = State()
    description = State()
    confirm = State()


class PromoSG(StatesGroup):
    code = State()
    discount_type = State()
    discount_value = State()
    edit_value = State()
    edit_limit = State()
    edit_per_user = State()
    edit_min_order = State()
    edit_until = State()


class BroadcastSG(StatesGroup):
    content = State()
    buttons = State()
    confirm = State()


class TextSG(StatesGroup):
    value = State()


class SettingSG(StatesGroup):
    value = State()
    header_image = State()


class ChannelSG(StatesGroup):
    chat_ref = State()
    title = State()
    invite_url = State()


class AdminSG(StatesGroup):
    add_id = State()


class UserAdminSG(StatesGroup):
    search = State()
    balance_amount = State()
    balance_comment = State()
