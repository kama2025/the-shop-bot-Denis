"""Права доступа.

Ролей больше нет, дверь одна. Проверяется главное свойство: умолчание — отказ.
Всё, что не является явным `True`, не проходит.
"""

from __future__ import annotations

import pytest

from bot.services.access import Actor, allows


def test_admin_passes():
    assert allows(True) is True


def test_not_admin_denied():
    assert allows(False) is False


def test_missing_flag_denied():
    """`None` из базы означает «записи нет», а не «разрешено»."""
    assert allows(None) is False


@pytest.mark.parametrize(
    "value",
    [
        1,          # истинное число
        "admin",    # непустая строка
        [1],        # непустой список
        object(),   # что угодно
    ],
)
def test_truthy_but_not_true_denied(value):
    """Истинное значение — не то же самое, что признак администратора.

    Проверка написана как `is True`, а не как `if is_admin`. Разница видна
    ровно здесь: строка «admin» истинна, но администратором не делает. Если
    однажды в поле попадёт что-то из внешнего источника, отказ должен остаться
    отказом.
    """
    assert allows(value) is False


def test_actor_defaults_to_denied():
    """Актор без явного признака — не администратор.

    Значение по умолчанию решает, что произойдёт, если кто-то создаст `Actor`
    и забудет передать признак. Оно должно закрывать, а не открывать.
    """
    assert Actor(user_id=123).is_admin is False
    assert allows(Actor(user_id=123).is_admin) is False
