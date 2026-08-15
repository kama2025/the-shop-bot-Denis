"""Разбор реквизитов и карточка заказа для администратора.

Отдельно от `bot.services.delivery`: там переходы состояний, здесь — тексты и
разбор ввода. Смешивать не стоит, потому что разбор пользовательского ввода
меняется от жалоб покупателей, а переходы состояний — нет.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from bot.db.models import Order, User
from bot.services import pricing
from bot.utils.money import format_kop


@dataclass(frozen=True)
class Credentials:
    login: str
    password: str | None

    @property
    def complete(self) -> bool:
        return bool(self.login and self.password)


def parse_credentials(text: str) -> Credentials | None:
    """Разбирает сообщение с логином и паролем.

    Первая непустая строка — логин, вторая — пароль. Если строка одна, пароль
    остаётся `None`, и бот спросит его отдельным сообщением.

    Разбирать `login:password` из одной строки намеренно не будем: пароль имеет
    полное право содержать двоеточие, и такой разбор молча отрежет ему хвост.
    Молчаливая порча пароля хуже лишнего вопроса — она всплывёт только когда
    администратор не сможет войти.
    """
    if not text:
        return None

    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return None

    login = lines[0]
    password = lines[1] if len(lines) > 1 else None
    return Credentials(login=login, password=password)


def admin_card(order: Order, buyer: User | None) -> str:
    """Сообщение администратору о заказе, готовом к работе."""
    lines = [
        "🛠 <b>Новый заказ в работу</b>",
        "",
        f"🎟 Токен: <code>{order.token or '—'}</code>",
        f"📦 {html.escape(order.product_title)}",
        f"💵 {pricing.format_usd(order.price_usd_cents)} · оплачено {format_kop(order.total_kop)}",
        "",
        "🔑 <b>Доступ к аккаунту</b>",
        f"Логин: <code>{html.escape(order.account_login or '')}</code>",
        f"Пароль: <code>{html.escape(order.account_password or '')}</code>",
        "",
        f"👤 Покупатель: {buyer_mention(order, buyer)}",
    ]
    return "\n".join(lines)


def buyer_mention(order: Order, buyer: User | None) -> str:
    """Как сослаться на покупателя в тексте.

    Если юзернейма нет, ставим упоминание по идентификатору. Оно подчиняется
    настройкам приватности и может не открыться — но неактивный текст выглядит
    куда лучше, чем кнопка, которая не работает.
    """
    if buyer is not None and buyer.username:
        return f"@{html.escape(buyer.username)} (<code>{order.user_id}</code>)"
    return (
        f'<a href="tg://user?id={order.user_id}">покупатель</a> '
        f"(<code>{order.user_id}</code>)"
    )


def buyer_username(buyer: User | None) -> str | None:
    return buyer.username if buyer is not None and buyer.username else None
