"""Права доступа в админ-панель.

Дверь одна: администратор или нет. Разграничение по ролям убрано по решению
заказчика — все администраторы равны.

Умолчание — отказ. Актор без записи в таблице администраторов не проходит
никуда: ветка, ставящая «разрешено» в неизвестном случае, по умолчанию
открывает то, что должна закрывать.

Раньше здесь была матрица «раздел → дверь → роли» с четырьмя дверями (открыть
запись, показать список, выполнить действие, создать). Она удалена вместе с
ролями. Если разграничение понадобится снова, восстанавливать придётся именно
матрицу: проверка в одном месте на входе в роутер её не заменяет — она не
отличает «показать список» от «удалить».
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from bot.repo import users as users_repo


@dataclass(frozen=True)
class Actor:
    user_id: int
    is_admin: bool = False


def allows(is_admin: bool | None) -> bool:
    """Чистая функция проверки — её же дёргают тесты и мутационная проверка.

    Принимает именно `bool | None`, а не «что угодно истинное»: `None` из базы
    и `False` должны вести себя одинаково, и оба означают отказ.
    """
    return is_admin is True


async def load_actor(session: AsyncSession, user_id: int) -> Actor:
    """Читает признак администратора из базы при каждом обращении.

    Не из кеша и не из состояния FSM: снятый администратор должен терять доступ
    сразу, а не когда истечёт чей-то кеш.
    """
    admin = await users_repo.get_admin(session, user_id)
    return Actor(user_id=user_id, is_admin=admin is not None)


async def can(session: AsyncSession, user_id: int) -> bool:
    actor = await load_actor(session, user_id)
    return allows(actor.is_admin)
