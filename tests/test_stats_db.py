"""Статистика и шапка админ-панели.

Файл появился после двух одинаковых аварий. Обе — не логика, а имена: код
обращался к полю `Snapshot`, удалённому вместе со складом, и к переменной,
убранной вместе с видами заказа. Оба раза бот собирался, импортировался,
проходил всю сюиту — и падал ровно в тот момент, когда администратор нажимал
кнопку. Со стороны это выглядело как «ничего не происходит»: исключение
уходило в журнал, а пользователю не приходило ничего.

Ловится такое одним способом — вызвать функцию целиком и на настоящей базе.
Поэтому здесь нет тонких проверок цифр: тесты собирают снимок и обе строки,
которые из него делаются, и требуют, чтобы это просто отработало.
"""

from __future__ import annotations

import pytest

from bot.db.base import utcnow
from bot.db.models import OrderStatus
from bot.handlers.admin.menu import _summary
from bot.services import delivery as delivery_service
from bot.services import orders as orders_service
from bot.services import stats as stats_service
from bot.services.access import Actor
from tests.factories import DEFAULT_RATE_KOP, make_product, make_user

pytestmark = pytest.mark.db

ADMIN = Actor(user_id=100000001, is_admin=True)


async def _order_in(session, status: str, tg_id: int):
    user = await make_user(session, tg_id=tg_id)
    product = await make_product(session)
    await session.flush()
    order = await orders_service.create_order(
        session,
        user.tg_id,
        product,
        promo=None,
        rate_kop=DEFAULT_RATE_KOP,
        markup_pct=10,
        reserve_minutes=20,
    )
    if status == OrderStatus.NEW:
        return order
    order.status = OrderStatus.PAID
    order.paid_at = utcnow()
    await session.flush()
    assert (await delivery_service.start(session, order)).ok
    if status == OrderStatus.AWAITING_CREDENTIALS:
        return order
    assert (await delivery_service.accept_credentials(session, order, "log", "pass")).ok
    if status == OrderStatus.IN_WORK:
        return order
    assert (await delivery_service.confirm_done(session, order, admin_id=ADMIN.user_id)).ok
    return order


async def test_snapshot_collects_on_an_empty_shop(session_factory) -> None:
    """Пустой магазин — тоже рабочий случай.

    Первый запуск бота происходит именно в нём, и падение здесь означает, что
    админ-панель не открывается вообще ни разу.
    """
    async with session_factory() as session:
        snapshot = await stats_service.collect(session)
        assert snapshot.users_total == 0
        assert snapshot.orders_total == 0
        assert snapshot.revenue_kop == 0
        assert snapshot.orders_awaiting_credentials == 0
        assert snapshot.orders_in_work == 0


async def test_admin_header_builds_on_an_empty_shop(session_factory) -> None:
    """Шапка админ-панели собирается — та самая, что дважды падала."""
    async with session_factory() as session:
        snapshot = await stats_service.collect(session)
        text = _summary(ADMIN, snapshot)

        assert "Админ-панель" in text
        assert str(ADMIN.user_id) in text
        # Когда работы нет, об этом сказано явно: пустая шапка читается как
        # «данные не загрузились».
        assert "Незакрытых заказов нет" in text


async def test_full_snapshot_formats(session_factory) -> None:
    """`format_snapshot` обходит все поля снимка.

    Именно она первой ломается, когда поле переименовали: раздел статистики
    открывают реже, чем меню, и ошибку замечают позже.
    """
    async with session_factory() as session:
        snapshot = await stats_service.collect(session)
        text = stats_service.format_snapshot(snapshot)

        assert "Статистика" in text
        assert "Пользователи" in text
        assert "Продажи" in text
        assert "В работе" in text


async def test_counters_follow_real_orders(session_factory) -> None:
    """Числа считаются по настоящим заказам, проведённым через переходы.

    Заказы не расставляются по статусам руками: снимок обязан сходиться
    с тем, что получается при обычной работе магазина.
    """
    async with session_factory() as session:
        await _order_in(session, OrderStatus.AWAITING_CREDENTIALS, tg_id=2001)
        await _order_in(session, OrderStatus.IN_WORK, tg_id=2002)
        await _order_in(session, OrderStatus.IN_WORK, tg_id=2003)
        await _order_in(session, OrderStatus.DELIVERED, tg_id=2004)
        await _order_in(session, OrderStatus.NEW, tg_id=2005)
        await session.commit()

    async with session_factory() as session:
        snapshot = await stats_service.collect(session)

        assert snapshot.users_total == 5
        assert snapshot.orders_total == 5
        assert snapshot.orders_awaiting_credentials == 1
        assert snapshot.orders_in_work == 2

        text = _summary(ADMIN, snapshot)
        assert "Ждут логин и пароль: 1" in text
        assert "В работе: 2" in text
        assert "Незакрытых заказов нет" not in text

        # И полный отчёт тоже собирается на непустых данных.
        assert "Статистика" in stats_service.format_snapshot(snapshot)


async def test_revenue_counts_paid_orders_only(session_factory) -> None:
    """Неоплаченный заказ в выручку не попадает."""
    async with session_factory() as session:
        delivered = await _order_in(session, OrderStatus.DELIVERED, tg_id=3001)
        await _order_in(session, OrderStatus.NEW, tg_id=3002)
        await session.commit()
        expected = delivered.total_kop

    async with session_factory() as session:
        snapshot = await stats_service.collect(session)
        assert snapshot.revenue_kop == expected
