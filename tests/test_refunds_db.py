"""Возврат по заказу.

Внутреннего баланса у магазина больше нет, и денег бот не двигает: возврат —
это **отметка** о том, что заказ закрыт в пользу покупателя, плюс откат
промокода. Сами деньги владелец переводит сам.

От этого проверяемое не исчезло, а сместилось. Отметка о возврате — основание
для перевода живых денег, поэтому цена ошибки прежняя: вторая отметка по одному
заказу означает второй перевод, а сумма, взятая не из того поля, — перевод не
той суммы.

Замена товара и брак партии проверялись здесь раньше — они убраны вместе со
складом: заказ описывает работу над чужим аккаунтом, заменять в нём нечего.
"""

from __future__ import annotations

import asyncio

import pytest

from bot.db.base import utcnow
from bot.db.models import Order, OrderStatus, PromoCode
from bot.repo import orders as orders_repo
from bot.repo import promo as promo_repo
from bot.services import delivery as delivery_service
from bot.services import orders as orders_service
from bot.services import promo as promo_service
from bot.services import refunds as refunds_service
from bot.utils.money import DISCOUNT_PERCENT
from tests.factories import DEFAULT_RATE_KOP, make_product, make_user

pytestmark = pytest.mark.db

ADMIN_ID = 501
MARKUP_PCT = 10

# Все состояния, в которых деньги уже у магазина. Список записан руками, а не
# взят из refunds_service.REFUNDABLE: тест, который параметризуется тем же
# кортежем, что проверяет, вычеркнет состояние вместе с реализацией и промолчит.
MONEY_HELD = [
    OrderStatus.PAID,
    OrderStatus.AWAITING_CREDENTIALS,
    OrderStatus.IN_WORK,
    OrderStatus.DELIVERED,
]

# Состояния, в которых денег магазин не получал или уже закрыл заказ без них.
NO_MONEY = [
    OrderStatus.NEW,
    OrderStatus.PENDING,
    OrderStatus.CANCELED,
    OrderStatus.EXPIRED,
]


async def _advance_to(session, order: Order, status: str, promo: PromoCode | None) -> None:
    """Доводит заказ до нужного состояния настоящими переходами.

    Статус не присваивается руками (кроме самой оплаты — там граница с
    провайдером): заказ в `in_work` без токена и без реквизитов в бою не
    встречается, и возврат по такой заготовке проверял бы не тот мир.
    """
    if status == OrderStatus.NEW:
        return
    if status == OrderStatus.PENDING:
        await orders_service.attach_payment(
            session, order, "test", "test:card", f"txn-{order.id}", None
        )
        return
    if status in (OrderStatus.CANCELED, OrderStatus.EXPIRED):
        reason = "expired" if status == OrderStatus.EXPIRED else "canceled"
        await orders_service.cancel(session, order, reason=reason)
        return

    # Дальше — оплаченные состояния. Промокод списывается в момент оплаты,
    # ровно как в payments._finalize: иначе возврату нечего было бы откатывать.
    if promo is not None:
        assert await promo_service.consume(session, promo.id, order.user_id, order.id)
    order.status = OrderStatus.PAID
    order.paid_at = utcnow()
    await session.flush()
    if status == OrderStatus.PAID:
        return

    assert (await delivery_service.start(session, order)).ok
    if status == OrderStatus.AWAITING_CREDENTIALS:
        return

    assert (
        await delivery_service.accept_credentials(session, order, "buyer@example.com", "s3cret")
    ).ok
    if status == OrderStatus.IN_WORK:
        return

    assert (await delivery_service.confirm_done(session, order, admin_id=ADMIN_ID)).ok


async def _make_order(session, status: str, promo: PromoCode | None = None, tg_id: int = 1001):
    user = await make_user(session, tg_id=tg_id)
    product = await make_product(session)
    await session.flush()

    order = await orders_service.create_order(
        session,
        user.tg_id,
        product,
        promo=promo,
        rate_kop=DEFAULT_RATE_KOP,
        markup_pct=MARKUP_PCT,
        reserve_minutes=20,
    )
    await _advance_to(session, order, status, promo)
    await session.commit()
    return order, user


# --- возврат возможен из любого оплаченного состояния -----------------------


@pytest.mark.parametrize("status", MONEY_HELD)
async def test_refund_is_allowed_from_every_paid_state(session_factory, status) -> None:
    """Из всех четырёх состояний, где деньги у нас, возврат проходит.

    Заказ, ждущий реквизиты, и заказ в работе входят сюда наравне с
    выполненным. Если договориться не вышло на любом из этих шагов, покупателя
    нельзя оставлять и без денег, и без работы.
    """
    async with session_factory() as session:
        order, _ = await _make_order(session, status)
        order_id, total = order.id, order.total_kop

    async with session_factory() as session:
        result = await refunds_service.mark_refunded(session, order_id, admin_id=ADMIN_ID)
        await session.commit()

        assert result.ok, result.detail
        # Сумма — та, что покупатель заплатил, а не цена товара и не сумма
        # до скидки: именно её владелец переведёт обратно.
        assert result.amount_kop == total

    async with session_factory() as session:
        saved = await orders_repo.get(session, order_id)
        assert saved.status == OrderStatus.REFUNDED
        assert saved.refunded_at is not None


@pytest.mark.parametrize("status", NO_MONEY)
async def test_refund_is_refused_when_no_money_was_taken(session_factory, status) -> None:
    """По неоплаченному и по уже закрытому заказу возврата нет.

    Отметка о возврате — основание для перевода денег. Появившись на заказе,
    за который никто не платил, она приведёт к переводу из своего кармана.
    """
    async with session_factory() as session:
        order, _ = await _make_order(session, status)
        order_id, before = order.id, order.status

    async with session_factory() as session:
        result = await refunds_service.mark_refunded(session, order_id, admin_id=ADMIN_ID)
        await session.commit()
        assert not result.ok
        assert result.amount_kop == 0

    async with session_factory() as session:
        saved = await orders_repo.get(session, order_id)
        assert saved.status == before, "статус заказа изменился при отказе в возврате"
        assert saved.refunded_at is None


async def test_missing_order_is_refused(session_factory) -> None:
    async with session_factory() as session:
        result = await refunds_service.mark_refunded(session, 999_999, admin_id=ADMIN_ID)
        assert not result.ok
        assert "не найден" in (result.detail or "")


# --- дважды не возвращаем ---------------------------------------------------


async def test_second_refund_is_refused(session_factory) -> None:
    """Главный тест файла: вторая отметка — это второй перевод денег."""
    async with session_factory() as session:
        order, _ = await _make_order(session, OrderStatus.DELIVERED)
        order_id, total = order.id, order.total_kop

    async with session_factory() as session:
        first = await refunds_service.mark_refunded(session, order_id, admin_id=ADMIN_ID)
        await session.commit()
        assert first.ok
        assert first.amount_kop == total

    async with session_factory() as session:
        second = await refunds_service.mark_refunded(session, order_id, admin_id=ADMIN_ID)
        await session.commit()
        assert not second.ok
        assert second.amount_kop == 0
        assert "уже" in (second.detail or "")

    async with session_factory() as session:
        saved = await orders_repo.get(session, order_id)
        assert saved.status == OrderStatus.REFUNDED


async def test_two_admins_refunding_at_once_succeed_only_once(session_factory) -> None:
    """Двое администраторов нажали «Вернуть» одновременно.

    Уведомление о заказе уходит всем сразу, так что это не редкость. Без
    блокировки строки обе проверки увидят заказ невозвращённым, и владелец
    переведёт деньги дважды.
    """
    async with session_factory() as session:
        order, _ = await _make_order(session, OrderStatus.IN_WORK)
        order_id = order.id

    async def refund(admin_id: int) -> bool:
        async with session_factory() as session:
            result = await refunds_service.mark_refunded(session, order_id, admin_id=admin_id)
            await session.commit()
            return result.ok

    results = await asyncio.gather(refund(501), refund(502))
    assert sorted(results) == [False, True], f"возврат прошёл дважды: {results}"


# --- промокод возвращается покупателю ---------------------------------------


async def test_refund_releases_the_promo_code(session_factory) -> None:
    """Возврат откатывает использование промокода.

    Иначе покупатель теряет и работу, и промокод: заказ отменили, а
    одноразовый код числится потраченным.
    """
    async with session_factory() as session:
        promo = PromoCode(
            code="ONCE",
            discount_type=DISCOUNT_PERCENT,
            discount_value=10,
            usage_limit=1,
        )
        session.add(promo)
        await session.flush()
        promo_id = promo.id

        order, user = await _make_order(session, OrderStatus.DELIVERED, promo=promo)
        order_id, user_id = order.id, user.tg_id

    async with session_factory() as session:
        used = await promo_repo.uses_total(session, promo_id)
        assert used == 1, "промокод не списался при оплате — тест ничего не проверит"

    async with session_factory() as session:
        assert (await refunds_service.mark_refunded(session, order_id, admin_id=ADMIN_ID)).ok
        await session.commit()

    async with session_factory() as session:
        assert await promo_repo.uses_total(session, promo_id) == 0
        # И код снова годен тому же покупателю.
        check = await promo_service.validate(session, "ONCE", user_id)
        assert check.ok, check.reason


# --- пометка администратора -------------------------------------------------


async def test_comment_is_saved_on_the_order(session_factory) -> None:
    """Причина возврата остаётся в карточке заказа.

    Возврат делают руками и деньгами; через месяц вопрос «за что вернули»
    возникнет обязательно, и ответ должен лежать в заказе, а не в переписке.
    """
    async with session_factory() as session:
        order, _ = await _make_order(session, OrderStatus.IN_WORK)
        order_id = order.id

    async with session_factory() as session:
        result = await refunds_service.mark_refunded(
            session, order_id, admin_id=ADMIN_ID, comment="Не смогли войти в аккаунт"
        )
        await session.commit()
        assert result.ok

    async with session_factory() as session:
        saved = await orders_repo.get(session, order_id)
        assert saved.admin_note == "Не смогли войти в аккаунт"


async def test_long_comment_is_cut_to_the_column(session_factory) -> None:
    """Длинная причина обрезается, а не роняет операцию.

    Колонка на 255 символов; администратор об этом не знает и пишет сколько
    напишется. Падение здесь стоило бы отменённого возврата.
    """
    async with session_factory() as session:
        order, _ = await _make_order(session, OrderStatus.PAID)
        order_id = order.id

    async with session_factory() as session:
        assert (
            await refunds_service.mark_refunded(
                session, order_id, admin_id=ADMIN_ID, comment="я" * 400
            )
        ).ok
        await session.commit()

    async with session_factory() as session:
        saved = await orders_repo.get(session, order_id)
        assert len(saved.admin_note) == 255


# --- признак для кнопки -----------------------------------------------------


@pytest.mark.parametrize("status", MONEY_HELD)
def test_button_is_offered_where_money_is_held(status) -> None:
    assert refunds_service.order_can_be_refunded(Order(status=status)) is True


@pytest.mark.parametrize("status", [*NO_MONEY, OrderStatus.REFUNDED])
def test_button_is_hidden_where_there_is_nothing_to_return(status) -> None:
    assert refunds_service.order_can_be_refunded(Order(status=status)) is False
