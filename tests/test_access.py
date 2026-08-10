"""Права доступа: четыре двери и отказ по умолчанию."""

from __future__ import annotations

import pytest

from bot.db.models import AdminRole
from bot.services.access import DOORS, PERMISSIONS, allows

OWNER_ONLY_SECTIONS = ("promo", "balance", "texts", "settings", "channels", "admins", "audit")
SHARED_SECTIONS = ("categories", "products", "stock", "orders", "broadcast", "users", "export")


@pytest.mark.parametrize("section", OWNER_ONLY_SECTIONS)
@pytest.mark.parametrize("door", DOORS)
def test_admin_cannot_touch_owner_sections(section: str, door: str) -> None:
    """Проверяем каждую дверь отдельно.

    Защита на трёх дверях из четырёх — это дыра: список утекает молча, потому
    что кнопки в интерфейсе нет, а callback-запрос отправить может кто угодно.
    """
    assert allows(AdminRole.ADMIN, section, door) is False
    assert allows(AdminRole.OWNER, section, door) is True


@pytest.mark.parametrize("section", SHARED_SECTIONS)
def test_admin_can_work_in_shared_sections(section: str) -> None:
    for door in ("view", "list", "act"):
        assert allows(AdminRole.ADMIN, section, door) is True


@pytest.mark.parametrize("section", [*OWNER_ONLY_SECTIONS, *SHARED_SECTIONS])
@pytest.mark.parametrize("door", DOORS)
def test_stranger_is_denied_everywhere(section: str, door: str) -> None:
    assert allows(None, section, door) is False
    assert allows("", section, door) is False
    assert allows("superuser", section, door) is False


def test_unknown_section_is_denied() -> None:
    """Ветка else, ставящая «разрешено», открывает то, что должна закрывать."""
    assert allows(AdminRole.OWNER, "secret_backdoor", "act") is False
    assert allows(AdminRole.ADMIN, "secret_backdoor", "act") is False


def test_unknown_door_is_denied() -> None:
    assert allows(AdminRole.OWNER, "orders", "delete_everything") is False


def test_every_section_declares_all_four_doors() -> None:
    """Незаявленная дверь — это дыра, которую не видно при чтении таблицы."""
    for section, doors in PERMISSIONS.items():
        missing = set(DOORS) - set(doors)
        assert not missing, f"у раздела {section} не описаны двери: {sorted(missing)}"
