"""Права доступа в админ-панель.

Проверка выполняется на **четырёх дверях** отдельно:

* `VIEW`   — открыть одну запись;
* `LIST`   — показать список;
* `ACT`    — выполнить действие над существующей записью;
* `CREATE` — создать новую.

Защита на трёх дверях из четырёх — это дыра: список утекает молча, и никто не
замечает, потому что кнопки в интерфейсе нет, а callback-запрос отправить может
кто угодно.

Умолчание — отказ. Неизвестный раздел или неизвестная дверь запрещены: ветка
`else`, ставящая «разрешено», по умолчанию открывает то, что должна закрывать.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import AdminRole
from bot.repo import users as users_repo

VIEW = "view"
LIST = "list"
ACT = "act"
CREATE = "create"

DOORS = (VIEW, LIST, ACT, CREATE)

OWNER_ONLY = frozenset({AdminRole.OWNER})
ANY_ADMIN = frozenset({AdminRole.OWNER, AdminRole.ADMIN})

# Раздел → дверь → роли, которым дверь открыта.
# Разделы, которых здесь нет, закрыты для всех.
PERMISSIONS: dict[str, dict[str, frozenset[str]]] = {
    "categories": {VIEW: ANY_ADMIN, LIST: ANY_ADMIN, ACT: ANY_ADMIN, CREATE: ANY_ADMIN},
    "products": {VIEW: ANY_ADMIN, LIST: ANY_ADMIN, ACT: ANY_ADMIN, CREATE: ANY_ADMIN},
    "stock": {VIEW: ANY_ADMIN, LIST: ANY_ADMIN, ACT: ANY_ADMIN, CREATE: ANY_ADMIN},
    "orders": {VIEW: ANY_ADMIN, LIST: ANY_ADMIN, ACT: ANY_ADMIN, CREATE: OWNER_ONLY},
    "broadcast": {VIEW: ANY_ADMIN, LIST: ANY_ADMIN, ACT: ANY_ADMIN, CREATE: ANY_ADMIN},
    "stats": {VIEW: ANY_ADMIN, LIST: ANY_ADMIN, ACT: OWNER_ONLY, CREATE: OWNER_ONLY},
    "users": {VIEW: ANY_ADMIN, LIST: ANY_ADMIN, ACT: ANY_ADMIN, CREATE: OWNER_ONLY},
    "export": {VIEW: ANY_ADMIN, LIST: ANY_ADMIN, ACT: ANY_ADMIN, CREATE: ANY_ADMIN},
    # Ниже — то, что меняет деньги, доступ и лицо магазина. Только владелец.
    "promo": {VIEW: OWNER_ONLY, LIST: OWNER_ONLY, ACT: OWNER_ONLY, CREATE: OWNER_ONLY},
    "balance": {VIEW: OWNER_ONLY, LIST: OWNER_ONLY, ACT: OWNER_ONLY, CREATE: OWNER_ONLY},
    "texts": {VIEW: OWNER_ONLY, LIST: OWNER_ONLY, ACT: OWNER_ONLY, CREATE: OWNER_ONLY},
    "settings": {VIEW: OWNER_ONLY, LIST: OWNER_ONLY, ACT: OWNER_ONLY, CREATE: OWNER_ONLY},
    "channels": {VIEW: OWNER_ONLY, LIST: OWNER_ONLY, ACT: OWNER_ONLY, CREATE: OWNER_ONLY},
    "admins": {VIEW: OWNER_ONLY, LIST: OWNER_ONLY, ACT: OWNER_ONLY, CREATE: OWNER_ONLY},
    "audit": {VIEW: OWNER_ONLY, LIST: OWNER_ONLY, ACT: OWNER_ONLY, CREATE: OWNER_ONLY},
}


@dataclass(frozen=True)
class Actor:
    user_id: int
    role: str | None

    @property
    def is_admin(self) -> bool:
        return self.role in (AdminRole.OWNER, AdminRole.ADMIN)

    @property
    def is_owner(self) -> bool:
        return self.role == AdminRole.OWNER


def allows(role: str | None, section: str, door: str) -> bool:
    """Чистая функция проверки — её же дёргают тесты и мутационная проверка."""
    if role is None:
        return False
    doors = PERMISSIONS.get(section)
    if doors is None:
        return False
    allowed = doors.get(door)
    if allowed is None:
        return False
    return role in allowed


async def load_actor(session: AsyncSession, user_id: int) -> Actor:
    """Читает роль из базы при каждом обращении.

    Не из кеша и не из состояния FSM: снятый администратор должен терять доступ
    сразу, а не когда истечёт чей-то кеш.
    """
    admin = await users_repo.get_admin(session, user_id)
    return Actor(user_id=user_id, role=admin.role if admin else None)


async def can(session: AsyncSession, user_id: int, section: str, door: str) -> bool:
    actor = await load_actor(session, user_id)
    return allows(actor.role, section, door)
