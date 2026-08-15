"""Заготовка kassa.ai: проверяется не «код написан», а что он остаётся честным.

Файл `bot/payments/kassa.py` существует ровно ради одного свойства: пока нет
документации API, провайдер обязан **отказывать**, а не притворяться рабочим.
Свойство это ничем не держалось — весь модуль можно было заменить на выдуманный
клиент с придуманной ссылкой на оплату, и ни один из 437 тестов не заметил бы.
Проверено мутацией: `create_invoice` возвращал `Invoice(pay_url=".../fake-1")`,
`fetch_status` — `confirmed`, `methods()` — живую кнопку «Карта»; сюита осталась
зелёной. Такой заказ ушёл бы в PENDING с несуществующим платежом, а покупатель
получил бы кнопку, ведущую в никуда.

Поэтому тесты ниже описывают именно контракт заготовки, а не её строки.
"""

from __future__ import annotations

import importlib
import inspect
import pathlib
import re

import pytest

from bot.payments.base import Invoice, PaymentProvider, ProviderError, StatusResult
from bot.payments.kassa import KassaProvider
from bot.payments.registry import PaymentRegistry

SECRET = "секрет-которого-не-должно-быть-в-тексте-ошибки"


def _provider() -> KassaProvider:
    return KassaProvider(
        base_url="https://example.invalid/",
        merchant_id="merchant-uuid",
        secret=SECRET,
        return_url="https://example.invalid/ok",
        failed_url="https://example.invalid/fail",
    )


# --- главное свойство: отказ, а не имитация ---------------------------------


async def test_create_invoice_refuses_instead_of_inventing_link() -> None:
    """Счёт не выставляется и ссылка не выдумывается.

    Возврат любого `Invoice` означал бы, что заказ уедет в PENDING с
    несуществующей транзакцией: поллер потом будет вечно спрашивать статус,
    а покупатель — смотреть на мёртвую ссылку.
    """
    provider = _provider()
    with pytest.raises(ProviderError) as info:
        result = await provider.create_invoice(
            order_id=1,
            amount_kop=18000,
            description="Тест",
            method_code="kassa:card",
            user_id=42,
            username="user",
        )
        assert not isinstance(result, Invoice), "заготовка вернула счёт вместо отказа"
    assert str(info.value).strip(), "у отказа должен быть текст"


async def test_fetch_status_refuses_instead_of_confirming() -> None:
    """Статус не выдумывается.

    `confirm_order` выдаёт товар по ответу этого метода. Любой возврат
    `StatusResult(status="confirmed")` — это бесплатная выдача товара.
    """
    provider = _provider()
    with pytest.raises(ProviderError) as info:
        result = await provider.fetch_status("txn-1")
        assert not isinstance(result, StatusResult), "заготовка подтвердила платёж"
    assert str(info.value).strip()


@pytest.mark.parametrize("method_name", ["create_invoice", "fetch_status"])
async def test_error_type_is_the_one_callers_catch(method_name: str) -> None:
    """Тип исключения — часть контракта, а не деталь.

    `bot/handlers/user/purchase.py` и `bot/services/payments.py` ловят именно
    `ProviderError`. Любое другое исключение пролетит мимо `except`: хендлер
    aiogram упадёт, покупатель не увидит вообще ничего вместо внятного
    «попробуйте другой способ».
    """
    provider = _provider()
    method = getattr(provider, method_name)
    kwargs: dict = (
        {"txn_id": "txn-1"}
        if method_name == "fetch_status"
        else {
            "order_id": 1,
            "amount_kop": 100,
            "description": "Тест",
            "method_code": "kassa:card",
            "user_id": 42,
        }
    )
    try:
        await method(**kwargs)
    except ProviderError:
        pass
    except Exception as exc:  # noqa: BLE001 — ровно это и проверяем
        pytest.fail(f"{method_name} возбудил {type(exc).__name__}, а ловят ProviderError")
    else:
        pytest.fail(f"{method_name} не возбудил исключение")


async def test_error_text_is_readable_and_hides_secret() -> None:
    """Текст ошибки уходит в журнал и в `ConfirmResult.detail`.

    Значит, он обязан объяснять причину по-русски и не содержать секрет
    мерчанта: `bot/services/payments.py` кладёт `str(exc)` в `detail`.
    """
    provider = _provider()
    with pytest.raises(ProviderError) as info:
        await provider.fetch_status("txn-1")
    text = str(info.value)
    assert SECRET not in text, "секрет мерчанта утёк в текст ошибки"
    assert "kassa" in text.lower()
    assert re.search(r"[а-яё]", text, re.IGNORECASE), "текст ошибки должен быть русским"


def test_no_invented_endpoints_in_module() -> None:
    """В модуле не должно быть ни одного адреса.

    Ровно это обещает docstring модуля. Выдуманный URL, который выглядит
    рабочим, уедет в бой и сломается на живом покупателе; отсутствие URL
    ломается только у нас.
    """
    module = importlib.import_module(KassaProvider.__module__)
    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]  # docstring модуля не считаем
    assert "http://" not in body and "https://" not in body, "в заготовке появился адрес"


# --- совместимость с остальным кодом ----------------------------------------


def test_methods_offers_nothing() -> None:
    """Ни одной кнопки оплаты.

    Кнопка, после которой покупатель получает ошибку, читается как поломка
    магазина, а не как «способ временно недоступен».
    """
    assert _provider().methods() == []


def test_signatures_match_provider_protocol() -> None:
    """Расхождение с протоколом ловится тестом, а не в бою.

    Заготовка нигде не инстанцируется, поэтому изменение `PaymentProvider`
    (например, новый обязательный аргумент) не проявится нигде, кроме первого
    настоящего вызова.
    """
    for name in ("methods", "create_invoice", "fetch_status", "close"):
        expected = inspect.signature(getattr(PaymentProvider, name))
        actual = inspect.signature(getattr(KassaProvider, name))
        assert actual == expected, f"{name}: {actual} вместо {expected}"
    assert inspect.iscoroutinefunction(KassaProvider.close)


async def test_registry_can_close_and_list_kassa() -> None:
    """Реестр обходит провайдеров одинаково.

    `__init__` реестра обходим намеренно: kassa ещё не подключена в
    `bot/config.py` и `bot/payments/registry.py`, а проверить нужно именно
    `methods()` и `close()` реестра на настоящем `KassaProvider` — отсутствие
    `close` уронило бы выключение бота.
    """
    registry = PaymentRegistry.__new__(PaymentRegistry)
    registry._providers = {"kassa": _provider()}

    assert registry.any_enabled is True
    assert registry.enabled_names == ["kassa"]
    assert registry.methods() == []
    assert registry.provider_of("kassa:card") is registry.get("kassa")
    await registry.close()
