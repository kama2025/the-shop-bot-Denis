"""Путь заказа после оплаты: реквизиты от покупателя и подтверждение работы.

Магазин продаёт не содержимое склада, а работу над чужим аккаунтом. Значит
между «деньги пришли» и «заказ выполнен» стоят два человека: покупатель,
который присылает логин с паролем, и администратор, который жмёт кнопку. Оба
шага делаются в Telegram, где сообщение приходит дважды, а кнопка нажимается
трижды. Поэтому проверяется здесь не «функция вызвалась», а то, что состояние
заказа меняется ровно один раз и только из подходящего состояния.

Тесты с базой идут на настоящую MySQL: `credentials_at`, `delivered_at` и
`delivered_by` — это колонки, и проверять их на объекте в памяти значит верить,
что запись доехала. Ключевые утверждения перечитывают заказ **в новой сессии**:
только так видно, что в базе лежит, а не что осталось в кеше ORM.

Разбор реквизитов (`parse_credentials`) базы не требует и живёт в конце файла
без пометки `db` — иначе на машине без MySQL пропадала бы и чистая функция.

Асинхронность включена глобально (`asyncio_mode = auto` в pytest.ini), поэтому
`@pytest.mark.asyncio` нигде не ставится — как и в соседних файлах с БД.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from bot.db.base import utcnow
from bot.db.models import Order, OrderStatus
from bot.repo import orders as orders_repo
from bot.services import delivery as delivery_service
from bot.services import fulfillment
from tests.factories import make_paid_order, make_product, make_user

LOGIN = "buyer@example.com"
PASSWORD = "s3cret pass"

FIRST_ADMIN = 777
SECOND_ADMIN = 888


async def _order(session, status: str = OrderStatus.PAID) -> Order:
    """Оплаченный заказ в нужном статусе, уже записанный в базу."""
    user = await make_user(session)
    product = await make_product(session)
    order = await make_paid_order(session, user, product, status=status)
    await session.commit()
    return order


async def _awaiting(session) -> Order:
    """Заказ, доведённый до ожидания реквизитов штатным путём.

    Через `start`, а не установкой статуса руками: так заказ получает токен —
    ровно то состояние, в котором покупателя просят прислать логин и пароль.
    """
    order = await _order(session, status=OrderStatus.PAID)
    result = await delivery_service.start(session, order)
    await session.commit()
    assert result.ok is True
    assert order.status == OrderStatus.AWAITING_CREDENTIALS
    assert order.token, "переход в ожидание реквизитов обязан выдать токен"
    return order


async def _in_work(session) -> Order:
    """Заказ с принятыми реквизитами — то, что видит администратор."""
    order = await _awaiting(session)
    result = await delivery_service.accept_credentials(session, order, LOGIN, PASSWORD)
    await session.commit()
    assert result.ok is True
    assert order.status == OrderStatus.IN_WORK
    return order


async def _reload(session_factory, order_id: int) -> Order:
    """Перечитать заказ в отдельной сессии — проверка, что запись доехала."""
    async with session_factory() as fresh:
        order = await orders_repo.get(fresh, order_id)
        assert order is not None
        return order


async def _mark_time_back(session, session_factory, order: Order, field: str):
    """Отодвинуть отметку времени на час назад и вернуть её значение из базы.

    Нужно, чтобы проверка «повтор не переписал время» вообще что-то ловила.
    MySQL хранит DATETIME с точностью до секунды, и два вызова подряд попадают
    в одну и ту же секунду — перезапись выглядела бы как совпадение значений.
    Час назад — заведомо отличимая метка. Значение возвращается **прочитанным
    из базы**: то, что лежит в памяти, содержит микросекунды, которых в колонке
    нет, и сравнение с ним падало бы на округлении, а не на поведении кода.
    """
    setattr(order, field, utcnow() - timedelta(hours=1))
    await session.commit()
    return getattr(await _reload(session_factory, order.id), field)


# --- выход из оплаты: токен и переход к ожиданию реквизитов -----------------


@pytest.mark.db
@pytest.mark.parametrize(
    "status",
    [
        OrderStatus.NEW,
        OrderStatus.PENDING,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.REFUNDED,
    ],
)
async def test_start_refuses_orders_that_are_not_paid(session_factory, status) -> None:
    """Токен выдаётся только за деньги.

    `start` — единственная дверь в исполнение: здесь заказ получает токен и
    попадает к администраторам. Пропустить сюда неоплаченный заказ (NEW,
    PENDING) значит поставить в план работу, за которую не заплатили;
    пропустить закрытый (CANCELED, EXPIRED, REFUNDED) — работу, за которую
    деньги уже вернули.

    Проверяется и то, что **токена не появилось**: заказ без токена в очередь
    не попадёт даже при испорченном статусе, а с токеном — попадёт.
    """
    async with session_factory() as session:
        order = await _order(session, status=status)

        result = await delivery_service.start(session, order)
        await session.commit()

        assert result.ok is False
        stored = await _reload(session_factory, order.id)
        assert stored.status == status, "заказ ушёл в исполнение из неподходящего статуса"
        assert stored.token is None, "токен выдан заказу, за который магазин не получил денег"


@pytest.mark.db
async def test_start_is_idempotent_and_keeps_the_token(session_factory) -> None:
    """Повторный `start` не начинает всё заново.

    Подтверждение оплаты приходит тремя путями — callback провайдера, кнопка
    «Проверить оплату» и фоновый поллер, — и все три зовут `start` по одному
    заказу. Второй вызов обязан назваться повтором: иначе покупатель получит
    второе сообщение с новым номером заказа, а по номеру, названному первым,
    заказ уже не найдётся — ни у него, ни у поддержки.
    """
    async with session_factory() as session:
        order = await _awaiting(session)
        issued = order.token

        for _ in range(3):
            again = await delivery_service.start(session, order)
            await session.commit()
            assert again.ok is True, "повторное подтверждение оплаты сорвалось в отказ"
            assert again.repeated is True, "повторный старт выдал себя за первый"

        stored = await _reload(session_factory, order.id)
        assert stored.status == OrderStatus.AWAITING_CREDENTIALS
        assert stored.token == issued, "на повторном старте заказу перевыдали токен"


@pytest.mark.db
async def test_start_never_reissues_a_token_it_already_gave(session_factory) -> None:
    """Токен, названный покупателю, за ним и остаётся.

    Отдельно от идемпотентности по статусу: та отсекает повтор раньше, а здесь
    проверяется сама выдача. Статус возвращается в PAID руками — своим ходом
    заказ так не откатывается, но это ровно то состояние, которое оставляет
    ручная правка статуса или откат по инциденту. Цена ошибки одинаковая:
    новый токен затирает тот, который покупатель уже держит в переписке, и
    заказ перестаёт находиться по названному ему номеру.
    """
    async with session_factory() as session:
        order = await _awaiting(session)
        issued = order.token
        order.status = OrderStatus.PAID
        await session.commit()

        result = await delivery_service.start(session, order)
        await session.commit()

        assert result.ok is True
        stored = await _reload(session_factory, order.id)
        assert stored.token == issued, "заказу выдали второй токен поверх первого"
        assert stored.status == OrderStatus.AWAITING_CREDENTIALS


# --- реквизиты покупателя ---------------------------------------------------


@pytest.mark.db
async def test_credentials_move_order_into_work(session_factory) -> None:
    """Логин с паролем переводят заказ в работу и сохраняются в базе.

    Здесь же проверяется обрезка пробелов по краям: покупатель копирует пароль
    из заметок вместе с хвостом, и администратор потом не может войти, глядя
    на внешне правильный пароль. Пробелы **внутри** пароля при этом обязаны
    сохраниться — они его часть.
    """
    async with session_factory() as session:
        order = await _awaiting(session)
        before = utcnow()

        result = await delivery_service.accept_credentials(
            session, order, f"  {LOGIN}  ", f"\t{PASSWORD} "
        )
        await session.commit()

        assert result.ok is True
        assert result.repeated is False
        assert order.credentials_at is not None and order.credentials_at >= before

        stored = await _reload(session_factory, order.id)
        assert stored.status == OrderStatus.IN_WORK
        assert stored.account_login == LOGIN
        assert stored.account_password == PASSWORD, "пробелы внутри пароля потеряны"
        assert stored.credentials_at is not None


@pytest.mark.db
@pytest.mark.parametrize(
    "login, password",
    [
        ("", PASSWORD),
        ("   ", PASSWORD),
        (LOGIN, ""),
        (LOGIN, "   \t "),
        ("  ", "  "),
    ],
    ids=["пустой-логин", "логин-из-пробелов", "пустой-пароль", "пароль-из-пробелов", "оба-пустые"],
)
async def test_half_credentials_are_refused(session_factory, login, password) -> None:
    """Половина реквизитов не принимается и в базу не попадает.

    Самый дорогой из отказов. Заказ с одним логином и пустым паролем уезжает
    к администратору выглядящим готовым к работе, тот берёт его в работу — и
    упирается в невозможность войти, когда покупатель уже ждёт. Строка из
    одних пробелов — тот же случай: Telegram охотно отправляет такое сообщение.
    """
    async with session_factory() as session:
        order = await _awaiting(session)

        result = await delivery_service.accept_credentials(session, order, login, password)
        await session.commit()

        assert result.ok is False
        assert order.status == OrderStatus.AWAITING_CREDENTIALS

        stored = await _reload(session_factory, order.id)
        assert stored.status == OrderStatus.AWAITING_CREDENTIALS
        assert stored.account_login is None, "неполные реквизиты записались в базу"
        assert stored.account_password is None, "неполные реквизиты записались в базу"
        assert stored.credentials_at is None


@pytest.mark.db
@pytest.mark.parametrize("status", [OrderStatus.PAID, OrderStatus.NEW])
async def test_credentials_refused_before_they_are_asked(session_factory, status) -> None:
    """Реквизиты принимаются только тогда, когда их спросили.

    NEW — заказ, за который ещё не заплатили: принять по нему логин и пароль
    значит пустить в работу неоплаченный заказ. PAID — оплата прошла, но
    `start` ещё не отработал, токена нет; запись реквизитов в обход `start`
    оставила бы заказ в работе без идентификатора, по которому его ищут.
    """
    async with session_factory() as session:
        order = await _order(session, status=status)

        result = await delivery_service.accept_credentials(session, order, LOGIN, PASSWORD)
        await session.commit()

        assert result.ok is False
        stored = await _reload(session_factory, order.id)
        assert stored.status == status, "статус изменился из неподходящего состояния"
        assert stored.account_login is None
        assert stored.account_password is None
        assert stored.credentials_at is None


@pytest.mark.db
@pytest.mark.parametrize(
    "status", [OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REFUNDED]
)
async def test_credentials_refused_after_the_order_was_closed(session_factory, status) -> None:
    """По закрытому заказу реквизиты не принимаются.

    Дырка не теоретическая: заказ отменяют или возвращают по нему деньги, пока
    покупатель ещё сидит в том же диалоге с просьбой прислать логин. Его
    сообщение приходит после закрытия — и если его принять, заказ, за который
    магазину уже не платят, снова уедет администратору как работа. По
    возвращённому это прямой убыток: деньги отданы, работа сделана.

    Отдельно проверяется, что логин с паролем **не осели в базе**: хранить
    доступ к чужому аккаунту по заказу, которого больше нет, нельзя.
    """
    async with session_factory() as session:
        order = await _awaiting(session)
        order.status = status
        await session.commit()

        result = await delivery_service.accept_credentials(session, order, LOGIN, PASSWORD)
        await session.commit()

        assert result.ok is False
        stored = await _reload(session_factory, order.id)
        assert stored.status == status, "закрытый заказ вернулся в работу"
        assert stored.account_login is None, "реквизиты сохранены по закрытому заказу"
        assert stored.account_password is None
        assert stored.credentials_at is None


@pytest.mark.db
async def test_credentials_are_not_overwritten_after_delivery(session_factory) -> None:
    """По выполненному заказу реквизиты не переписываются.

    Заказ закрыт, но покупатель остаётся в том же диалоге и может отправить
    ещё одно сообщение — например, сменив пароль у себя. Реквизиты, по которым
    работа уже сделана, менять нельзя: журнал перестанет соответствовать тому,
    что происходило.

    Отдельно: `ok` здесь **True** с `repeated=True`, а не отказ — DELIVERED
    обрабатывается тем же ветвлением, что и IN_WORK, то есть как «шаг уже
    сделан». См. отчёт: это расходится с формулировкой «неподходящий статус».
    """
    async with session_factory() as session:
        order = await _in_work(session)
        done = await delivery_service.confirm_done(session, order, FIRST_ADMIN)
        await session.commit()
        assert done.ok is True

        result = await delivery_service.accept_credentials(
            session, order, "новый-логин", "новый-пароль"
        )
        await session.commit()

        assert result.repeated is True

        stored = await _reload(session_factory, order.id)
        assert stored.status == OrderStatus.DELIVERED, "выполненный заказ вернулся в работу"
        assert stored.account_login == LOGIN
        assert stored.account_password == PASSWORD


@pytest.mark.db
async def test_second_credentials_message_does_not_replace_the_first(session_factory) -> None:
    """Повтор по заказу в работе ничего не меняет.

    Покупатель дублирует сообщение или присылает второй набор, пока
    администратор уже работает. Перезапись увела бы исполнителя на другой
    аккаунт посреди работы, а повторный переход в IN_WORK отправил бы
    администратору вторую карточку того же заказа.
    """
    async with session_factory() as session:
        order = await _in_work(session)
        first_at = await _mark_time_back(session, session_factory, order, "credentials_at")

        result = await delivery_service.accept_credentials(
            session, order, "другой-логин", "другой-пароль"
        )
        await session.commit()

        assert result.ok is True
        assert result.repeated is True

        stored = await _reload(session_factory, order.id)
        assert stored.status == OrderStatus.IN_WORK
        assert stored.account_login == LOGIN, "реквизиты первого сообщения перезаписаны"
        assert stored.account_password == PASSWORD
        assert stored.credentials_at == first_at, "время получения реквизитов сдвинулось"


# --- подтверждение выполнения ----------------------------------------------


@pytest.mark.db
async def test_confirm_closes_order_and_names_the_admin(session_factory) -> None:
    """Кнопка администратора закрывает заказ и записывает, кто это сделал.

    `delivered_by` — единственный след исполнителя. Без него по спорному
    заказу нельзя ответить на вопрос «кто подтвердил», а он возникает ровно
    тогда, когда работа сделана плохо.
    """
    async with session_factory() as session:
        order = await _in_work(session)
        before = utcnow()

        result = await delivery_service.confirm_done(session, order, FIRST_ADMIN)
        await session.commit()

        assert result.ok is True
        assert result.repeated is False
        assert order.delivered_at is not None and order.delivered_at >= before

        stored = await _reload(session_factory, order.id)
        assert stored.status == OrderStatus.DELIVERED
        assert stored.delivered_by == FIRST_ADMIN
        assert stored.delivered_at is not None


@pytest.mark.db
async def test_confirm_is_idempotent_and_keeps_the_first_admin(session_factory) -> None:
    """Второе нажатие ничего не меняет — включая имя исполнителя.

    Кнопка живёт в чате администраторов, её видят все и жмут повторно. Без
    идемпотентности покупатель получает второе «заказ выполнен», а в
    `delivered_by` оказывается тот, кто нажал последним, — то есть журнал
    начинает называть исполнителем случайного человека.
    """
    async with session_factory() as session:
        order = await _in_work(session)

        first = await delivery_service.confirm_done(session, order, FIRST_ADMIN)
        await session.commit()
        assert first.repeated is False
        first_at = await _mark_time_back(session, session_factory, order, "delivered_at")

        for _ in range(3):
            again = await delivery_service.confirm_done(session, order, SECOND_ADMIN)
            await session.commit()
            assert again.ok is True
            assert again.repeated is True, "повторное подтверждение выдало себя за первое"

        stored = await _reload(session_factory, order.id)
        assert stored.status == OrderStatus.DELIVERED
        assert stored.delivered_by == FIRST_ADMIN, "исполнителя подменил второй администратор"
        assert stored.delivered_at == first_at, "время выполнения переписано"


@pytest.mark.db
@pytest.mark.parametrize(
    "status", [OrderStatus.AWAITING_CREDENTIALS, OrderStatus.PAID, OrderStatus.NEW]
)
async def test_confirm_refused_before_work_started(session_factory, status) -> None:
    """Подтвердить нечего, пока заказ не в работе.

    Главный случай — AWAITING_CREDENTIALS: реквизитов ещё нет, работать было
    не с чем, и «выполнено» здесь означало бы закрытый заказ, по которому
    ничего не сделали. Деньги при этом остаются у магазина, а покупатель
    получает уведомление о выполнении.
    """
    async with session_factory() as session:
        order = await _order(session, status=status)

        result = await delivery_service.confirm_done(session, order, FIRST_ADMIN)
        await session.commit()

        assert result.ok is False

        stored = await _reload(session_factory, order.id)
        assert stored.status == status
        assert stored.delivered_at is None
        assert stored.delivered_by is None


@pytest.mark.db
@pytest.mark.parametrize(
    "status", [OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REFUNDED]
)
async def test_confirm_refused_on_a_closed_order(session_factory, status) -> None:
    """Закрытый заказ нельзя объявить выполненным.

    Обратная сторона предыдущего: карточка заказа с кнопкой «Выполнено»
    остаётся в чате администраторов и после того, как заказ отменили или
    вернули по нему деньги. Нажатие по такой карточке не должно ни закрывать
    заказ как выполненный, ни назначать исполнителя — иначе возвращённый заказ
    попадёт в отчёт как сделанная работа, а деньги по нему уже у покупателя.

    Статусы `AWAITING_CREDENTIALS`, `PAID` и `NEW` закрыты соседним тестом:
    там работа ещё не начиналась, здесь — уже не может.
    """
    async with session_factory() as session:
        order = await _in_work(session)
        order.status = status
        await session.commit()

        result = await delivery_service.confirm_done(session, order, FIRST_ADMIN)
        await session.commit()

        assert result.ok is False
        stored = await _reload(session_factory, order.id)
        assert stored.status == status, "закрытый заказ помечен выполненным"
        assert stored.delivered_at is None
        assert stored.delivered_by is None, "у закрытого заказа появился исполнитель"


@pytest.mark.parametrize("status", OrderStatus.ALL)
def test_needs_credentials_only_while_waiting(status) -> None:
    """Логин и пароль спрашивают ровно в одном состоянии.

    По этому признаку бот решает, считать ли следующее сообщение покупателя
    реквизитами. Расширь его на IN_WORK — и сообщение «спасибо» превратится
    в новый логин; сузь — и присланные реквизиты уйдут в никуда.
    """
    order = Order(status=status)
    assert delivery_service.needs_credentials(order) is (
        status == OrderStatus.AWAITING_CREDENTIALS
    )


# --- разбор сообщения с реквизитами (без базы) ------------------------------


def test_two_lines_become_login_and_password() -> None:
    """Штатный случай: первая строка — логин, вторая — пароль."""
    creds = fulfillment.parse_credentials("my_login\nmy_password")
    assert creds is not None
    assert creds.login == "my_login"
    assert creds.password == "my_password"
    assert creds.complete is True


def test_single_line_leaves_password_unknown() -> None:
    """Одна строка — это ещё не реквизиты, пароль спросят отдельно.

    Отличие `None` от пустой строки здесь несущее: `complete` ложно, и бот
    обязан задать второй вопрос, а не отправить администратору полузаказ.
    """
    creds = fulfillment.parse_credentials("my_login")
    assert creds is not None
    assert creds.login == "my_login"
    assert creds.password is None
    assert creds.complete is False


@pytest.mark.parametrize("text", ["", "   ", "\n\n", " \t \n  \n"])
def test_blank_message_is_not_credentials(text) -> None:
    """Пустое сообщение — не реквизиты, а `None`.

    Вернуть `Credentials(login="")` значило бы протащить пустой логин дальше,
    где он уже выглядит как ответ покупателя.
    """
    assert fulfillment.parse_credentials(text) is None


def test_blank_lines_between_login_and_password_are_skipped() -> None:
    """Пустая строка между логином и паролем — обычный способ набора.

    Считать её паролем нельзя: сообщение выглядит заполненным, а пароля нет.
    """
    creds = fulfillment.parse_credentials("\n\n  my_login  \n\n\n  my_password  \n\n")
    assert creds is not None
    assert creds.login == "my_login"
    assert creds.password == "my_password"


def test_password_is_the_second_line_and_not_the_rest_of_the_message() -> None:
    """Пароль — вторая строка, и только она.

    Покупатель дописывает третьей строкой «спасибо!» или подпись — это самый
    обычный вид сообщения. Взять последнюю строку значит отдать администратору
    «спасибо!» вместо пароля; склеить все строки после логина — отдать пароль
    с приклеенным хвостом. Оба случая выглядят как правильно заполненная
    карточка и ломаются только на попытке войти.
    """
    creds = fulfillment.parse_credentials("my_login\nmy_password\nспасибо!\nи ещё строка")
    assert creds is not None
    assert creds.login == "my_login"
    assert creds.password == "my_password"
    assert creds.complete is True


def test_password_with_colons_survives_whole() -> None:
    """Двоеточие в пароле не считается разделителем.

    Разбирать `login:password` соблазнительно, но пароль имеет полное право
    содержать двоеточие, и такой разбор молча отрежет ему хвост. Ошибка
    всплывёт только тогда, когда администратор не сможет войти, — и будет
    выглядеть как проблема на стороне покупателя.
    """
    creds = fulfillment.parse_credentials("my_login\na:b:c:d")
    assert creds is not None
    assert creds.login == "my_login"
    assert creds.password == "a:b:c:d"


def test_single_line_with_colon_is_not_split() -> None:
    """Та же защита с другой стороны: одна строка остаётся логином целиком.

    Строка `login:password` не разбирается на пару — иначе пароль с
    двоеточием потеряет часть себя ещё до того, как его кто-то увидит.
    """
    creds = fulfillment.parse_credentials("login:password")
    assert creds is not None
    assert creds.login == "login:password"
    assert creds.password is None


def test_edges_are_trimmed_but_inner_spaces_stay() -> None:
    """Пробелы по краям срезаются, внутри пароля — сохраняются.

    Копирование из заметок приносит хвостовые пробелы, и с ними вход не
    получится. А вот пробел внутри пароля — его законный символ, и потеря
    такого пробела ломает вход ровно так же.
    """
    creds = fulfillment.parse_credentials("   user name   \n   pa ss  word   ")
    assert creds is not None
    assert creds.login == "user name"
    assert creds.password == "pa ss  word"
    assert creds.complete is True
