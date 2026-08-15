"""Цена товара: пересчёт из долларов в рубли по курсу плюс наценка.

Единицы хранения — целые: доллары в центах, рубли в копейках. float запрещён:
цена проходит через два умножения и два округления, и двоичная погрешность
вылезет расхождением с эквайрингом на копейку, которое будут искать день.

Округление — половина ВВЕРХ. Встроенный round() округляет половину к чётному
(round(2.5) == 2), и итог разошёлся бы с расчётом бухгалтерии.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

CENTS_IN_DOLLAR = 100
PERCENT_BASE = 100


def _require_int(value: object, name: str) -> int:
    """Пускает дальше только настоящий int.

    bool отсеиваем отдельно: он подкласс int, и случайно переданный флаг
    посчитался бы ценой в одну копейку вместо явного отказа.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} должен быть int, получено {type(value).__name__}")
    return value


def _round_half_up(value: Decimal) -> int:
    """Decimal → целое, половина вверх."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def base_kop(price_usd_cents: int, rate_kop: int) -> int:
    """Чистый пересчёт без наценки.

    price_usd_cents — цена товара в центах (2000 = $20.00)
    rate_kop — копеек за ОДИН доллар (9000 = 90.00 руб)
    Возвращает копейки.
    """
    price = _require_int(price_usd_cents, "price_usd_cents")
    rate = _require_int(rate_kop, "rate_kop")
    if price < 0:
        raise ValueError("Цена не может быть отрицательной")
    if rate <= 0:
        # Ноль отделён от минуса намеренно: нулевой курс — это не «бесплатно»,
        # а незаполненная настройка, по которой нельзя отдавать товар.
        raise ValueError("Курс должен быть больше нуля")
    # Делим на 100, потому что цена в центах, а курс задан за целый доллар.
    return _round_half_up(Decimal(price) * Decimal(rate) / CENTS_IN_DOLLAR)


def with_markup(amount_kop: int, markup_pct: int) -> int:
    """Добавляет наценку в процентах."""
    amount = _require_int(amount_kop, "amount_kop")
    markup = _require_int(markup_pct, "markup_pct")
    if amount < 0:
        raise ValueError("Сумма не может быть отрицательной")
    if markup < 0:
        # Отрицательная наценка — это скидка, а она живёт в bot/utils/money.py
        # со своим правилом округления (вниз, в пользу покупателя).
        raise ValueError("Наценка не может быть отрицательной")
    return _round_half_up(Decimal(amount) * (PERCENT_BASE + markup) / PERCENT_BASE)


def charge_kop(price_usd_cents: int, rate_kop: int, markup_pct: int) -> int:
    """Итог к оплате: пересчёт плюс наценка. Ровно base_kop + with_markup.

    Округляем дважды — сначала курс, потом наценку. Так итог сходится с тем,
    что покупатель видел в карточке: там показана уже округлённая базовая цена.
    """
    return with_markup(base_kop(price_usd_cents, rate_kop), markup_pct)


def format_usd(price_usd_cents: int) -> str:
    """$20 для круглой суммы, $19.99 для дробной. Без пробела после знака."""
    price = _require_int(price_usd_cents, "price_usd_cents")
    if price < 0:
        raise ValueError("Цена не может быть отрицательной")
    dollars, cents = divmod(price, CENTS_IN_DOLLAR)
    if cents == 0:
        return f"${dollars}"
    return f"${dollars}.{cents:02d}"
