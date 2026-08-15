"""Подтверждение оплаты — самая дорогая часть магазина.

Проверяется не «работает ли счастливый путь», а что заказ **не** уходит в
работу там, где не должен: при неподтверждённом статусе, при расхождении
суммы, при чужом payload, при повторных вызовах и при недоступном провайдере.

Со склада продавать нечего, поэтому цена ошибки сместилась: раньше лишняя
выдача означала отданный товар, теперь — выданный токен и запрос реквизитов
аккаунта у человека, который ещё не заплатил. Токен здесь играет роль
«товара»: он выдаётся ровно один раз, ровно за подтверждённые деньги и не
перевыдаётся при повторных проверках.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from bot.db.base import utcnow
from bot.db.models import Order, OrderStatus
from bot.payments.base import Invoice, PaymentMethod, PayStatus, ProviderError, StatusResult
from bot.repo import orders as orders_repo
from bot.services import orders as orders_service
from bot.services import payments as payments_service
from bot.services import tokens
from bot.services.payments import Outcome
from tests.factories import DEFAULT_RATE_KOP, make_product, make_user

pytestmark = pytest.mark.db

MARKUP_PCT = 10
TOTAL_KOP = 198_000
"""$20 × 90,00 ₽ = 1800 ₽, плюс 10 % наценки = 1980 ₽.

Число зафиксировано в тесте намеренно: сверка суммы с провайдером идёт по
снимку в заказе, и если расчёт поедет, ожидание в тесте не должно ехать
вместе с ним.
"""


class FakeProvider:
    """Провайдер, которым управляет тест."""

    name = "fake"

    def __init__(self) -> None:
        self.status = PayStatus.PENDING
        self.amount_kop: int | None = None
        self.payload: str | None = None
        self.raise_error = False
        self.calls = 0

    def methods(self) -> list[PaymentMethod]:
        return [PaymentMethod(provider="fake", code="fake:card", title="Тест")]

    async def create_invoice(self, order_id: int, amount_kop: int, **kwargs) -> Invoice:
        return Invoice(txn_id=f"txn-{order_id}", pay_url="https://example.test/pay")

    async def fetch_status(self, txn_id: str) -> StatusResult:
        self.calls += 1
        if self.raise_error:
            raise ProviderError("провайдер недоступен")
        return StatusResult(
            status=self.status,
            amount_kop=self.amount_kop,
            currency="RUB",
            payload=self.payload,
            raw={"txn": txn_id},
        )

    async def close(self) -> None:
        return None


class FakeRegistry:
    def __init__(self, provider: FakeProvider) -> None:
        self._provider = provider

    def get(self, name: str):
        return self._provider if name == "fake" else None

    def provider_of(self, method_code: str):
        return self._provider if method_code.startswith("fake") else None

    def methods(self):
        return self._provider.methods()

    @property
    def any_enabled(self) -> bool:
        return True


async def _make_pending_order(session, *, tg_id: int = 1001, user=None, product=None):
    """Заказ со счётом у провайдера — состояние, из которого идёт подтверждение."""
    if user is None:
        user = await make_user(session, tg_id=tg_id)
    if product is None:
        product = await make_product(session)
    await session.commit()

    order = await orders_service.create_order(
        session,
        user.tg_id,
        product,
        promo=None,
        rate_kop=DEFAULT_RATE_KOP,
        markup_pct=MARKUP_PCT,
        reserve_minutes=20,
    )
    order.provider = "fake"
    order.payment_method = "fake:card"
    order.provider_txn_id = f"txn-{order.id}"
    order.status = OrderStatus.PENDING
    await session.commit()
    return order, product, user


def _confirmed(provider: FakeProvider, order: Order) -> FakeRegistry:
    """Провайдер, который говорит «оплачено ровно за этот заказ»."""
    provider.status = PayStatus.CONFIRMED
    provider.amount_kop = order.total_kop
    provider.payload = str(order.id)
    return FakeRegistry(provider)


# --- подтверждение через провайдера ----------------------------------------


async def test_pending_payment_gives_no_token(session_factory) -> None:
    """Пока провайдер не сказал «оплачено», токена быть не должно.

    Токен — это обещание сделать работу. Выдать его по неоплаченному счёту
    значит запросить у человека логин и пароль от аккаунта за деньги, которые
    не пришли.
    """
    async with session_factory() as session:
        order, _, _ = await _make_pending_order(session)
        provider = FakeProvider()
        provider.status = PayStatus.PENDING
        registry = FakeRegistry(provider)

        result = await payments_service.confirm_order(session, registry, order.id, "poller")
        await session.commit()

        assert result.outcome == Outcome.PENDING
        assert result.accepted_now is False
        await session.refresh(order)
        assert order.status == OrderStatus.PENDING
        assert order.token is None
        assert order.paid_at is None


async def test_confirmed_payment_asks_for_credentials(session_factory) -> None:
    """Оплата подтверждена → заказ ждёт реквизиты и у него есть токен.

    `accepted_now` — единственный признак, по которому бот решает написать
    покупателю «пришлите логин и пароль». Если ACCEPTED перестанет означать
    «принято именно сейчас», сообщение уйдёт либо ноль раз, либо каждый раз.
    """
    async with session_factory() as session:
        order, _, _ = await _make_pending_order(session)
        provider = FakeProvider()
        registry = _confirmed(provider, order)

        result = await payments_service.confirm_order(session, registry, order.id, "callback")
        await session.commit()

        assert result.outcome == Outcome.ACCEPTED
        assert result.accepted_now is True

        await session.refresh(order)
        assert order.status == OrderStatus.AWAITING_CREDENTIALS
        assert order.paid_at is not None
        assert order.token, "оплаченный заказ обязан получить токен"
        assert tokens.is_valid(order.token), f"токен не той формы: {order.token!r}"
        # Реквизитов ещё нет — их присылает покупатель следующим шагом.
        assert order.account_login is None
        assert order.account_password is None


async def test_repeat_confirmation_keeps_the_same_token(session_factory) -> None:
    """Идемпотентность: три источника подтверждения не должны выдать три токена.

    Callback, кнопка «Проверить оплату» и поллер приходят по одному и тому же
    заказу и почти одновременно. Перевыданный токен — это не косметика: старый
    токен покупатель уже записал, а администратор ищет заказ именно по нему.
    """
    async with session_factory() as session:
        order, _, _ = await _make_pending_order(session)
        provider = FakeProvider()
        registry = _confirmed(provider, order)

        first = await payments_service.confirm_order(session, registry, order.id, "callback")
        await session.commit()
        assert first.outcome == Outcome.ACCEPTED
        token_after_first = order.token
        assert token_after_first
        calls_after_first = provider.calls

        for source in ("callback", "button", "poller"):
            again = await payments_service.confirm_order(session, registry, order.id, source)
            await session.commit()
            assert again.outcome == Outcome.ALREADY, f"источник {source} провёл оплату заново"
            assert again.accepted_now is False, "повтор не должен просить реквизиты второй раз"

        # Оплаченный заказ вообще не должен доходить до провайдера: иначе
        # поздний ответ «отменено» или «другая сумма» отыграет назад платёж,
        # который уже принят.
        assert provider.calls == calls_after_first

        await session.refresh(order)
        assert order.token == token_after_first, "повторное подтверждение перевыдало токен"
        assert order.status == OrderStatus.AWAITING_CREDENTIALS


async def test_confirmation_does_not_rewind_order_in_work(session_factory) -> None:
    """Запоздавший callback не должен вернуть заказ из работы обратно к реквизитам.

    Покупатель уже прислал логин с паролем, администратор уже работает — а
    поллер только сейчас увидел «оплачено». Откат статуса выглядел бы для
    покупателя как просьба прислать реквизиты второй раз.
    """
    async with session_factory() as session:
        order, _, _ = await _make_pending_order(session)
        provider = FakeProvider()
        registry = _confirmed(provider, order)

        await payments_service.confirm_order(session, registry, order.id, "callback")
        await session.commit()

        order.status = OrderStatus.IN_WORK
        order.account_login = "buyer@example.test"
        order.account_password = "secret"
        await session.commit()

        late = await payments_service.confirm_order(session, registry, order.id, "poller")
        await session.commit()

        assert late.outcome == Outcome.ALREADY
        await session.refresh(order)
        assert order.status == OrderStatus.IN_WORK
        assert order.account_login == "buyer@example.test"


async def test_amount_mismatch_blocks_credentials_request(session_factory) -> None:
    """Провайдер говорит «оплачено», но сумма другая — работу не начинаем."""
    async with session_factory() as session:
        order, _, _ = await _make_pending_order(session)
        provider = FakeProvider()
        provider.status = PayStatus.CONFIRMED
        provider.amount_kop = 100  # заплатили 1 ₽ вместо 1980 ₽
        provider.payload = str(order.id)
        registry = FakeRegistry(provider)

        result = await payments_service.confirm_order(session, registry, order.id, "callback")
        await session.commit()

        assert result.outcome == Outcome.MISMATCH
        assert result.accepted_now is False
        # В расхождении должны быть оба числа: без них администратор не поймёт,
        # недоплата это или чужой платёж.
        assert "100" in (result.detail or "") and str(TOTAL_KOP) in (result.detail or "")

        await session.refresh(order)
        assert order.status == OrderStatus.PENDING, "заказ не должен становиться оплаченным"
        assert order.token is None, "токен выдан за неоплаченный заказ"
        assert order.paid_at is None


async def test_foreign_payload_blocks_credentials_request(session_factory) -> None:
    """Оплата с чужим payload не должна закрывать наш заказ.

    Совпадения идентификатора транзакции мало: payload — это номер заказа,
    который провайдер вернул нам обратно, и его расхождение означает, что
    платёж относится к другой покупке.
    """
    async with session_factory() as session:
        order, _, _ = await _make_pending_order(session)
        foreign_id = order.id + 777
        provider = FakeProvider()
        provider.status = PayStatus.CONFIRMED
        provider.amount_kop = order.total_kop
        provider.payload = str(foreign_id)
        registry = FakeRegistry(provider)

        result = await payments_service.confirm_order(session, registry, order.id, "callback")
        await session.commit()

        assert result.outcome == Outcome.MISMATCH
        assert str(foreign_id) in (result.detail or "")
        await session.refresh(order)
        assert order.status == OrderStatus.PENDING
        assert order.token is None


async def test_missing_amount_still_accepted(session_factory) -> None:
    """Провайдер не вернул сумму.

    Заказ найден по нашему же идентификатору транзакции, поэтому принимаем, но
    факт «сумму не проверяли» обязан попасть в журнал — молчание опаснее шума.
    Тест держит именно решение «принимаем»: превратить его в отказ значит
    сломать оплату у провайдера, который сумму не отдаёт.
    """
    async with session_factory() as session:
        order, _, _ = await _make_pending_order(session)
        provider = FakeProvider()
        provider.status = PayStatus.CONFIRMED
        provider.amount_kop = None
        provider.payload = None
        registry = FakeRegistry(provider)

        result = await payments_service.confirm_order(session, registry, order.id, "callback")
        await session.commit()

        assert result.outcome == Outcome.ACCEPTED
        await session.refresh(order)
        assert order.status == OrderStatus.AWAITING_CREDENTIALS
        assert tokens.is_valid(order.token)


async def test_canceled_payment_closes_order_without_token(session_factory) -> None:
    async with session_factory() as session:
        order, _, _ = await _make_pending_order(session)
        provider = FakeProvider()
        provider.status = PayStatus.CANCELED
        registry = FakeRegistry(provider)

        result = await payments_service.confirm_order(session, registry, order.id, "callback")
        await session.commit()

        assert result.outcome == Outcome.FAILED
        await session.refresh(order)
        assert order.status == OrderStatus.CANCELED
        assert order.token is None


async def test_closed_order_is_not_revived(session_factory) -> None:
    """Деньги, пришедшие после отмены, не должны молча воскресить заказ.

    Отменённый заказ — это уже сообщённое покупателю «оплата не прошла».
    Тихая выдача токена по нему разошлась бы и с этим сообщением, и с
    возвратом, который к тому времени мог уже уйти.
    """
    async with session_factory() as session:
        order, _, _ = await _make_pending_order(session)
        order.status = OrderStatus.CANCELED
        await session.commit()

        provider = FakeProvider()
        registry = _confirmed(provider, order)

        result = await payments_service.confirm_order(session, registry, order.id, "callback")
        await session.commit()

        assert result.outcome == Outcome.CLOSED
        assert provider.calls == 0, "закрытый заказ не повод ходить к провайдеру"
        await session.refresh(order)
        assert order.status == OrderStatus.CANCELED
        assert order.token is None


async def test_provider_error_keeps_order_open(session_factory) -> None:
    """Недоступный провайдер — не повод отменить заказ покупателя."""
    async with session_factory() as session:
        order, _, _ = await _make_pending_order(session)
        provider = FakeProvider()
        provider.raise_error = True
        registry = FakeRegistry(provider)

        result = await payments_service.confirm_order(session, registry, order.id, "poller")
        await session.commit()

        assert result.outcome == Outcome.UNAVAILABLE
        await session.refresh(order)
        assert order.status == OrderStatus.PENDING
        assert order.token is None


async def test_paid_order_is_finished_without_asking_provider(session_factory) -> None:
    """Заказ застрял в PAID — следующая проверка обязана довести его сама.

    Так бывает, если процесс упал между «деньги приняты» и выдачей токена.
    Деньги уже подтверждены, спрашивать провайдера второй раз незачем, а
    заказ, навсегда оставшийся в PAID, — это оплата без обещания работы.
    """
    async with session_factory() as session:
        order, _, _ = await _make_pending_order(session)
        order.status = OrderStatus.PAID
        order.paid_at = utcnow()
        await session.commit()

        provider = FakeProvider()
        registry = _confirmed(provider, order)

        result = await payments_service.confirm_order(session, registry, order.id, "button")
        await session.commit()

        assert result.outcome == Outcome.ACCEPTED
        assert provider.calls == 0, "оплаченный заказ не нужно перепроверять у провайдера"
        await session.refresh(order)
        assert order.status == OrderStatus.AWAITING_CREDENTIALS
        assert tokens.is_valid(order.token)


async def test_tokens_of_two_orders_differ(session_factory) -> None:
    """Два проведённых заказа не должны получить один токен.

    По токену администратор находит, над каким аккаунтом работать. Совпадение
    означает, что работу сделают не тому покупателю, и заметят это не сразу.
    """
    async with session_factory() as session:
        first, product, user = await _make_pending_order(session)
        second, _, _ = await _make_pending_order(session, user=user, product=product)

        for order in (first, second):
            provider = FakeProvider()
            registry = _confirmed(provider, order)
            result = await payments_service.confirm_order(session, registry, order.id, "callback")
            await session.commit()
            assert result.outcome == Outcome.ACCEPTED

        await session.refresh(first)
        await session.refresh(second)
        assert first.token and second.token
        assert first.token != second.token


# --- пополнение баланса -----------------------------------------------------



# --- оплата с баланса -------------------------------------------------------





# --- то, что пережило мутационную проверку ----------------------------------
#
# Три теста ниже написаны не «на всякий случай»: без каждого из них
# соответствующая поломка проходила мимо всей остальной сюиты.


async def test_existing_token_is_never_reissued(session_factory) -> None:
    """У заказа, у которого токен уже есть, подтверждение его не меняет.

    Случай не выдуманный: миграция на новую модель раздаёт токены заказам,
    застрявшим в `paid`, — и такой заказ приходит на подтверждение УЖЕ
    с токеном. Если выдачу токена сделать безусловной, покупателю назовут
    один номер, а в базе окажется другой, и найти свой заказ он не сможет.

    Обычный путь этого не ловит: заказ, оплаченный ботом, доходит до выдачи
    токена ровно один раз, и перевыдавать там нечего.
    """
    async with session_factory() as session:
        order, _, _ = await _make_pending_order(session)
        order.status = OrderStatus.PAID
        order.paid_at = utcnow()
        order.token = "AAAA-BBBB"
        await session.commit()
        order_id, before = order.id, order.token

    async with session_factory() as session:
        provider = FakeProvider()
        order = await orders_repo.get(session, order_id)
        registry = _confirmed(provider, order)

        result = await payments_service.confirm_order(session, registry, order_id, "button")
        await session.commit()

        assert result.outcome == Outcome.ACCEPTED
        assert result.order.token == before, "токен подменили задним числом"
        assert result.order.status == OrderStatus.AWAITING_CREDENTIALS


async def test_two_confirmations_at_once_accept_only_one(session_factory) -> None:
    """Два одновременных подтверждения — один приём оплаты.

    Кнопку «Проверить оплату» нажимают дважды подряд, и ровно в этот момент
    может подоспеть поллер. Без блокировки строки обе проверки читают заказ
    в состоянии «оплачен», обе считают себя первыми, и покупателя дважды
    просят прислать реквизиты, а в платёжный журнал ложится вторая запись.

    Последовательные вызовы эту поломку не видят: второй читает уже
    изменённый статус. Нужна именно одновременность.
    """
    async with session_factory() as session:
        order, _, _ = await _make_pending_order(session)
        order.status = OrderStatus.PAID
        order.paid_at = utcnow()
        await session.commit()
        order_id = order.id
        total_kop = order.total_kop

    async def confirm() -> str:
        async with session_factory() as session:
            provider = FakeProvider()
            provider.status = PayStatus.CONFIRMED
            provider.amount_kop = total_kop
            provider.payload = str(order_id)
            result = await payments_service.confirm_order(
                session, FakeRegistry(provider), order_id, "button"
            )
            await session.commit()
            return result.outcome

    outcomes = await asyncio.gather(confirm(), confirm())

    assert sorted(outcomes) == [Outcome.ACCEPTED, Outcome.ALREADY], (
        f"оплату приняли дважды: {outcomes}"
    )

    async with session_factory() as session:
        order = await orders_repo.get(session, order_id)
        assert order.status == OrderStatus.AWAITING_CREDENTIALS
        assert order.token



