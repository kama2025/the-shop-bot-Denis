"""Деньги: разбор, форматирование, скидки, округление."""

from __future__ import annotations

import pytest

from bot.utils.money import (
    DISCOUNT_FIXED,
    DISCOUNT_PERCENT,
    NBSP,
    PriceParseError,
    apply_discount,
    discount_amount_kop,
    floor_to_ruble,
    format_kop,
    parse_price_to_kop,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("90", 9000),
        ("90.5", 9050),
        ("90,50", 9050),
        (" 1 500 ₽ ", 150000),
        ("0", 0),
        ("0,01", 1),
    ],
)
def test_parse_price(raw: str, expected: int) -> None:
    assert parse_price_to_kop(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "-5", "90.555", "₽"])
def test_parse_price_rejects_garbage(raw: str) -> None:
    with pytest.raises(PriceParseError):
        parse_price_to_kop(raw)


def test_parse_price_rejects_third_decimal() -> None:
    """Три знака после запятой молча теряли бы копейки."""
    with pytest.raises(PriceParseError):
        parse_price_to_kop("10.005")


@pytest.mark.parametrize(
    ("kop", "expected"),
    [
        (9000, f"90{NBSP}₽"),
        (8350, f"83,50{NBSP}₽"),
        (150000, f"1{NBSP}500{NBSP}₽"),
        (0, f"0{NBSP}₽"),
        (-9000, f"-90{NBSP}₽"),
    ],
)
def test_format(kop: int, expected: str) -> None:
    assert format_kop(kop) == expected


def test_format_with_sign() -> None:
    assert format_kop(500, with_sign=True) == f"+5{NBSP}₽"
    assert format_kop(-500, with_sign=True) == f"-5{NBSP}₽"


def test_format_output_survives_reparsing() -> None:
    """Отформатированную цену обязан разобрать наш же разборщик.

    Иначе админ, скопировавший цену из карточки товара, получит отказ
    «не похоже на число» — на неразрывном пробеле, которого он не видит.
    """
    for kop in (0, 1, 9000, 8350, 150000, 1234567):
        assert parse_price_to_kop(format_kop(kop)) == kop


def test_percent_discount() -> None:
    assert discount_amount_kop(10000, DISCOUNT_PERCENT, 10) == 1000


def test_fixed_discount_never_exceeds_total() -> None:
    """Скидка 500 ₽ на заказ в 90 ₽ не должна уводить итог в минус."""
    assert discount_amount_kop(9000, DISCOUNT_FIXED, 50000) == 9000


def test_percent_is_clamped() -> None:
    assert discount_amount_kop(10000, DISCOUNT_PERCENT, 200) == 10000
    assert discount_amount_kop(10000, DISCOUNT_PERCENT, -5) == 0


def test_floor_to_ruble() -> None:
    assert floor_to_ruble(8399) == 8300
    assert floor_to_ruble(8300) == 8300
    assert floor_to_ruble(-100) == 0


def test_apply_discount_keeps_invariant() -> None:
    """Итог = сумма − скидка. Считать их независимо нельзя."""
    for subtotal in (9000, 9999, 12345, 100000, 1):
        for percent in (0, 7, 10, 33, 100):
            discount, total = apply_discount(subtotal, DISCOUNT_PERCENT, percent)
            assert total == subtotal - discount
            assert total % 100 == 0, "итог обязан быть целым числом рублей"
            assert 0 <= total <= subtotal


def test_apply_discount_rounds_in_buyer_favour() -> None:
    """90 ₽ минус 7 % = 83,70 ₽ → покупатель платит 83 ₽, не 84 ₽."""
    discount, total = apply_discount(9000, DISCOUNT_PERCENT, 7)
    assert total == 8300
    assert discount == 700


def test_apply_discount_without_promo_still_floors() -> None:
    discount, total = apply_discount(9050, None, None)
    assert total == 9000
    assert discount == 50


def test_full_discount_gives_zero() -> None:
    discount, total = apply_discount(9000, DISCOUNT_PERCENT, 100)
    assert total == 0
    assert discount == 9000


def test_unknown_discount_type_raises() -> None:
    """Умолчание — отказ: неизвестный тип скидки не должен молча стать нулём."""
    with pytest.raises(ValueError):
        discount_amount_kop(9000, "mystery", 10)
