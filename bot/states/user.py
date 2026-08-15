from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class UserSG(StatesGroup):
    promo = State()
    search = State()
    topup = State()
    # Ждём логин и пароль от аккаунта после оплаты. Номер заказа лежит
    # в данных состояния: покупатель может оплатить несколько заказов подряд.
    credentials = State()
    # Пароль отдельным сообщением — если в первом была только одна строка.
    credentials_password = State()
