from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class UserSG(StatesGroup):
    promo = State()
    search = State()
    topup = State()
