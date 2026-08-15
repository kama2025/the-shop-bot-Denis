"""Клавиатуры: стили кнопок, длина callback_data и вид ссылок.

Все три свойства Telegram проверяет на своей стороне и при нарушении отвергает
**всю клавиатуру целиком**. Сообщение не уходит вообще, и у покупателя экран
просто исчезает. Поймать это в тесте дешевле, чем разбирать «Bad Request»
из журнала.

Набор допустимых стилей получен пробой к живому Bot API: `default`, `primary`,
`success`, `danger`. Значения вроде `secondary`, `gray`, `accent` отвергаются.

Файл держит ОБХОД ВСЕХ экранов бота. Обход не ручной список, а сверка со
списком публичных сборщиков в `bot.keyboards.*`: экран, добавленный в код и
забытый здесь, роняет тест. Именно так этот файл однажды и отстал — новые
`fulfillment_card` и `send_credentials` месяц жили без единой проверки.
"""

from __future__ import annotations

import inspect
from datetime import datetime
from types import ModuleType, SimpleNamespace

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from magic_filter import MagicFilter

from bot.db.models import (
    Admin,
    Broadcast,
    Category,
    Channel,
    Order,
    OrderStatus,
    Product,
    PromoCode,
    SettingEntry,
    TextEntry,
)
from bot.keyboards import admin as admin_kb
from bot.keyboards import user as user_kb
from bot.keyboards.theme import ALLOWED_STYLES, SUCCESS, ButtonStyleError, btn, nav_row
from bot.payments.base import PaymentMethod
from bot.utils.money import DISCOUNT_PERCENT, format_kop

CALLBACK_LIMIT = 64
NOW = datetime(2026, 8, 10, 12, 0)

# Заказ с большим номером: длина callback_data проверяется на правдоподобном
# худшем случае, а не на «#1», где в лимит влезет что угодно.
ORDER_ID = 987654321
BUYER_ID = 878351372


# --- проверка самого btn() --------------------------------------------------


@pytest.mark.parametrize("style", sorted(ALLOWED_STYLES))
def test_btn_accepts_documented_styles(style: str) -> None:
    assert btn("текст", callback_data="noop", style=style).style == style


def test_btn_accepts_no_style() -> None:
    assert btn("текст", callback_data="noop").style is None


@pytest.mark.parametrize("style", ["secondary", "gray", "accent", "warning", "PRIMARY", ""])
def test_btn_rejects_unknown_styles(style: str) -> None:
    """Именно `secondary` однажды уронил экран профиля в проде."""
    with pytest.raises(ButtonStyleError):
        btn("текст", callback_data="noop", style=style)


# --- заготовки --------------------------------------------------------------


CATEGORY_ID = 7
PRODUCT_ID = 42


def _category(id_: int = CATEGORY_ID) -> Category:
    return Category(id=id_, title="Категория", sort_order=10, is_active=True)


def _product(id_: int = PRODUCT_ID) -> Product:
    # id товара и id его категории РАЗНЫЕ. Пока они совпадали, подстановка
    # `product.category_id` вместо `product.id` в callback_data кнопки «Купить»
    # проходила все проверки: сравнение шло числа с самим собой.
    return Product(
        id=id_,
        category_id=CATEGORY_ID,
        title="Товар",
        price_usd_cents=2000,
        sort_order=10,
        is_active=True,
    )


def _order(
    id_: int = ORDER_ID,
    status: str = OrderStatus.PENDING,
    pay_url: str | None = "https://pay.example/x",
) -> Order:
    return Order(
        id=id_,
        user_id=BUYER_ID,
        product_id=PRODUCT_ID,
        product_title="Товар",
        price_usd_cents=2000,
        rate_kop=9000,
        markup_pct=10,
        qty=1,
        unit_price_kop=198000,
        subtotal_kop=198000,
        discount_kop=19800,
        total_kop=178200,
        status=status,
        pay_url=pay_url,
        created_at=NOW,
    )


def _promo() -> PromoCode:
    return PromoCode(
        id=1, code="DEMO10", discount_type=DISCOUNT_PERCENT, discount_value=10,
        used_count=3, usage_limit=10, is_active=True,
    )


def _channel() -> Channel:
    return Channel(
        id=1, chat_ref="@demo", title="Канал", invite_url="https://t.me/demo",
        sort_order=10, is_active=True,
    )


def _methods() -> list[PaymentMethod]:
    return [
        PaymentMethod(provider="platega", code="platega:2", title="СБП", emoji="🏦"),
        PaymentMethod(provider="platega", code="platega:11", title="Карта", emoji="💳"),
        PaymentMethod(provider="cryptobot", code="cryptobot:crypto", title="CryptoBot", emoji="🪙"),
    ]


def _all_keyboards() -> list[tuple[str, InlineKeyboardMarkup]]:
    """Каждая клавиатура бота, собранная на правдоподобных данных.

    Имя слева — «модуль.функция» и, в скобках, ветка. По этим именам сверяется
    полнота обхода, поэтому часть до скобки обязана совпадать с именем функции.
    """
    category, product, order = _category(), _product(), _order()

    return [
        ("user.subscription", user_kb.subscription([_channel()])),
        ("user.main_menu(admin)", user_kb.main_menu(is_admin=True)),
        ("user.main_menu(guest)", user_kb.main_menu(is_admin=False)),
        ("user.categories", user_kb.categories([category], 0, 3)),
        ("user.products", user_kb.products([product], 9000, 1, 1, 3)),
        # Курс не получен: рублёвой цены в кнопке нет, но экран обязан собраться.
        ("user.products(no rate)", user_kb.products([product], 0, 1, 0, 1)),
        ("user.product_card", user_kb.product_card(product, 178200, f"u:cat:{CATEGORY_ID}:0")),
        ("user.product_card(no rate)", user_kb.product_card(product, None, f"u:cat:{CATEGORY_ID}:0")),
        ("user.send_credentials", user_kb.send_credentials(ORDER_ID)),
        ("user.payment_methods", user_kb.payment_methods(order, _methods())),
        # Баланса меньше суммы заказа — кнопка меняет стиль, а не исчезает.
        ("user.payment_link", user_kb.payment_link(order)),
        # Ссылку провайдер ещё не выдал — экран обязан собраться без неё.
        ("user.payment_link(no link)", user_kb.payment_link(_order(pay_url=None))),
        ("user.profile", user_kb.profile(has_promo=True)),
        ("user.profile(bare)", user_kb.profile(has_promo=False)),
        ("user.purchases", user_kb.purchases([order], 1, 5)),
        ("user.simple_back", user_kb.simple_back()),
        ("admin.menu", admin_kb.menu()),
        ("admin.categories", admin_kb.categories([category], 0, 2)),
        ("admin.category_card", admin_kb.category_card(category, 4)),
        ("admin.products", admin_kb.products([product], 1, 0, 2)),
        ("admin.product_card", admin_kb.product_card(product)),
        ("admin.category_picker", admin_kb.category_picker([category], 1)),
        ("admin.orders", admin_kb.orders([order], 0, 2, None)),
        ("admin.orders(filtered)", admin_kb.orders([order], 0, 2, OrderStatus.DELIVERED)),
        (
            "admin.order_card",
            admin_kb.order_card(order, True, True, buyer_username="buyer"),
        ),
        ("admin.order_card(closed)", admin_kb.order_card(order, False, False)),
        ("admin.fulfillment_card", admin_kb.fulfillment_card(ORDER_ID, "buyer")),
        ("admin.fulfillment_card(no username)", admin_kb.fulfillment_card(ORDER_ID, None)),
        ("admin.promos", admin_kb.promos([_promo()], 0, 1)),
        ("admin.promo_card", admin_kb.promo_card(_promo())),
        ("admin.promo_scope", admin_kb.promo_scope(1, [category])),
        ("admin.broadcast_menu", admin_kb.broadcast_menu([Broadcast(id=1, admin_id=1, status="done", sent=5, total=5)])),
        ("admin.broadcast_confirm", admin_kb.broadcast_confirm(1, 1543)),
        ("admin.broadcast_running", admin_kb.broadcast_running(1)),
        ("admin.texts", admin_kb.texts([TextEntry(key="welcome", value="x", title="Приветствие")], 0, 6)),
        ("admin.settings", admin_kb.settings([SettingEntry(key="shop_name", value="Shop", title="Название")], 0, 2)),
        ("admin.channels", admin_kb.channels([_channel()])),
        ("admin.admins", admin_kb.admins([Admin(id=1, user_id=BUYER_ID)], [BUYER_ID])),
        # Палитра цвета категории: каждая кнопка выкрашена в тот стиль, который
        # предлагает, — то есть проверяет сама себя на допустимость.
        ("admin.accent_picker", admin_kb.accent_picker("a:cats:0", "a:cat_newaccent:")),
        ("admin.admins(removable)", admin_kb.admins([Admin(id=2, user_id=111)], [BUYER_ID])),
        ("admin.export_menu", admin_kb.export_menu()),
        # Подтверждение — универсальный виджет, но в обходе он получает
        # НАСТОЯЩИЕ адреса: на выдуманных «a:x» проверка обработчиков ниже
        # проверяла бы заготовку теста, а не бота.
        (
            "admin.confirm",
            admin_kb.confirm(f"a:cat_del_ok:{category.id}", f"a:cat:{category.id}"),
        ),
        ("admin.user_card", admin_kb.user_card(BUYER_ID, is_blocked=False)),
        ("admin.user_card(blocked)", admin_kb.user_card(BUYER_ID, is_blocked=True)),
        ("admin.users_menu", admin_kb.users_menu()),
    ]


# --- сами проверки ----------------------------------------------------------


# Клавиатуры собираются ВНУТРИ тестов, а не в декораторе parametrize.
#
# Причина конкретная: если сборка клавиатуры падает (например, из-за стиля,
# который Telegram не принимает), при сборке в декораторе pytest сообщает об
# ошибке сбора тестов и возвращает код «прогон не состоялся». Поломка кода
# должна давать красный тест, а не неопределённость.


def _walk(keyboards):
    for name, markup in keyboards:
        for row in markup.inline_keyboard:
            for button in row:
                yield name, button


def _buttons(markup: InlineKeyboardMarkup) -> list[InlineKeyboardButton]:
    return [button for row in markup.inline_keyboard for button in row]


def _urls(markup: InlineKeyboardMarkup) -> list[str]:
    return [button.url for button in _buttons(markup) if button.url]


def _callbacks(markup: InlineKeyboardMarkup) -> list[str]:
    return [button.callback_data for button in _buttons(markup) if button.callback_data]


def _button_with(markup: InlineKeyboardMarkup, callback_data: str) -> InlineKeyboardButton:
    """Единственная кнопка с этим адресом. Две одинаковых — тоже поломка."""
    found = [button for button in _buttons(markup) if button.callback_data == callback_data]
    assert len(found) == 1, f"кнопок {callback_data!r} на экране: {len(found)}"
    return found[0]


# Не экраны, а куски экранов: `kb` собирает готовые ряды в клавиатуру,
# `back_row` возвращает ряд кнопок. Список закрытый и перечислен здесь ЯВНО —
# см. комментарий в `_screen_builders`.
_NOT_A_SCREEN = {"kb", "back_row"}


def _screen_builders(module: ModuleType) -> set[str]:
    """Публичные функции модуля, которые обязаны быть в обходе.

    Признак «возвращает клавиатуру» намеренно НЕ берётся из аннотации возврата.
    Раньше брался — и экран, объявленный без `-> InlineKeyboardMarkup`, тихо
    выпадал и из обхода, и из этой сверки: тест сообщал о полном покрытии,
    которого не было. Аннотация — оформление, забыть её ничего не стоит.

    Поэтому правило обратное и неудобное намеренно: любая новая публичная
    функция модуля клавиатур считается экраном, пока её не внесли в
    `_NOT_A_SCREEN` руками. Автор нового помощника получит красный тест и
    одну строку правки; автор нового экрана — красный тест вместо тишины.
    """
    return {
        name
        for name, obj in vars(module).items()
        if not name.startswith("_")
        and name not in _NOT_A_SCREEN
        and inspect.isfunction(obj)
        and obj.__module__ == module.__name__
    }


def test_walk_covers_every_screen() -> None:
    """Новый экран без места в обходе — дыра ровно там, где её не ждут.

    Тест сверяет список выше с тем, что реально объявлено в модулях клавиатур,
    в обе стороны: не только забытый экран, но и опечатка в имени ветки
    («admin.fulfilment_card») делает обход тише, чем он выглядит.
    """
    walked = {name.split("(")[0] for name, _ in _all_keyboards()}
    expected = {f"user.{name}" for name in _screen_builders(user_kb)}
    expected |= {f"admin.{name}" for name in _screen_builders(admin_kb)}

    assert not sorted(expected - walked), (
        "экраны есть в коде, но не проверяются: " + ", ".join(sorted(expected - walked))
    )
    assert not sorted(walked - expected), (
        "в обходе имена, которых нет в коде: " + ", ".join(sorted(walked - expected))
    )


def _callback_filters() -> list[MagicFilter]:
    """Условия по `callback_data` у всех зарегистрированных обработчиков.

    `fallback` пропускается сознательно: он ловит ВСЁ подряд и отвечает
    «кнопка устарела». Если считать его обработчиком, любая опечатка окажется
    «обработанной», и проверка ниже станет тождеством.

    Роутеры импортируются ЗДЕСЬ, а не наверху файла: поломка импорта в любом
    хендлере иначе роняла бы сбор всего файла, и проверки клавиатур — которым
    хендлеры не нужны — превращались бы в «прогон не состоялся».
    """
    from bot.handlers import build_router

    magics: list[MagicFilter] = []

    def collect(router) -> None:
        if router.name != "fallback":
            for handler in router.callback_query.handlers:
                for flt in handler.filters or []:
                    owner = getattr(flt.callback, "__self__", None)
                    if isinstance(owner, MagicFilter):
                        magics.append(owner)
        for sub in router.sub_routers:
            collect(sub)

    collect(build_router())
    return magics


def test_every_callback_reaches_a_handler() -> None:
    """У каждой кнопки есть обработчик, который её примет.

    Опечатка в `callback_data` — самая тихая поломка в боте: кнопка на месте,
    выглядит рабочей, стиль верный, в лимит влезает, и все проверки выше
    зелёные. Нажатие уходит в `fallback`, покупатель получает «кнопка
    устарела» и не понимает, что делать.

    Сверка идёт не со списком строк в тесте (он разъедется с ботом за неделю),
    а с настоящими фильтрами настоящих роутеров: `callback_data` прогоняется
    через них ровно так, как это сделает aiogram в бою.
    """
    filters = _callback_filters()
    assert len(filters) > 50, "обработчики не собрались — проверка стала пустой"

    orphans = sorted(
        {
            f"{name}: {button.callback_data}"
            for name, button in _walk(_all_keyboards())
            if button.callback_data
            and not any(f.resolve(SimpleNamespace(data=button.callback_data)) for f in filters)
        }
    )
    assert not orphans, "кнопки, которые никто не обработает:\n" + "\n".join(orphans)


def test_styles_are_accepted_by_telegram() -> None:
    bad = [
        f"{name}: {button.text!r} → стиль {button.style!r}"
        for name, button in _walk(_all_keyboards())
        if button.style is not None and button.style not in ALLOWED_STYLES
    ]
    assert not bad, "Telegram отвергнет всю клавиатуру:\n" + "\n".join(bad)


def test_callback_data_fits_the_limit() -> None:
    """callback_data длиннее 64 байт Telegram тоже не принимает."""
    bad = [
        f"{name}: {button.callback_data!r} — {len(button.callback_data.encode())} байт"
        for name, button in _walk(_all_keyboards())
        if button.callback_data and len(button.callback_data.encode()) > CALLBACK_LIMIT
    ]
    assert not bad, "\n".join(bad)


def test_every_button_leads_somewhere() -> None:
    """Кнопка без действия читается как поломка."""
    bad = [
        f"{name}: {button.text!r}"
        for name, button in _walk(_all_keyboards())
        if not (button.callback_data or button.url)
    ]
    assert not bad, "кнопки ничего не делают:\n" + "\n".join(bad)


def test_url_buttons_are_https() -> None:
    """Ссылка на кнопке — только внешний https.

    `tg://user?id=…` выглядит рабочим и в коде, и в тесте на «кнопка куда-то
    ведёт», но открывается лишь при определённых настройках приватности
    покупателя, а на части клиентов молча не делает ничего. Схемы кроме https
    Bot API либо режет, либо превращает в неработающую кнопку — обе беды видны
    только в бою.
    """
    urls = [(name, button.url) for name, button in _walk(_all_keyboards()) if button.url]
    assert urls, "в обходе не осталось ни одной ссылки — проверка стала пустой"

    bad = [f"{name}: {url}" for name, url in urls if not url.startswith("https://")]
    assert not bad, "недопустимая схема ссылки:\n" + "\n".join(bad)


# --- ветки, где кнопки не должно быть вовсе ---------------------------------


def test_fulfillment_card_without_username_has_no_link() -> None:
    """У покупателя без юзернейма ссылки на него не существует.

    Соблазн собрать `https://t.me/None` или `tg://user?id=…` велик, и оба
    варианта Telegram отвергает вместе со ВСЕЙ клавиатурой: администратор
    получает сообщение о новом заказе без единой кнопки и не может ни
    подтвердить работу, ни открыть заказ.
    """
    markup = admin_kb.fulfillment_card(ORDER_ID, None)

    assert _urls(markup) == [], "ссылка на покупателя без юзернейма"
    # Кнопка «Связаться» пропала — остальные обязаны остаться на месте.
    assert f"a:done:{ORDER_ID}" in _callbacks(markup)
    assert f"a:order:{ORDER_ID}" in _callbacks(markup)


def test_fulfillment_card_with_username_links_to_that_buyer() -> None:
    """Юзернейм есть — ссылка ведёт именно на него, а не на заглушку."""
    markup = admin_kb.fulfillment_card(ORDER_ID, "buyer")

    assert _urls(markup) == ["https://t.me/buyer"]
    assert f"a:done:{ORDER_ID}" in _callbacks(markup)


def test_order_card_without_username_has_no_link() -> None:
    """Та же ловушка в карточке заказа: ветка другая, авария одна и та же."""
    order = _order()
    markup = admin_kb.order_card(order, can_refund=True, can_confirm=True)

    assert _urls(markup) == []
    assert f"a:done:{order.id}" in _callbacks(markup)


def test_order_card_hides_actions_it_cannot_perform() -> None:
    """Возврат и подтверждение выдаются по флагам, а не «всегда».

    Кнопка, после которой приходит отказ, для администратора неотличима от
    поломки бота: он жмёт «Вернуть деньги» по закрытому заказу и не понимает,
    сработало или нет.
    """
    order = _order(status=OrderStatus.REFUNDED)
    callbacks = _callbacks(admin_kb.order_card(order, can_refund=False, can_confirm=False))

    assert f"a:order_refund:{order.id}" not in callbacks
    assert f"a:done:{order.id}" not in callbacks
    # Просмотр платежей доступен всегда — это чтение, а не действие.
    assert f"a:order_pays:{order.id}" in callbacks


def test_product_card_without_rate_offers_no_purchase() -> None:
    """Нет курса — нет цены, а значит и покупать нечего.

    `total_kop=None` означает, что курс ЦБ не получен. Кнопка «Купить» в этот
    момент ведёт в отказ: рублёвую сумму заказа посчитать не из чего.
    """
    product = _product()
    callbacks = _callbacks(user_kb.product_card(product, None, "u:cat:1:0"))

    assert f"u:buy:{product.id}" not in callbacks, "кнопка покупки без курса"
    # Уйти с экрана всё равно можно — иначе покупатель заперт в карточке.
    assert callbacks, "карточка осталась совсем без кнопок"


def test_product_card_with_price_offers_purchase() -> None:
    """Обратная сторона предыдущего теста: с ценой кнопка обязана быть.

    Без этой пары «убрать кнопку навсегда» проходит проверку как исправление.
    """
    product = _product()
    markup = user_kb.product_card(product, 178200, "u:cat:1:0")

    assert f"u:buy:{product.id}" in _callbacks(markup)
    # Сумма к оплате видна до нажатия. Форматтер общий для всего бота, поэтому
    # сверяемся с ним, а не с записью числа: проверяем ЧИСЛО, а не разделители.
    price = format_kop(178200)
    # Вырожденный форматтер («» вместо суммы) превращал бы строку ниже в
    # тождество: пустое входит в любой текст, и проверка молча исчезала.
    assert price.strip(), "форматтер сумм вернул пустоту — сверять нечем"
    assert any(price in button.text for button in _buttons(markup)), (
        "итоговая сумма не попала на кнопку покупки"
    )


def test_send_credentials_points_at_its_order() -> None:
    """Кнопка возврата к вводу реквизитов должна нести номер заказа.

    Оплаченных заказов у покупателя может быть несколько; кнопка без номера
    отправит логин и пароль не в тот заказ.
    """
    callbacks = _callbacks(user_kb.send_credentials(ORDER_ID))

    assert f"u:creds:{ORDER_ID}" in callbacks


def test_payment_methods_keeps_a_way_out() -> None:
    """Экран выбора оплаты без отмены — тупик с замороженными деньгами."""
    order = _order()
    callbacks = _callbacks(user_kb.payment_methods(order, _methods()))

    assert f"u:cancel:{order.id}" in callbacks
    # Каждый способ оплаты — отдельная кнопка со своим кодом.
    for method in _methods():
        assert f"u:pay:{order.id}:{method.code}" in callbacks





def test_payment_link_leads_to_the_payment_page() -> None:
    """Ссылка провайдера пришла — она на кнопке, ровно та, что дал провайдер."""
    order = _order()

    assert _urls(user_kb.payment_link(order)) == [order.pay_url]


def test_payment_link_without_a_url_still_lets_the_order_go() -> None:
    """Ссылки ещё нет — экран собирается, но и врать кнопкой не должен.

    Парная к предыдущей: без пары «убрать кнопку оплаты навсегда» выглядит
    как аккуратная обработка пустого `pay_url`.
    """
    order = _order(pay_url=None)
    markup = user_kb.payment_link(order)

    assert _urls(markup) == []
    callbacks = _callbacks(markup)
    assert f"u:check:{order.id}" in callbacks
    assert f"u:cancel:{order.id}" in callbacks


def test_main_menu_hides_the_admin_panel_from_buyers() -> None:
    """Вход в админку показывается только администратору.

    Права проверяются не здесь, и отказ покупатель получит. Но кнопка «Админ-
    панель» в чужом меню — это сообщение «тут есть дверь», и она приглашает её
    подёргать. Кнопка отсутствует у покупателя и присутствует у администратора:
    порознь эти два утверждения ловят только одну из двух поломок.
    """
    assert "a:menu" not in _callbacks(user_kb.main_menu(is_admin=False))
    assert "a:menu" in _callbacks(user_kb.main_menu(is_admin=True))


def test_owner_cannot_be_removed_from_admins() -> None:
    """Администратора из окружения удалить нельзя.

    Он последняя дверь в магазин. Удалить его — значит остаться без админки
    совсем: восстановить доступ можно будет только правкой базы руками.
    Кнопка удаления у остальных обязана остаться, иначе «убрать кнопку у всех»
    сойдёт за исправление.
    """
    owner, hired = BUYER_ID, 111

    owner_row = _callbacks(admin_kb.admins([Admin(id=1, user_id=owner)], [owner]))
    assert f"a:admin_del:{owner}" not in owner_row

    hired_row = _callbacks(admin_kb.admins([Admin(id=2, user_id=hired)], [owner]))
    assert f"a:admin_del:{hired}" in hired_row


def test_pager_never_points_outside_the_pages() -> None:
    """Перелистывание не уводит за края списка.

    Страница «-1» или «за последней» — это пустой экран и ощущение, что бот
    сломался. Проверяется через настоящий экран, а не через `pager_row`
    отдельно: важно, что за края не уходит именно то, что видит покупатель.
    """
    items, pages = [_category()], 3

    first = _callbacks(user_kb.categories(items, 0, pages))
    assert "u:cats:-1" not in first, "с первой страницы предлагают уйти назад"
    assert "u:cats:1" in first

    last = _callbacks(user_kb.categories(items, pages - 1, pages))
    assert f"u:cats:{pages}" not in last, "с последней страницы предлагают уйти вперёд"
    assert "u:cats:1" in last

    single = _callbacks(user_kb.categories(items, 0, 1))
    assert not [c for c in single if c.startswith("u:cats:")], (
        "листалка на единственной странице"
    )


def test_screens_keep_the_way_back() -> None:
    """«Назад» ведёт туда, откуда пришли, а не в общее меню.

    Карточка товара открывается из категории, покупки — из профиля. Потеря
    ряда навигации не роняет ни один экран и не видна ни одной проверке выше:
    кнопки на месте, стили верные, — просто выход остался один, «в начало»,
    и покупатель каждый раз проходит каталог заново.
    """
    back_to_category = f"u:cat:{CATEGORY_ID}:0"
    assert back_to_category in _callbacks(
        user_kb.product_card(_product(), 178200, back_to_category)
    )
    assert "u:profile" in _callbacks(user_kb.purchases([_order()], 0, 1))
    assert "u:profile" in _callbacks(user_kb.send_credentials(ORDER_ID))

    # Тот же ряд без адреса возврата: «Назад» не выдумывается на пустом месте.
    assert [button.callback_data for button in nav_row(back_data=None)] == ["u:menu"]


# --- надпись на кнопке-переключателе -----------------------------------------
#
# Адрес у переключателя один и тот же в обоих положениях: нажатие переворачивает
# состояние. Значит вся видимая часть договора — НАДПИСЬ. Перепутанные местами
# ветки не роняют ни одну проверку выше (кнопка на месте, стиль допустимый,
# обработчик есть), а администратор жмёт «Заблокировать» и разблокирует.


def test_toggle_button_names_what_pressing_will_do() -> None:
    """Включённое предлагают выключить, и наоборот."""
    product = _product()
    product.is_active = True
    active = _button_with(admin_kb.product_card(product), f"a:prod_toggle:{product.id}")
    product.is_active = False
    hidden = _button_with(admin_kb.product_card(product), f"a:prod_toggle:{product.id}")

    assert "Выключить" in active.text, f"включённый товар предлагают «{active.text}»"
    assert "Включить" in hidden.text, f"скрытый товар предлагают «{hidden.text}»"

    category = _category()
    category.is_active = True
    on = _button_with(admin_kb.category_card(category, 4), f"a:cat_toggle:{category.id}")
    category.is_active = False
    off = _button_with(admin_kb.category_card(category, 4), f"a:cat_toggle:{category.id}")

    assert "Выключить" in on.text
    assert "Включить" in off.text


def test_block_button_names_what_pressing_will_do() -> None:
    """Незаблокированного предлагают заблокировать, и наоборот.

    Ошибка тут стоит покупателя: администратор открывает карточку жалобщика,
    жмёт «Заблокировать» — и снимает блокировку с того, кого только что закрыл.
    """
    free = _button_with(admin_kb.user_card(BUYER_ID, is_blocked=False), f"a:user_block:{BUYER_ID}")
    blocked = _button_with(admin_kb.user_card(BUYER_ID, is_blocked=True), f"a:user_block:{BUYER_ID}")

    assert "Заблокировать" in free.text and "Разблокировать" not in free.text
    assert "Разблокировать" in blocked.text


def test_orders_filter_button_offers_the_other_view() -> None:
    """Кнопка фильтра называет то, куда переключит, а не то, что видно сейчас.

    Список у неё один и тот же адрес: `a:orders_filter` переворачивает фильтр.
    Перепутанные ветки дают экран, где под списком всех заказов написано «Все
    статусы» — администратор жмёт и решает, что кнопка не работает.
    """
    unfiltered = _button_with(admin_kb.orders([_order()], 0, 2, None), "a:orders_filter")
    filtered = _button_with(
        admin_kb.orders([_order()], 0, 2, OrderStatus.DELIVERED), "a:orders_filter"
    )

    assert unfiltered.text != filtered.text, "фильтр не меняет надпись"
    assert "Все" in filtered.text, f"из-под фильтра не предлагают выйти: «{filtered.text}»"
    assert "Все" not in unfiltered.text, f"без фильтра предлагают снять фильтр: «{unfiltered.text}»"


def test_subscription_links_to_the_channel_it_names() -> None:
    """Кнопка канала ведёт в тот канал, который на ней написан.

    Каналов в проверке подписки бывает несколько. Подставленный не тот адрес
    (общий, первый, захардкоженный) даёт вечный цикл: покупатель подписывается
    туда, куда его послали, а проверка подписки его всё равно не пускает.
    """
    first = _channel()
    second = Channel(
        id=2, chat_ref="@second", title="Второй", invite_url="https://t.me/second",
        sort_order=20, is_active=True,
    )
    buttons = _buttons(user_kb.subscription([first, second]))

    for channel in (first, second):
        linked = [b for b in buttons if b.url == channel.invite_url]
        assert len(linked) == 1, f"на «{channel.title}» ведёт кнопок: {len(linked)}"
        assert channel.title in linked[0].text, "надпись на кнопке от другого канала"


def test_buy_button_carries_the_product_not_its_category() -> None:
    """В «Купить» уходит номер товара.

    Соседнее поле `product.category_id` подставляется опечаткой на раз, а
    последствие тихое: покупатель жмёт «Купить» на одном товаре и получает
    заказ на другой — или отказ «товар не найден», если номера разошлись.
    """
    product = _product()
    assert product.id != product.category_id, "заготовка снова прячет подмену"

    callbacks = _callbacks(user_kb.product_card(product, 178200, "u:cats:0"))

    assert f"u:buy:{product.id}" in callbacks
    assert f"u:buy:{product.category_id}" not in callbacks
