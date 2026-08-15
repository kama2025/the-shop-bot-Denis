"""Возврат денег на баланс.

Возврат — единственная операция магазина, которая создаёт деньги на балансе
без входящего платежа. Ошибку здесь не поймает сверка с провайдером: в
эквайринге не происходит ничего, а на балансе появляются рубли, которыми
покупатель оплатит следующий заказ. Поэтому проверяется не «работает ли
возврат», а его границы: из каких состояний он вообще допустим, сколько именно
зачисляет и почему не срабатывает дважды.

Замена товара и брак партии проверялись здесь раньше — они убраны вместе со
складом: заказ описывает работу над чужим аккаунтом, заменять в нём нечего.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from bot.db.base import utcnow
from bot.db.models import (
    BalanceTxn,
    BalanceTxnKind,
    Order,
    OrderKind,
    OrderStatus,
    PromoCode,
)
from bot.repo import balance as balance_repo
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

START_BALANCE_KOP = 5000
"""Стартовый баланс намеренно не нулевой.

При нулевом «прибавить сумму заказа» и «записать сумму заказа» дают один и тот
же результат, и подмена сложения присваиванием прошла бы мимо тестов.
"""

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
    """Покупатель с деньгами на балансе и его заказ в состоянии `status`."""
    user = await make_user(session, tg_id=tg_id, balance_kop=START_BALANCE_KOP)
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


async def _refund_txn_count(session, user_id: int) -> int:
    stmt = select(func.count(BalanceTxn.id)).where(
        BalanceTxn.user_id == user_id, BalanceTxn.kind == BalanceTxnKind.REFUND
    )
    return int((await session.execute(stmt)).scalar_one())


# --- деньги возвращаются из любого оплаченного состояния --------------------


@pytest.mark.parametrize("status", MONEY_HELD)
async def test_refund_credits_balance_from_every_paid_state(session_factory, status) -> None:
    """Из всех четырёх состояний деньги обязаны вернуться одинаково.

    Ждущий реквизиты и взятый в работу заказ — не «недооплаченные»: деньги
    списаны в момент оплаты. Если возврат из них не пройдёт, единственным
    выходом останется ручной перевод мимо леджера, и баланс перестанет
    сходиться с историей.
    """
    async with session_factory() as session:
        order, user = await _make_order(session, status)
        total = order.total_kop

        result = await refunds_service.refund_to_balance(session, order.id, admin_id=ADMIN_ID)
        await session.commit()

        assert result.ok is True
        assert result.amount_kop == total

        await session.refresh(order)
        await session.refresh(user)
        assert order.status == OrderStatus.REFUNDED
        assert order.refunded_at is not None
        assert user.balance_kop == START_BALANCE_KOP + total
        # Леджер — источник правды; кеш в users обязан совпасть с ним.
        assert await balance_repo.ledger_balance(session, user.tg_id) == START_BALANCE_KOP + total


async def test_refund_credits_order_total_not_subtotal(session_factory) -> None:
    """Зачисляется ровно то, что покупатель заплатил, — итог со скидкой.

    Со скидкой у заказа три разных числа: цена за единицу, сумма до скидки и
    итог. Возврат по subtotal вернул бы больше денег, чем магазин получил, и
    промокод превратился бы в способ зарабатывать на возвратах.
    """
    async with session_factory() as session:
        promo = PromoCode(
            code="BACK10", discount_type=DISCOUNT_PERCENT, discount_value=10, usage_limit=5
        )
        session.add(promo)
        order, user = await _make_order(session, OrderStatus.DELIVERED, promo=promo)

        assert order.discount_kop > 0, "заготовка без скидки ничего бы не проверила"
        assert order.total_kop < order.subtotal_kop == order.unit_price_kop

        result = await refunds_service.refund_to_balance(session, order.id, admin_id=ADMIN_ID)
        await session.commit()

        assert result.amount_kop == order.total_kop
        await session.refresh(user)
        assert user.balance_kop == START_BALANCE_KOP + order.total_kop


# --- возврат того, чего не платили ------------------------------------------


@pytest.mark.parametrize("status", NO_MONEY)
async def test_refund_refused_when_money_was_never_taken(session_factory, status) -> None:
    """Заказ без денег возврату не подлежит.

    Неоплаченный и отменённый заказ живут в базе рядом с оплаченными и
    открываются той же карточкой. Возврат по ним — чистая эмиссия: на балансе
    появляются рубли, за которые магазин ничего не получал.
    """
    async with session_factory() as session:
        order, user = await _make_order(session, status)

        result = await refunds_service.refund_to_balance(session, order.id, admin_id=ADMIN_ID)
        await session.commit()

        assert result.ok is False
        assert result.amount_kop == 0

        await session.refresh(order)
        await session.refresh(user)
        assert order.status == status, "отказ не должен менять состояние заказа"
        assert order.refunded_at is None
        assert user.balance_kop == START_BALANCE_KOP
        assert await balance_repo.ledger_balance(session, user.tg_id) == START_BALANCE_KOP
        assert await _refund_txn_count(session, user.tg_id) == 0


async def test_refund_refuses_unknown_order(session_factory) -> None:
    """Опечатка в номере заказа обязана давать отказ, а не исключение.

    Номер админ вводит руками; необработанное исключение здесь оставило бы его
    без ответа — и он нажал бы «Возврат» ещё раз.
    """
    async with session_factory() as session:
        result = await refunds_service.refund_to_balance(session, 999_999, admin_id=ADMIN_ID)
        assert result.ok is False
        assert result.amount_kop == 0


# --- двойной возврат --------------------------------------------------------


async def test_second_refund_does_not_pay_twice(session_factory) -> None:
    """Главный тест файла: два возврата по одному заказу — потеря денег.

    Кнопка «Возврат» живёт в карточке заказа, карточка после возврата никуда
    не исчезает, а Telegram доставляет нажатия по одному на каждое касание.
    Второй возврат обязан упереться в статус `refunded`, а не зачислить сумму
    ещё раз: пропажу увидят только при сверке кассы, если увидят вообще.
    """
    async with session_factory() as session:
        order, user = await _make_order(session, OrderStatus.DELIVERED)
        total = order.total_kop

        first = await refunds_service.refund_to_balance(session, order.id, admin_id=ADMIN_ID)
        await session.commit()
        assert first.ok is True

        second = await refunds_service.refund_to_balance(session, order.id, admin_id=ADMIN_ID)
        await session.commit()
        assert second.ok is False
        assert second.amount_kop == 0
        assert "уже" in (second.detail or ""), "админу надо сказать, что возврат уже был"

        await session.refresh(user)
        assert user.balance_kop == START_BALANCE_KOP + total
        assert await balance_repo.ledger_balance(session, user.tg_id) == START_BALANCE_KOP + total
        # Одна проводка, а не две: кеш баланса мог бы сойтись и при двух.
        assert await _refund_txn_count(session, user.tg_id) == 1


async def test_parallel_refunds_pay_once(session_factory) -> None:
    """Два админа нажали «Возврат» одновременно.

    Последовательная проверка «статус не refunded» этот случай не ловит: обе
    транзакции читают ещё не возвращённый заказ и обе зачисляют деньги.
    Спасает только блокировка строки заказа в `get_for_update`.
    """
    async with session_factory() as session:
        order, user = await _make_order(session, OrderStatus.DELIVERED)
        order_id, user_id, total = order.id, user.tg_id, order.total_kop

    async def refund() -> bool:
        async with session_factory() as session:
            result = await refunds_service.refund_to_balance(session, order_id, admin_id=ADMIN_ID)
            await session.commit()
            return result.ok

    results = await asyncio.gather(refund(), refund())
    assert sorted(results) == [False, True], f"получили {results}"

    async with session_factory() as session:
        assert await balance_repo.ledger_balance(session, user_id) == START_BALANCE_KOP + total
        assert await _refund_txn_count(session, user_id) == 1
        assert await balance_repo.find_mismatches(session) == []


# --- промокод ---------------------------------------------------------------


async def test_refund_returns_promo_use(session_factory) -> None:
    """Возврат обязан вернуть и промокод.

    Иначе покупатель теряет дважды: работу ему не сделали, а одноразовая
    скидка списана — второй раз тем же кодом он не купит.
    """
    async with session_factory() as session:
        promo = PromoCode(
            code="BACK10",
            discount_type=DISCOUNT_PERCENT,
            discount_value=10,
            usage_limit=5,
            per_user_limit=1,
        )
        session.add(promo)
        order, user = await _make_order(session, OrderStatus.IN_WORK, promo=promo)

        await session.refresh(promo)
        assert promo.used_count == 1
        assert await promo_repo.uses_total(session, promo.id) == 1
        # До возврата код исчерпан для этого покупателя — с этого и начинается
        # его потеря.
        spent = await promo_service.validate(session, promo.code, user.tg_id)
        assert spent.ok is False
        assert spent.reason == promo_service.REASON_USER_LIMIT

        assert (
            await refunds_service.refund_to_balance(session, order.id, admin_id=ADMIN_ID)
        ).ok is True
        await session.commit()

        await session.refresh(promo)
        assert promo.used_count == 0
        assert await promo_repo.uses_total(session, promo.id) == 0
        # Главное следствие: тем же кодом можно купить снова.
        again = await promo_service.validate(session, promo.code, user.tg_id)
        assert again.ok is True


# --- след в журнале ---------------------------------------------------------


async def test_refund_records_who_and_why(session_factory) -> None:
    """Проводка обязана хранить админа, заказ и причину.

    Возврат делает человек, и разбирают его тоже люди. Зачисление без автора и
    без ссылки на заказ невозможно оспорить: по леджеру видно только, что
    деньги взялись.
    """
    async with session_factory() as session:
        order, user = await _make_order(session, OrderStatus.PAID)

        await refunds_service.refund_to_balance(
            session, order.id, admin_id=ADMIN_ID, comment="работу не сделали"
        )
        await session.commit()

        txn = (
            await session.execute(
                select(BalanceTxn).where(
                    BalanceTxn.user_id == user.tg_id, BalanceTxn.kind == BalanceTxnKind.REFUND
                )
            )
        ).scalar_one()
        assert txn.amount_kop == order.total_kop
        assert txn.order_id == order.id
        assert txn.admin_id == ADMIN_ID
        assert txn.comment == "работу не сделали"
        assert txn.balance_after_kop == START_BALANCE_KOP + order.total_kop

        await session.refresh(order)
        assert order.admin_note == "работу не сделали"


# --- договор о состояниях ---------------------------------------------------


def test_refundable_covers_every_state_where_money_is_held() -> None:
    """Список возврата обязан совпадать с «деньги у нас».

    Он задан отдельным кортежем от OrderStatus.ACTIVE, и при добавлении нового
    оплаченного состояния его легко забыть — тогда по таким заказам вернуть
    деньги будет нечем. Обратная ошибка дороже: неоплаченное состояние,
    попавшее в список, разрешает возврат по счёту, который никто не оплачивал.
    """
    assert set(refunds_service.REFUNDABLE) == set(OrderStatus.ACTIVE) | {OrderStatus.DELIVERED}


def test_every_order_status_lands_in_one_of_the_two_lists() -> None:
    """Каждое состояние заказа обязано быть отнесено к «деньги у нас» или нет.

    Оба параметризованных теста ходят по спискам `MONEY_HELD` и `NO_MONEY`,
    записанным в этом файле руками. Новое состояние заказа не попадёт ни в
    один из них — и не проверится ничем. Соседний тест эту дыру не закрывает:
    он смотрит только на `OrderStatus.ACTIVE`, а состояние можно добавить и
    мимо этой группы.
    """
    assert set(MONEY_HELD) & set(NO_MONEY) == set(), "состояние попало в оба списка"
    assert set(MONEY_HELD) | set(NO_MONEY) | {OrderStatus.REFUNDED} == set(OrderStatus.ALL)


# --- кнопка «Вернуть деньги» ------------------------------------------------


@pytest.mark.parametrize("status", OrderStatus.ALL)
def test_refund_button_is_offered_exactly_where_refund_works(status) -> None:
    """Предикат кнопки обязан совпадать с тем, что сделает сама операция.

    Карточка заказа рисует «Вернуть деньги» по `order_can_be_refunded`
    (`bot/handlers/admin/orders.py`), а решает всё `refund_to_balance`. Пока
    эту функцию не звал ни один тест, она могла возвращать что угодно: и
    `return True`, и «всё, кроме уже возвращённого» проходили мимо файла
    целиком. Разъезд в одну сторону даёт кнопку, которая всегда отвечает
    отказом, в другую — прячет возврат там, где он нужен.
    """
    offered = refunds_service.order_can_be_refunded(Order(status=status))
    assert offered is (status in MONEY_HELD), f"состояние {status}"


async def test_refund_button_goes_away_after_the_refund(session_factory) -> None:
    """Возврат сделан — кнопка обязана исчезнуть с той же карточки.

    Админ возвращает деньги, карточка перерисовывается из того же заказа. Если
    кнопка останется, следующее нажатие упрётся в отказ вместо того, чтобы
    просто не существовать, — а по виду карточки возврат будет выглядеть
    несделанным.
    """
    async with session_factory() as session:
        order, _ = await _make_order(session, OrderStatus.DELIVERED)
        assert refunds_service.order_can_be_refunded(order) is True

        assert (
            await refunds_service.refund_to_balance(session, order.id, admin_id=ADMIN_ID)
        ).ok is True
        await session.commit()

        await session.refresh(order)
        assert refunds_service.order_can_be_refunded(order) is False


# --- возврат не задевает соседей --------------------------------------------


async def test_two_purchases_of_one_buyer_are_refunded_separately(session_factory) -> None:
    """Постоянный покупатель: две покупки, обе обязаны вернуться.

    Отсечка двойного возврата привязана к заказу, а не к человеку. Привязка к
    покупателю выглядит такой же защитой от второго нажатия, но молча съедает
    деньги за вторую покупку. Выше этого не видно: ни один тест не даёт одному
    покупателю два заказа, а `_refund_txn_count` считает проводки по
    покупателю — при одном заказе оба правила неотличимы.
    """
    async with session_factory() as session:
        first, user = await _make_order(session, OrderStatus.DELIVERED)

        product = await make_product(session, title="Вторая работа")
        await session.flush()
        second = await orders_service.create_order(
            session,
            user.tg_id,
            product,
            promo=None,
            rate_kop=DEFAULT_RATE_KOP,
            markup_pct=MARKUP_PCT,
            reserve_minutes=20,
        )
        await _advance_to(session, second, OrderStatus.DELIVERED, None)
        await session.commit()

        one = await refunds_service.refund_to_balance(session, first.id, admin_id=ADMIN_ID)
        await session.commit()
        two = await refunds_service.refund_to_balance(session, second.id, admin_id=ADMIN_ID)
        await session.commit()

        assert one.ok is True
        assert two.ok is True, f"вторую покупку вернуть не дали: {two.detail}"

        await session.refresh(user)
        expected = START_BALANCE_KOP + first.total_kop + second.total_kop
        assert user.balance_kop == expected
        assert await balance_repo.ledger_balance(session, user.tg_id) == expected
        assert await _refund_txn_count(session, user.tg_id) == 2


async def test_refund_returns_only_this_orders_promo_use(session_factory) -> None:
    """Возврат отпускает промокод этого заказа, а не весь код целиком.

    Одним кодом пользуются все покупатели. Откат «по промокоду» вместо «по
    заказу» раздаёт чужие использования обратно: одноразовый код, которым уже
    расплатились сто человек, снова становится годным для каждого из них.
    Тест выше этого не различает — там один покупатель и одно использование.
    """
    async with session_factory() as session:
        promo = PromoCode(
            code="SHARED10",
            discount_type=DISCOUNT_PERCENT,
            discount_value=10,
            usage_limit=5,
            per_user_limit=1,
        )
        session.add(promo)
        refunded_order, buyer = await _make_order(
            session, OrderStatus.IN_WORK, promo=promo, tg_id=1001
        )
        _, neighbour = await _make_order(session, OrderStatus.IN_WORK, promo=promo, tg_id=2002)

        await session.refresh(promo)
        assert promo.used_count == 2
        assert await promo_repo.uses_total(session, promo.id) == 2

        assert (
            await refunds_service.refund_to_balance(session, refunded_order.id, admin_id=ADMIN_ID)
        ).ok is True
        await session.commit()

        await session.refresh(promo)
        assert promo.used_count == 1, "возврат одного заказа сбросил счётчик всего кода"
        assert await promo_repo.uses_total(session, promo.id) == 1
        assert await promo_repo.uses_by_user(session, promo.id, neighbour.tg_id) == 1

        # Своё использование вернулось, чужое — на месте.
        assert (await promo_service.validate(session, promo.code, buyer.tg_id)).ok is True
        stranger = await promo_service.validate(session, promo.code, neighbour.tg_id)
        assert stranger.ok is False
        assert stranger.reason == promo_service.REASON_USER_LIMIT

        await session.refresh(neighbour)
        assert neighbour.balance_kop == START_BALANCE_KOP, "деньги ушли не тому покупателю"


# --- причина возврата длиннее колонки ---------------------------------------


async def test_refund_survives_a_comment_longer_than_the_column(session_factory) -> None:
    """Причину админ пишет сообщением в Telegram — до 4096 символов.

    `Order.admin_note` и `BalanceTxn.comment` — VARCHAR(255), а MySQL поднята в
    STRICT_TRANS_TABLES: длинная строка не обрежется молча, а свалит вставку
    прямо в середине возврата. Админ увидит трейсбек вместо ответа и нажмёт
    «Возврат» ещё раз. Обрезка стоит в двух местах сразу — в `refunds` и в
    `balance.move` — и обе не были прикрыты ничем.
    """
    async with session_factory() as session:
        order, user = await _make_order(session, OrderStatus.IN_WORK)
        comment = "исполнитель пропал и на сообщения не отвечает вторую неделю; " * 20
        assert len(comment) > 255, "короткий комментарий ничего бы не проверил"

        result = await refunds_service.refund_to_balance(
            session, order.id, admin_id=ADMIN_ID, comment=comment
        )
        await session.commit()

        assert result.ok is True
        assert result.amount_kop == order.total_kop

        await session.refresh(order)
        assert order.admin_note, "причина потерялась целиком"
        assert comment.startswith(order.admin_note), "в пометке оказался не начало причины"

        txn = (
            await session.execute(
                select(BalanceTxn).where(
                    BalanceTxn.user_id == user.tg_id, BalanceTxn.kind == BalanceTxnKind.REFUND
                )
            )
        ).scalar_one()
        assert txn.comment and comment.startswith(txn.comment)

        await session.refresh(user)
        assert user.balance_kop == START_BALANCE_KOP + order.total_kop


# --- пополнение баланса — не покупка ----------------------------------------


async def test_refund_of_topup_does_not_double_the_balance(session_factory) -> None:
    """Пополнение баланса — тоже заказ, и он попадает в REFUNDABLE.

    Оплаченное пополнение заканчивается в `delivered`, а деньги уже лежат на
    балансе. Возврат по такому заказу зачисляет их второй раз: покупатель
    платит 1000 ₽ и получает 2000 ₽, которыми оплатит следующие покупки.
    Заказ на пополнение виден в общем списке админки, и кнопка «Возврат» у
    него активна.
    """
    async with session_factory() as session:
        user = await make_user(session)
        amount_kop = 100_000
        order = Order(
            user_id=user.tg_id,
            kind=OrderKind.TOPUP,
            product_title="Пополнение баланса",
            qty=1,
            unit_price_kop=amount_kop,
            subtotal_kop=amount_kop,
            discount_kop=0,
            total_kop=amount_kop,
            status=OrderStatus.NEW,
        )
        session.add(order)
        await session.flush()

        # Ровно то, что делает payments._finalize при подтверждённой оплате.
        await balance_repo.move(
            session,
            user_id=user.tg_id,
            amount_kop=amount_kop,
            kind=BalanceTxnKind.TOPUP,
            order_id=order.id,
            comment=f"Пополнение по заказу #{order.id}",
        )
        order.status = OrderStatus.DELIVERED
        order.delivered_at = utcnow()
        await session.commit()

        await refunds_service.refund_to_balance(session, order.id, admin_id=ADMIN_ID)
        await session.commit()

        await session.refresh(user)
        # Отказ оставит на балансе внесённую сумму, списание — ноль. Больше
        # внесённого не должно остаться ни при каком решении.
        assert user.balance_kop <= amount_kop
