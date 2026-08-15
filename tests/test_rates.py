"""Курс ЦБ: разбор XML и поход в сеть через подменённый клиент.

Реальных запросов здесь нет и быть не должно: тест, зависящий от доступности
cbr.ru, красный не тогда, когда сломан код.
"""

from __future__ import annotations

import pytest

from bot.services.rates import CBR_URL, RateError, fetch_cbr, parse_cbr_xml

DECLARATION = '<?xml version="1.0" encoding="windows-1251"?>'


def _valute(
    code: str = "USD",
    nominal: str | None = "1",
    value: str | None = "90,1234",
    name: str = "Доллар США",
) -> str:
    """Один <Valute>. None у nominal/value означает «поля нет вовсе»."""
    parts = ["<NumCode>840</NumCode>", f"<CharCode>{code}</CharCode>"]
    if nominal is not None:
        parts.append(f"<Nominal>{nominal}</Nominal>")
    parts.append(f"<Name>{name}</Name>")
    if value is not None:
        parts.append(f"<Value>{value}</Value>")
    return '<Valute ID="R01235">' + "".join(parts) + "</Valute>"


def _document(*valutes: str, declaration: bool = True) -> str:
    head = f"{DECLARATION}\n" if declaration else ""
    return (
        head
        + '<ValCurs Date="15.08.2026" name="Foreign Currency Market">'
        + ("".join(valutes) or _valute())
        + "</ValCurs>"
    )


class _HttpError(Exception):
    """Заглушка под httpx.HTTPStatusError / TransportError."""


class _StubResponse:
    def __init__(self, content: bytes, *, status: int = 200, charset: str | None = None) -> None:
        self.content = content
        self.status_code = status
        # httpx кладёт сюда charset из заголовка и None, если его не прислали.
        self.charset_encoding = charset

    def raise_for_status(self) -> "_StubResponse":
        if self.status_code >= 400:
            raise _HttpError(f"HTTP {self.status_code}")
        return self


class _StubClient:
    """Минимальный двойник httpx.AsyncClient: помнит запрошенные URL."""

    def __init__(self, response: _StubResponse | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.urls: list[str] = []

    async def get(self, url: str, **kwargs: object) -> _StubResponse:
        self.urls.append(url)
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


# --- разбор -----------------------------------------------------------------


def test_parses_plain_answer() -> None:
    assert parse_cbr_xml(_document()) == 9012


def test_parses_without_declaration() -> None:
    assert parse_cbr_xml(_document(declaration=False)) == 9012


def test_comma_is_decimal_separator() -> None:
    """«1,50» — это полтора рубля, а не полторы тысячи и не ошибка разбора."""
    assert parse_cbr_xml(_document(_valute(value="1,50"))) == 150


def test_nominal_divides() -> None:
    """У йены Nominal=100: курс за единицу в сто раз меньше напечатанного."""
    document = _document(_valute(code="JPY", nominal="100", value="57,1234", name="Японских иен"))
    assert parse_cbr_xml(document, "JPY") == 57


def test_nominal_not_ignored() -> None:
    """Ровные числа: 10,00 за 10 единиц — это ровно 1 рубль за единицу."""
    assert parse_cbr_xml(_document(_valute(nominal="10", value="10,00"))) == 100


def test_rounds_half_up() -> None:
    """9012,5 копейки → 9013. Банковское округление дало бы 9012."""
    assert parse_cbr_xml(_document(_valute(value="90,125"))) == 9013


def test_returns_int_not_float() -> None:
    result = parse_cbr_xml(_document())
    assert isinstance(result, int) and not isinstance(result, bool)


def test_picks_requested_currency() -> None:
    document = _document(
        _valute(code="EUR", value="100,0000", name="Евро"),
        _valute(code="USD", value="90,1234"),
        _valute(code="JPY", nominal="100", value="57,1234", name="Японских иен"),
    )
    assert parse_cbr_xml(document, "USD") == 9012
    assert parse_cbr_xml(document, "EUR") == 10000


def test_char_code_case_and_spaces() -> None:
    assert parse_cbr_xml(_document(), " usd ") == 9012


def test_unknown_currency() -> None:
    with pytest.raises(RateError):
        parse_cbr_xml(_document(), "XYZ")


@pytest.mark.parametrize(
    "broken",
    [
        "",
        "   ",
        "<ValCurs><Valute><CharCode>USD</CharCode>",
        "не xml вовсе",
        "{\"USD\": 90}",
        f"{DECLARATION}<ValCurs>",
    ],
)
def test_broken_xml(broken: str) -> None:
    with pytest.raises(RateError):
        parse_cbr_xml(broken)


@pytest.mark.parametrize("value", ["", "   ", "0", "0,0000", "-90,1234", "-0,01"])
def test_bad_value_numbers(value: str) -> None:
    with pytest.raises(RateError):
        parse_cbr_xml(_document(_valute(value=value)))


@pytest.mark.parametrize("value", ["девяносто", "9O,1234", "90,12,34", "NaN", "Infinity", "1e"])
def test_non_numeric_value(value: str) -> None:
    with pytest.raises(RateError):
        parse_cbr_xml(_document(_valute(value=value)))


def test_missing_value_tag() -> None:
    with pytest.raises(RateError):
        parse_cbr_xml(_document(_valute(value=None)))


def test_missing_nominal_tag() -> None:
    """Молча подставить Nominal=1 нельзя: у йены это ошибка в сто раз."""
    with pytest.raises(RateError):
        parse_cbr_xml(_document(_valute(nominal=None)))


@pytest.mark.parametrize("nominal", ["0", "-1", "", "сто"])
def test_bad_nominal(nominal: str) -> None:
    with pytest.raises(RateError):
        parse_cbr_xml(_document(_valute(nominal=nominal)))


def test_rate_rounding_to_zero_rejected() -> None:
    """Нулевой курс дальше по коду означает деление на ноль или бесплатный товар."""
    with pytest.raises(RateError):
        parse_cbr_xml(_document(_valute(value="0,000004")))


# --- сеть -------------------------------------------------------------------


async def test_fetch_returns_rate_from_cbr_url() -> None:
    client = _StubClient(_StubResponse(_document().encode("windows-1251")))
    assert await fetch_cbr(client) == 9012
    assert client.urls == [CBR_URL]


async def test_fetch_decodes_by_declaration_when_header_silent() -> None:
    """Кириллица в Name в windows-1251 — невалидный utf-8; разбор по utf-8 упал бы."""
    body = _document().encode("windows-1251")
    with pytest.raises(UnicodeDecodeError):
        body.decode("utf-8")
    client = _StubClient(_StubResponse(body, charset=None))
    assert await fetch_cbr(client) == 9012


async def test_fetch_decodes_by_header_charset() -> None:
    body = _document(declaration=False).encode("windows-1251")
    client = _StubClient(_StubResponse(body, charset="windows-1251"))
    assert await fetch_cbr(client) == 9012


async def test_fetch_other_currency() -> None:
    document = _document(_valute(code="EUR", value="100,5000", name="Евро"))
    client = _StubClient(_StubResponse(document.encode("windows-1251")))
    assert await fetch_cbr(client, "EUR") == 10050


async def test_fetch_network_error() -> None:
    client = _StubClient(error=_HttpError("connection refused"))
    with pytest.raises(RateError):
        await fetch_cbr(client)


async def test_fetch_http_error_status() -> None:
    client = _StubClient(_StubResponse(b"<html>503</html>", status=503))
    with pytest.raises(RateError):
        await fetch_cbr(client)


async def test_fetch_garbage_body() -> None:
    client = _StubClient(_StubResponse(b"<html>\xef\xbf\xbdservice unavailable</html>"))
    with pytest.raises(RateError):
        await fetch_cbr(client)


async def test_fetch_currency_absent() -> None:
    client = _StubClient(_StubResponse(_document().encode("windows-1251")))
    with pytest.raises(RateError):
        await fetch_cbr(client, "XYZ")
