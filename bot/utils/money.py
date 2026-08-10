"""Деньги.

Единица хранения — копейка, целое число. Никаких float: 0.1 + 0.2 != 0.3, и
однажды это вылезет в отчёте расхождением на копейку, которое будут искать день.

Правило округления итога согласовано с заказчиком: после применения скидки
итог округляется **вниз до целого рубля**, то есть в пользу покупателя.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

KOPECKS_IN_RUBLE = 100

DISCOUNT_PERCENT = "percent"
DISCOUNT_FIXED = "fixed"

# Неразрывный пробел: «1 500 ₽» не должно переноситься по строкам, иначе
# в узком экране Telegram цена рвётся пополам и читается как две.
NBSP = " "


class PriceParseError(ValueError):
    """Введённая цена не разобрана."""


def parse_price_to_kop(raw: str) -> int:
    """Разбирает пользовательский ввод цены в копейки.

    Принимает «90», «90.5», «90,50», « 1 500 ₽ ». Отрицательное — ошибка,
    больше двух знаков после запятой — ошибка (иначе тихо потеряем копейки).
    """
    text = (
        str(raw)
        .replace(NBSP, "")
        .replace(" ", "")
        .replace("₽", "")
        .replace("р.", "")
        .replace("руб", "")
        .replace(",", ".")
        .strip()
    )
    if not text:
        raise PriceParseError("Пустая строка")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise PriceParseError(f"Не похоже на число: {raw!r}") from exc
    if value < 0:
        raise PriceParseError("Цена не может быть отрицательной")
    if value.as_tuple().exponent < -2:
        raise PriceParseError("Максимум два знака после запятой")
    return int(value * KOPECKS_IN_RUBLE)


def format_kop(kop: int, with_sign: bool = False) -> str:
    """Копейки → «1 500 ₽» или «83,50 ₽»."""
    sign = ""
    if with_sign and kop > 0:
        sign = "+"
    elif kop < 0:
        sign = "-"
    absolute = abs(int(kop))
    rubles, kopecks = divmod(absolute, KOPECKS_IN_RUBLE)
    grouped = f"{rubles:,}".replace(",", NBSP)
    tail = "" if kopecks == 0 else f",{kopecks:02d}"
    return f"{sign}{grouped}{tail}{NBSP}₽"


def discount_amount_kop(subtotal_kop: int, discount_type: str, discount_value: int) -> int:
    """Размер скидки в копейках до округления итога.

    Для процентной скидки `discount_value` — целые проценты (10 = 10 %).
    Для фиксированной — копейки.
    Скидка никогда не превышает сумму заказа.
    """
    if subtotal_kop <= 0:
        return 0
    if discount_type == DISCOUNT_PERCENT:
        percent = max(0, min(100, int(discount_value)))
        raw = subtotal_kop * percent // 100
    elif discount_type == DISCOUNT_FIXED:
        raw = max(0, int(discount_value))
    else:
        raise ValueError(f"Неизвестный тип скидки: {discount_type!r}")
    return min(raw, subtotal_kop)


def floor_to_ruble(kop: int) -> int:
    """Округление вниз до целого рубля."""
    if kop <= 0:
        return 0
    return (kop // KOPECKS_IN_RUBLE) * KOPECKS_IN_RUBLE


def apply_discount(
    subtotal_kop: int, discount_type: str | None, discount_value: int | None
) -> tuple[int, int]:
    """Считает итог заказа.

    Возвращает `(фактическая_скидка_коп, итог_коп)`.

    Фактическая скидка возвращается уже с учётом округления итога вниз, чтобы
    в заказе всегда выполнялось `итог = сумма_до_скидки - скидка`. Если считать
    их независимо, отчёт по продажам однажды не сойдётся, и виноватым окажется
    округление, которое никто не помнит.
    """
    subtotal_kop = max(0, int(subtotal_kop))
    if not discount_type or discount_value is None:
        total = floor_to_ruble(subtotal_kop)
        return subtotal_kop - total, total

    raw_discount = discount_amount_kop(subtotal_kop, discount_type, int(discount_value))
    total = floor_to_ruble(subtotal_kop - raw_discount)
    return subtotal_kop - total, total
