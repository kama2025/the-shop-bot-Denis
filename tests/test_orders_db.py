"""Заказ на настоящей MySQL: снимок цены, отказы, отмена, истечение.

Магазин продаёт работу над аккаунтом покупателя. Склада нет, резервировать
нечего — единственное, что заказ «держит», это **замороженная цена**. Поэтому
здесь проверяется не остаток, а два свойства, потеря которых стоит денег:

* число, названное покупателю, не меняется под ним, когда меняется курс;
* оплаченный заказ не закрывается сам по таймауту, пока работа не сделана.

Промокоды и баланс остались в этом же файле: их защита — про одновременные
запросы, а такое проверяется только на настоящей СУБД.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from bot.db.base import utcnow
from bot.db.models import BalanceTxnKind, Order, OrderStatus, PromoCode
from bot.repo import balance as balance_repo
from bot.repo import orders as orders_repo
from bot.repo import promo as promo_repo
from bot.repo import rates as rates_repo
from bot.services import delivery as delivery_service
from bot.services import orders as orders_service
from bot.utils.money import DISCOUNT_FIXED, DISCOUNT_PERCENT
from tests.factories import (
    DEFAULT_RATE_KOP,
    make_paid_order,
    make_product,
    make_rate,
    make_user,
)

pytestmark = pytest.mark.db


# Числа заданы литералами, а не вызовом pricing: тест обязан знать правильный
# ответ сам. Считай он ту же формулу, что и код, — согласился бы с любой её
# заменой.
PRICE_USD_CENTS = 2000  # $20.00
RATE_KOP = DEFAULT_RATE_KOP  # 90,00 ₽ за доллар
MARKUP_PCT = 10
BASE_KOP = 180_000  # $20 × 90 ₽ = 1800 ₽
TOTAL_KOP = 198_000  # +10 % = 1980 ₽


async def _create(
    session,
    user,
    product,
    *,
    promo: PromoCode | None = None,
    rate_kop: int = RATE_KOP,
    markup_pct: int = MARKUP_PCT,
    reserve_minutes: int = 20,
) -> Order:
    return await orders_service.create_order(
        session,
        user.tg_id,
        product,
        promo=promo,
        rate_kop=rate_kop,
        markup_pct=markup_pct,
        reserve_minutes=reserve_minutes,
    )


async def _orders_count(session) -> int:
    return int((await session.execute(select(func.count(Order.id)))).scalar_one())


async def _shop(session, tg_id: int = 1001):
    """Покупатель, товар и курс — общая завязка почти каждого теста."""
    user = await make_user(session, tg_id=tg_id)
    product = await make_product(session, price_usd_cents=PRICE_USD_CENTS)
    await make_rate(session, RATE_KOP)
    await session.commit()
    return user, product


# --- Снимок цены ------------------------------------------------------------


async def test_create_order_stores_price_snapshot(session_factory) -> None:
    """Заказ хранит всё, из чего сложилась сумма, а не только сумму.

    Без слагаемых спор с покупателем и сверка с эквайрингом неразрешимы: по
    одному `total_kop` не сказать, курс ли изменился, наценка или цена товара.
    Поэтому проверяются и снимки, и итог, посчитанный из них.
    """
    async with session_factory() as session:
        user, product = await _shop(session)
        title = product.title

        order = await _create(session, user, product)
        await session.commit()
        order_id = order.id

    # Читаем в новой сессии: снимок должен лежать в базе, а не в объекте,
    # который случайно дожил до конца теста.
    async with session_factory() as session:
        saved = await orders_repo.get(session, order_id)

        assert saved.price_usd_cents == PRICE_USD_CENTS
        assert saved.rate_kop == RATE_KOP
        assert saved.markup_pct == MARKUP_PCT

        assert saved.unit_price_kop == TOTAL_KOP
        assert saved.subtotal_kop == TOTAL_KOP
        assert saved.discount_kop == 0
        assert saved.total_kop == TOTAL_KOP

        # Название тоже снимок: товар переименуют, а в истории покупок должно
        # остаться то, что человек купил.
        assert saved.product_title == title
        assert saved.status == OrderStatus.NEW
        assert saved.reserve_expires_at is not None
        # Токен выдаётся только после оплаты — называть номер за неоплаченный
        # заказ незачем.
        assert saved.token is None


async def test_saved_order_ignores_new_exchange_rate(session_factory) -> None:
    """ЗАМОРОЗКА КУРСА — главное свойство файла.

    Покупатель создал счёт на 1980 ₽ и ушёл за деньгами. Пока он ходил, ЦБ
    поднял доллар. Если сумму пересчитывать по текущему курсу, человек вернётся
    к другому числу, а платёж, пришедший на старую сумму, не сойдётся со
    счётом и уедет в разбор вручную.

    Тест сначала убеждается, что новый курс в системе действительно стал
    текущим (иначе он ничего не измеряет), и только потом требует, чтобы заказ
    остался прежним. Замена снимка на пересчёт даёт здесь 2640 ₽ — число
    выписано явно, чтобы падение сразу называло причину.
    """
    async with session_factory() as session:
        user, product = await _shop(session)
        order = await _create(session, user, product)
        await session.commit()
        order_id = order.id
        assert order.total_kop == TOTAL_KOP

    # Курс вырос: 90 ₽ → 120 ₽ за доллар.
    async with session_factory() as session:
        await make_rate(session, 12_000)
        await session.commit()

    async with session_factory() as session:
        current = await rates_repo.latest(session)
        assert current.rate_kop == 12_000, "новый курс не стал текущим — тест ничего не проверяет"

        saved = await orders_repo.get(session, order_id)
        assert saved.rate_kop == RATE_KOP, "курс в заказе поехал за текущим"
        assert saved.unit_price_kop == TOTAL_KOP
        assert saved.total_kop == TOTAL_KOP
        # $20 по 120 ₽ = 2400 ₽, +10 % = 2640 ₽ — столько было бы при пересчёте.
        assert saved.total_kop != 264_000


async def test_promo_applies_after_markup_not_before(session_factory) -> None:
    """Порядок расчёта: курс → наценка → скидка.

    Скидка фиксированная намеренно. Процентная скидка порядок не вскрывает:
    умножения переставимы, и оба варианта дают одно число. С фиксированной
    разница видна — 1950 ₽ против 1875 ₽. Считать скидку от суммы без наценки
    значит подарить покупателю ещё и наценку на неё, а в чеке это выглядит как
    расхождение с объявленной ценой.
    """
    async with session_factory() as session:
        user, product = await _shop(session)
        promo = PromoCode(
            code="FIX300",
            discount_type=DISCOUNT_FIXED,
            discount_value=30_000,  # 300 ₽
        )
        session.add(promo)
        await session.commit()
        promo_id = promo.id

        calc = orders_service.quote(product, RATE_KOP, 25, promo)

        assert calc.base_kop == BASE_KOP  # 1800 ₽ по курсу
        assert calc.unit_price_kop == 225_000  # +25 % = 2250 ₽
        assert calc.subtotal_kop == 225_000
        assert calc.discount_kop == 30_000
        assert calc.total_kop == 195_000  # 2250 − 300 = 1950 ₽
        # Перевёрнутый порядок: (1800 − 300) × 1.25 = 1875 ₽.
        assert calc.total_kop != 187_500

        order = await _create(session, user, product, promo=promo, markup_pct=25)
        await session.commit()
        order_id = order.id

    async with session_factory() as session:
        saved = await orders_repo.get(session, order_id)
        # Заказ считает тем же порядком, что и карточка товара: увидел одно —
        # заплатил столько же.
        assert saved.subtotal_kop == 225_000
        assert saved.discount_kop == 30_000
        assert saved.total_kop == 195_000
        # Код промокода — тоже снимок: промокод удалят, а в чеке он останется.
        assert saved.promo_id == promo_id
        assert saved.promo_code == "FIX300"


async def test_uneven_price_still_adds_up_in_the_order(session_factory) -> None:
    """Слагаемые сходятся и на цене, которая не ложится в целые рубли.

    Все остальные числа в файле круглые, и на них не виден последний шаг
    расчёта: итог округляется вниз до рубля, а срезанные копейки уходят в
    `discount_kop`, чтобы в заказе всегда выполнялось «сумма − скидка = итог».
    Считай эти три поля независимо — и отчёт по продажам не сойдётся на копейку,
    которую будут искать день. Круглая цена такую потерю прячет: там срезать
    нечего.

    $19.99 по 90 ₽ = 1799,10 ₽; +10 % = 1979,01 ₽; в счёт уходит 1979 ₽.
    """
    async with session_factory() as session:
        user = await make_user(session)
        product = await make_product(session, price_usd_cents=1999)
        await make_rate(session, RATE_KOP)
        await session.commit()

        order = await _create(session, user, product)
        await session.commit()
        order_id = order.id

    async with session_factory() as session:
        saved = await orders_repo.get(session, order_id)

        assert saved.price_usd_cents == 1999
        assert saved.unit_price_kop == 197_901  # 1979,01 ₽ — до округления итога
        assert saved.subtotal_kop == 197_901
        assert saved.discount_kop == 1  # копейка, срезанная в пользу покупателя
        assert saved.total_kop == 197_900  # 1979 ₽ ровно

        # Инвариант самого заказа, а не арифметика модуля денег.
        assert saved.subtotal_kop - saved.discount_kop == saved.total_kop
        # Покупателю называют целые рубли. Копейки в счёте провайдер округлит
        # по-своему, и сверка перестанет сходиться.
        assert saved.total_kop % 100 == 0


async def test_order_is_always_a_single_account(session_factory) -> None:
    """Один заказ — один аккаунт.

    Количество исчезло не из интерфейса, а из модели: покупатель не может
    заказать «две работы» одной оплатой, потому что реквизиты присылаются одни.
    Проверяется и записанный `qty`, и то, что итог равен цене за единицу — при
    возврате умножения на количество второе условие сломается первым.
    """
    async with session_factory() as session:
        user, product = await _shop(session)
        order = await _create(session, user, product)
        await session.commit()
        order_id = order.id

    async with session_factory() as session:
        saved = await orders_repo.get(session, order_id)
        assert saved.qty == 1
        assert saved.subtotal_kop == saved.unit_price_kop
        assert saved.total_kop == saved.unit_price_kop

    # Количество нельзя даже попросить: параметра в подписи нет. Это и есть
    # защита от возврата старого поведения — иначе `qty=2` вернётся тихо,
    # с умолчанием 1, и ни один тест выше не покраснеет.
    assert "qty" not in inspect.signature(orders_service.create_order).parameters


# --- Токен ------------------------------------------------------------------


async def test_token_is_not_issued_before_payment(session_factory) -> None:
    """До оплаты токена нет — и выдача его не назначит.

    `token is None` у свежесозданного заказа говорит лишь о том, что создание
    токен не ставит. Настоящая защита стоит в переходе: `start` обязан отказать
    заказу, за который не заплатили. Сними это условие — и в базе заведётся
    номер, который покупатель назовёт администратору, а денег за ним нет;
    администратор найдёт заказ по токену и сделает работу бесплатно.
    """
    async with session_factory() as session:
        user, product = await _shop(session)

        fresh = await _create(session, user, product)
        pending = await _create(session, user, product)
        await orders_service.attach_payment(
            session, pending, "fake", "fake:card", "txn-token-1", "https://pay.example/1"
        )
        await session.commit()

        # Счёт выставлен: заказ ждёт оплату, а не просто «создан». Без этой
        # проверки второй заказ ниже повторял бы первый.
        assert pending.status == OrderStatus.PENDING
        assert pending.pay_url == "https://pay.example/1"

        for order in (fresh, pending):
            result = await delivery_service.start(session, order)
            assert result.ok is False, f"выдача началась в статусе {order.status}"
            assert result.repeated is False
            assert order.token is None
        await session.commit()

        ids = (fresh.id, pending.id)

    async with session_factory() as session:
        first = await orders_repo.get(session, ids[0])
        second = await orders_repo.get(session, ids[1])
        assert (first.token, second.token) == (None, None)
        # И статус отказ не сдвинул: счёт всё ещё можно оплатить.
        assert first.status == OrderStatus.NEW
        assert second.status == OrderStatus.PENDING
        assert orders_service.is_payable(first) is True
        assert orders_service.is_payable(second) is True


async def test_second_start_keeps_the_first_token(session_factory) -> None:
    """Повторная выдача не выпускает второй токен и не откатывает заказ.

    Подтверждение оплаты приходит тремя путями (callback провайдера, кнопка
    «Проверить оплату», фоновый поллер), и `start` вызывается столько же раз.
    Повтор обязан ответить «уже сделано»: отказ на втором вызове превратит
    нажатие кнопки в «оплата не найдена» по оплаченному заказу, а новый токен
    обнулит номер, уже названный покупателю и уехавший в карточку админа.
    """
    async with session_factory() as session:
        user, product = await _shop(session)
        order = await _create(session, user, product)
        order.status = OrderStatus.PAID
        await session.flush()

        first = await delivery_service.start(session, order)
        assert first.ok is True
        assert first.repeated is False
        token = order.token
        assert token, "выдача обязана назвать токен"
        assert order.status == OrderStatus.AWAITING_CREDENTIALS

        again = await delivery_service.start(session, order)
        assert again.ok is True
        assert again.repeated is True, "повтор не отмечен как повтор"
        assert order.token == token, "токен перевыпущен на повторном вызове"
        assert order.status == OrderStatus.AWAITING_CREDENTIALS

        # Кнопку жмут и после того, как реквизиты уже присланы: заказ не должен
        # вернуться из работы обратно в ожидание.
        assert (
            await delivery_service.accept_credentials(session, order, "login", "secret")
        ).ok is True
        late = await delivery_service.start(session, order)
        assert late.ok is True
        assert late.repeated is True
        assert order.token == token
        assert order.status == OrderStatus.IN_WORK
        await session.commit()
        order_id = order.id

    async with session_factory() as session:
        saved = await orders_repo.get(session, order_id)
        assert saved.token == token
        assert saved.status == OrderStatus.IN_WORK
        assert saved.account_login == "login"


# --- Отказы -----------------------------------------------------------------


@pytest.mark.parametrize("rate_kop", [0, -1, -9000])
async def test_broken_rate_stops_the_sale(session_factory, rate_kop: int) -> None:
    """Нет курса — нет продажи.

    Ноль отделён от минуса намеренно: нулевой курс — это незаполненная
    настройка, а не «бесплатно». Продать по нему значит отдать работу даром.
    Заказа при отказе остаться не должно: висящий счёт на 0 ₽ покупатель
    оплатит нулём и придёт за результатом.
    """
    async with session_factory() as session:
        user, product = await _shop(session)

        with pytest.raises(orders_service.RateUnavailable):
            orders_service.quote(product, rate_kop, MARKUP_PCT)

        with pytest.raises(orders_service.RateUnavailable):
            await _create(session, user, product, rate_kop=rate_kop)

        assert await _orders_count(session) == 0, "отказ оставил заказ в базе"

    # Обработчики ловят общий OrderError. Разорви наследование — и вместо
    # «попробуйте позже» покупатель увидит падение бота.
    assert issubclass(orders_service.RateUnavailable, orders_service.OrderError)


async def test_inactive_product_is_not_sold(session_factory) -> None:
    """Снятый с продажи товар не продаётся даже по старой ссылке.

    Карточка живёт в истории чата: кнопка «Купить» нажимается через неделю
    после того, как товар убрали. Проверка на входе `create_order` — последняя,
    и заказа после отказа быть не должно.
    """
    async with session_factory() as session:
        user, product = await _shop(session)
        product.is_active = False
        await session.commit()

        with pytest.raises(orders_service.ProductUnavailable):
            await _create(session, user, product)

        assert await _orders_count(session) == 0
    assert issubclass(orders_service.ProductUnavailable, orders_service.OrderError)


# --- Отмена и истечение -----------------------------------------------------


async def test_cancel_touches_only_unpaid_orders(session_factory) -> None:
    """Отмена работает до оплаты и молчит после.

    Кнопка «Отменить» остаётся в старом сообщении, а заказ к этому моменту уже
    оплачен и ушёл в работу. Отмена такого заказа — это потеря денег
    покупателя: возврат делается через `refunds`, с записью, а не сменой
    статуса из чужой кнопки.
    """
    async with session_factory() as session:
        user, product = await _shop(session)

        fresh = await _create(session, user, product)
        pending = await _create(session, user, product)
        await orders_service.attach_payment(
            session, pending, "fake", "fake:card", "txn-cancel-1", None
        )
        stale = await _create(session, user, product)
        await session.commit()

        assert orders_service.is_payable(fresh) is True
        assert orders_service.is_payable(pending) is True

        await orders_service.cancel(session, fresh)
        await orders_service.cancel(session, pending)
        # Тот же переход, но по таймауту: покупателю надо сказать «счёт истёк»,
        # а не «вы отменили».
        await orders_service.cancel(session, stale, reason="expired")

        # Оплаченные и закрытые — по одному на каждый статус.
        untouchable = {}
        for status in (
            OrderStatus.PAID,
            OrderStatus.AWAITING_CREDENTIALS,
            OrderStatus.IN_WORK,
            OrderStatus.DELIVERED,
            OrderStatus.REFUNDED,
        ):
            order = await make_paid_order(session, user, product, status=status)
            await orders_service.cancel(session, order)
            untouchable[order.id] = status
        await session.commit()

        ids = (fresh.id, pending.id, stale.id)

    async with session_factory() as session:
        assert (await orders_repo.get(session, ids[0])).status == OrderStatus.CANCELED
        assert (await orders_repo.get(session, ids[1])).status == OrderStatus.CANCELED
        assert (await orders_repo.get(session, ids[2])).status == OrderStatus.EXPIRED

        for order_id, status in untouchable.items():
            saved = await orders_repo.get(session, order_id)
            assert saved.status == status, f"отмена тронула заказ в статусе {status}"
            # И платить по нему второй раз тоже нельзя.
            assert orders_service.is_payable(saved) is False


async def test_expire_stale_closes_only_unpaid_orders(session_factory) -> None:
    """Таймаут закрывает счета, а не заказы.

    У всех заказов срок в прошлом — специально, чтобы фильтр по времени никого
    не спас и работал именно фильтр по статусу.
    """
    past = utcnow() - timedelta(minutes=5)

    async with session_factory() as session:
        user, product = await _shop(session)

        fresh = await _create(session, user, product)
        pending = await _create(session, user, product)
        await orders_service.attach_payment(
            session, pending, "fake", "fake:card", "txn-expire-1", None
        )

        survivors = {}
        for status in (OrderStatus.PAID, OrderStatus.DELIVERED, OrderStatus.REFUNDED):
            order = await make_paid_order(session, user, product, status=status)
            survivors[order.id] = status

        for order in [fresh, pending, *[await orders_repo.get(session, i) for i in survivors]]:
            order.reserve_expires_at = past
        # autoflush выключен: без коммита запрос не увидит сдвинутые сроки и
        # тест пройдёт, ничего не проверив.
        await session.commit()

        expired = await orders_service.expire_stale(session)
        await session.commit()

        assert sorted(expired) == sorted([fresh.id, pending.id])
        open_ids = (fresh.id, pending.id)

    async with session_factory() as session:
        for order_id in open_ids:
            assert (await orders_repo.get(session, order_id)).status == OrderStatus.EXPIRED
        for order_id, status in survivors.items():
            saved = await orders_repo.get(session, order_id)
            assert saved.status == status, f"по таймауту сгорел заказ в статусе {status}"


async def test_paid_work_in_progress_never_expires(session_factory) -> None:
    """Деньги получены, работа не сделана — сгорать нечему.

    Самый дорогой случай: покупатель заплатил, ждёт реквизитов или уже прислал
    их, а фоновый закрыватель счетов уводит заказ в EXPIRED. Покупатель видит
    «счёт истёк» после списания денег, администратор не видит заказа вовсе.

    Состояния получены настоящими переходами `delivery`, а не присваиванием
    статуса: заодно проверяется, что выданный токен переживает прогон
    закрывателя — по нему администратор находит заказ.
    """
    past = utcnow() - timedelta(hours=3)

    async with session_factory() as session:
        user, product = await _shop(session)

        waiting = await _create(session, user, product)
        working = await _create(session, user, product)
        waiting.status = OrderStatus.PAID
        working.status = OrderStatus.PAID
        await session.flush()

        assert (await delivery_service.start(session, waiting)).ok is True
        assert (await delivery_service.start(session, working)).ok is True
        assert (
            await delivery_service.accept_credentials(session, working, "login", "secret")
        ).ok is True

        assert waiting.status == OrderStatus.AWAITING_CREDENTIALS
        assert working.status == OrderStatus.IN_WORK

        # Срок оплаты давно прошёл — он к оплаченному заказу больше не относится.
        waiting.reserve_expires_at = past
        working.reserve_expires_at = past
        await session.commit()

        expired = await orders_service.expire_stale(session)
        await session.commit()

        assert expired == [], "оплаченный заказ закрыли по таймауту"
        ids = (waiting.id, working.id)
        tokens = (waiting.token, working.token)

    async with session_factory() as session:
        first = await orders_repo.get(session, ids[0])
        second = await orders_repo.get(session, ids[1])
        assert first.status == OrderStatus.AWAITING_CREDENTIALS
        assert second.status == OrderStatus.IN_WORK
        assert (first.token, second.token) == tokens
        assert second.account_login == "login"
        assert first.total_kop == TOTAL_KOP and second.total_kop == TOTAL_KOP


async def test_reserve_window_is_the_one_the_caller_asked_for(session_factory) -> None:
    """Срок счёта — тот, который назвали, а не зашитый в код.

    Длина окна оплаты — настройка: её удлиняют, когда провайдер начинает
    подтверждать платежи дольше, и укорачивают, когда счета висят зря. Зашитое
    число переживёт любую правку настройки молча — счета будут гаснуть по
    старому сроку, деньги приходить на погашенные заказы, а в настройках будет
    стоять правильное значение, из-за которого причину искать негде.

    Числа проверяются двумя способами: каждый срок попадает в своё окно, и
    разница между сроками равна заказанной. Одно зашитое число даёт разницу
    в ноль и валит второе условие, даже если первое переживёт.
    """
    async with session_factory() as session:
        user, product = await _shop(session)

        before = utcnow()
        short_window = await _create(session, user, product, reserve_minutes=5)
        long_window = await _create(session, user, product, reserve_minutes=90)
        await session.commit()
        ids = (short_window.id, long_window.id)

    async with session_factory() as session:
        short_window = await orders_repo.get(session, ids[0])
        long_window = await orders_repo.get(session, ids[1])
        after = utcnow()

        # Секунда допуска с ОБЕИХ сторон. `DATETIME` без указания точности
        # хранит только целые секунды, и MySQL при записи доли не отбрасывает,
        # а округляет: 14:40:23.6 сохранится как 14:40:24. Значит прочитанное
        # значение может оказаться и меньше записанного, и больше — допуск
        # только снизу однажды уронил этот тест на ровном месте.
        second = timedelta(seconds=1)

        assert short_window.reserve_expires_at >= before + timedelta(minutes=5) - second
        assert short_window.reserve_expires_at <= after + timedelta(minutes=5) + second
        assert long_window.reserve_expires_at >= before + timedelta(minutes=90) - second
        assert long_window.reserve_expires_at <= after + timedelta(minutes=90) + second

        gap = long_window.reserve_expires_at - short_window.reserve_expires_at
        assert timedelta(minutes=85) - second <= gap <= timedelta(minutes=85) + second, (
            f"окна оплаты не различаются: разница {gap}"
        )


async def test_expire_stale_waits_for_the_deadline(session_factory) -> None:
    """До срока счёт не трогают.

    Покупатель ушёл в приложение банка — двадцать минут его. Закрытие «всех
    неоплаченных» вместо «просроченных» отменяет счёт прямо во время оплаты,
    и деньги приходят на отменённый заказ.
    """
    async with session_factory() as session:
        user, product = await _shop(session)
        order = await _create(session, user, product, reserve_minutes=20)
        await session.commit()
        order_id = order.id

        assert await orders_service.expire_stale(session) == []
        await session.commit()

    async with session_factory() as session:
        assert (await orders_repo.get(session, order_id)).status == OrderStatus.NEW


async def test_expire_stale_respects_the_limit(session_factory) -> None:
    """Закрыватель ходит порциями.

    Лимит — не украшение: после долгого простоя просроченных счетов накопятся
    тысячи, и один прогон уложит их в одну транзакцию, заблокировав таблицу
    заказов на время рассылки уведомлений.
    """
    past = utcnow() - timedelta(minutes=5)

    async with session_factory() as session:
        user, product = await _shop(session)
        for _ in range(3):
            order = await _create(session, user, product)
            order.reserve_expires_at = past
        await session.commit()

        first_batch = await orders_service.expire_stale(session, limit=2)
        await session.commit()
        assert len(first_batch) == 2

        second_batch = await orders_service.expire_stale(session, limit=2)
        await session.commit()
        assert len(second_batch) == 1
        assert set(first_batch) & set(second_batch) == set(), "заказ закрыт дважды"


# --- Промокоды --------------------------------------------------------------


async def test_promo_last_use_goes_to_one_order_only(session_factory) -> None:
    """Последнее использование промокода не должно достаться двоим.

    Проверка «прочитать счётчик, потом записать» здесь неверна: между чтением
    и записью помещается чужая покупка.
    """
    async with session_factory() as session:
        promo = PromoCode(
            code="LAST1", discount_type=DISCOUNT_PERCENT, discount_value=10, usage_limit=1
        )
        session.add(promo)
        users = [await make_user(session, tg_id=3000 + i) for i in range(2)]
        product = await make_product(session, price_usd_cents=PRICE_USD_CENTS)
        await session.flush()

        orders = [await _create(session, user, product, promo=promo) for user in users]
        await session.commit()
        promo_id = promo.id
        pairs = [(order.id, order.user_id) for order in orders]

    async def consume(order_id: int, user_id: int) -> bool:
        async with session_factory() as session:
            ok = await promo_repo.consume(session, promo_id, user_id, order_id)
            await session.commit()
            return ok

    results = await asyncio.gather(*(consume(oid, uid) for oid, uid in pairs))
    assert sorted(results) == [False, True], f"получили {results}"

    async with session_factory() as session:
        promo = await promo_repo.get(session, promo_id)
        assert promo.used_count == 1
        assert await promo_repo.uses_total(session, promo_id) == 1


async def test_promo_consume_is_idempotent_per_order(session_factory) -> None:
    """Повторное подтверждение одного заказа не списывает промокод дважды.

    Подтверждение оплаты приходит тремя путями (callback, кнопка, поллер).
    Без уникального индекса на (промокод, заказ) один заказ съедает лимит
    трижды, и следующий покупатель получает отказ по чужой скидке.
    """
    async with session_factory() as session:
        promo = PromoCode(
            code="TWICE", discount_type=DISCOUNT_PERCENT, discount_value=10, usage_limit=5
        )
        session.add(promo)
        user = await make_user(session)
        product = await make_product(session, price_usd_cents=PRICE_USD_CENTS)
        await session.flush()
        order = await _create(session, user, product, promo=promo)
        await session.commit()

        for _ in range(3):
            assert await promo_repo.consume(session, promo.id, user.tg_id, order.id) is True
            await session.commit()

        await session.refresh(promo)
        assert promo.used_count == 1
        assert await promo_repo.uses_total(session, promo.id) == 1


# --- Баланс -----------------------------------------------------------------


async def test_balance_ledger_matches_cached_field(session_factory) -> None:
    """Кеш баланса обязан сходиться с леджером — он источник правды."""
    async with session_factory() as session:
        user = await make_user(session, balance_kop=0)
        await session.commit()

        await balance_repo.move(session, user.tg_id, 50000, BalanceTxnKind.TOPUP)
        await balance_repo.move(session, user.tg_id, -18000, BalanceTxnKind.PURCHASE)
        await balance_repo.move(session, user.tg_id, 18000, BalanceTxnKind.REFUND)
        await session.commit()

        await session.refresh(user)
        assert user.balance_kop == 50000
        assert await balance_repo.ledger_balance(session, user.tg_id) == 50000
        assert await balance_repo.find_mismatches(session) == []


async def test_balance_cannot_go_negative(session_factory) -> None:
    async with session_factory() as session:
        user = await make_user(session, balance_kop=1000)
        await session.commit()

        with pytest.raises(balance_repo.InsufficientFunds):
            await balance_repo.move(session, user.tg_id, -5000, BalanceTxnKind.PURCHASE)
        await session.rollback()

        await session.refresh(user)
        assert user.balance_kop == 1000


async def test_parallel_spending_cannot_overdraw(session_factory) -> None:
    """Два одновременных списания не должны увести баланс в минус."""
    async with session_factory() as session:
        user = await make_user(session, balance_kop=10000)
        await session.commit()
        user_id = user.tg_id

    async def spend() -> bool:
        async with session_factory() as session:
            try:
                await balance_repo.move(session, user_id, -8000, BalanceTxnKind.PURCHASE)
                await session.commit()
                return True
            except balance_repo.InsufficientFunds:
                await session.rollback()
                return False

    results = await asyncio.gather(spend(), spend())
    assert sorted(results) == [False, True], f"получили {results}"

    async with session_factory() as session:
        assert await balance_repo.ledger_balance(session, user_id) == 2000
