"""Цена: пересчёт долларов в рубли по курсу, наценка, округление."""

from __future__ import annotations

from decimal import Decimal

import pytest

from bot.services.pricing import base_kop, charge_kop, format_usd, with_markup


def test_base_converts_by_rate() -> None:
    """$20.00 по курсу 90,00 ₽ за доллар = 1800 ₽."""
    assert base_kop(2000, 9000) == 180_000


def test_charge_adds_markup() -> None:
    """Те же 1800 ₽ с наценкой 10 % = 1980 ₽."""
    assert charge_kop(2000, 9000, 10) == 198_000


def test_zero_markup_means_no_markup() -> None:
    assert charge_kop(2000, 9000, 0) == base_kop(2000, 9000) == 180_000


def test_free_item_stays_free() -> None:
    """Нулевая цена не должна обрасти наценкой из ниоткуда."""
    assert charge_kop(0, 9000, 30) == 0


def test_base_rounds_half_kopeck_up() -> None:
    """1 цент по курсу 90,50 ₽ — это ровно 90,5 копейки, значит 91.

    Встроенный round() округляет половину к чётному и вернул бы 90.
    """
    assert base_kop(1, 9050) == 91


def test_markup_rounds_half_kopeck_up() -> None:
    """2 копейки плюс 25 % — ровно 2,5 копейки, значит 3.

    Это тот самый случай, где round(2.5) == 2 разошёлся бы с бухгалтерией.
    """
    assert with_markup(2, 25) == 3


def test_charge_rounds_half_kopeck_up() -> None:
    """Половина вверх обязана дожить до итоговой суммы, а не только до базы."""
    assert charge_kop(2, 100, 25) == 3


@pytest.mark.parametrize("price_usd_cents", [0, 1, 3, 999, 1999, 2000, 123_456])
@pytest.mark.parametrize("rate_kop", [1, 8333, 9000, 9050, 12_345])
@pytest.mark.parametrize("markup_pct", [0, 1, 10, 33, 200])
def test_charge_equals_base_then_markup(
    price_usd_cents: int, rate_kop: int, markup_pct: int
) -> None:
    """Итог обязан совпадать с последовательным вызовом, иначе карточка товара
    и счёт на оплату покажут покупателю разные числа."""
    assert charge_kop(price_usd_cents, rate_kop, markup_pct) == with_markup(
        base_kop(price_usd_cents, rate_kop), markup_pct
    )


@pytest.mark.parametrize("price_usd_cents", [1, 7, 199, 2000, 33_333])
@pytest.mark.parametrize("rate_kop", [1, 8333, 9000, 9050])
def test_base_lands_on_nearest_kopeck(price_usd_cents: int, rate_kop: int) -> None:
    """Результат отстоит от точного значения не больше чем на полкопейки.

    Ловит перепутанный делитель и потерянный множитель: такие ошибки
    промахиваются в сто раз, а не на копейку.
    """
    exact = Decimal(price_usd_cents * rate_kop) / 100
    assert abs(Decimal(base_kop(price_usd_cents, rate_kop)) - exact) <= Decimal("0.5")


def test_markup_never_lowers_price() -> None:
    """Наценка не может уменьшить сумму — даже на копейку округления."""
    for markup in range(0, 101):
        assert with_markup(180_000, markup) >= 180_000


def test_markup_grows_monotonically() -> None:
    previous = with_markup(12_345, 0)
    for markup in range(1, 51):
        current = with_markup(12_345, markup)
        assert current >= previous
        previous = current


@pytest.mark.parametrize(
    ("price_usd_cents", "expected"),
    [
        (2000, "$20"),
        (1999, "$19.99"),
        (2050, "$20.50"),
        (0, "$0"),
        (1, "$0.01"),
        (10, "$0.10"),
        (100, "$1"),
        (123_456, "$1234.56"),
    ],
)
def test_format_usd(price_usd_cents: int, expected: str) -> None:
    assert format_usd(price_usd_cents) == expected


def test_format_usd_has_no_space_after_sign() -> None:
    """Пробел после $ ломает вёрстку карточки в Telegram."""
    assert not format_usd(1999).startswith("$ ")


def test_negative_price_rejected() -> None:
    with pytest.raises(ValueError):
        base_kop(-1, 9000)


def test_negative_rate_rejected() -> None:
    with pytest.raises(ValueError):
        base_kop(2000, -9000)


def test_zero_rate_rejected() -> None:
    """Нулевой курс — это не «бесплатно», а несохранённая настройка."""
    with pytest.raises(ValueError):
        base_kop(2000, 0)


def test_negative_markup_rejected() -> None:
    with pytest.raises(ValueError):
        with_markup(180_000, -1)


def test_negative_amount_rejected() -> None:
    with pytest.raises(ValueError):
        with_markup(-100, 10)


def test_format_usd_rejects_negative() -> None:
    with pytest.raises(ValueError):
        format_usd(-1)


@pytest.mark.parametrize(
    ("price_usd_cents", "rate_kop", "markup_pct"),
    [
        (-1, 9000, 10),
        (2000, -9000, 10),
        (2000, 0, 10),
        (2000, 9000, -1),
    ],
)
def test_charge_rejects_bad_input(
    price_usd_cents: int, rate_kop: int, markup_pct: int
) -> None:
    """Итоговая функция обязана падать на том же, на чём падают её части."""
    with pytest.raises(ValueError):
        charge_kop(price_usd_cents, rate_kop, markup_pct)


@pytest.mark.parametrize("bad", [20.0, "2000", Decimal("2000"), None, True])
def test_non_int_money_rejected(bad: object) -> None:
    """float здесь запрещён: Decimal(0.1) развернётся в двоичный хвост.

    bool ловим отдельно — он подкласс int, и переданный по ошибке флаг
    посчитался бы ценой в одну копейку.
    """
    with pytest.raises(TypeError):
        base_kop(bad, 9000)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        base_kop(2000, bad)  # type: ignore[arg-type]
