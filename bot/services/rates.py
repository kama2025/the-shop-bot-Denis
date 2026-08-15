"""Курс валюты с сайта ЦБ РФ: запрос и разбор, ничего больше.

Хранение курса и расписание обновлений — не здесь: модуль ничего не знает ни
о базе, ни о боте, поэтому его можно дёргать откуда угодно и тестировать без
сети. Наружу отдаются целые копейки за ОДНУ единицу валюты — деньги во float
не переводим нигде.
"""

from __future__ import annotations

import inspect
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from xml.etree import ElementTree as ET

CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"

# ЦБ отдаёт windows-1251. Держим её последним запасным вариантом на случай,
# когда ни заголовок, ни пролог XML кодировку не назвали.
FALLBACK_ENCODING = "windows-1251"

# Пролог срезаем вместе с пробелами перед ним: XML с отступом в начале —
# невалидный («XML or text declaration not at start of entity»), а ЦБ вполне
# может отдать тело с лишним переводом строки. Кодировку из пролога к этому
# моменту уже прочитали по байтам, так что терять нечего.
_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>\s*")

# Кодировка, объявленная в самом XML; ищем по байтам, до декодирования.
_DECLARED_ENCODING = re.compile(
    rb"""^\s*<\?xml[^>]*?encoding\s*=\s*["']([\w.\-]+)["']""", re.IGNORECASE
)

_MISSING = object()


class RateError(Exception):
    """Курс получить не удалось."""


def parse_cbr_xml(xml_text: str, char_code: str = "USD") -> int:
    """Достаёт курс из ответа ЦБ. Возвращает копеек за ОДНУ единицу валюты."""
    try:
        root = ET.fromstring(_XML_DECLARATION.sub("", xml_text))
    except ET.ParseError as exc:
        raise RateError(f"ответ ЦБ не разбирается как XML: {exc}") from exc

    wanted = char_code.strip().upper()
    for valute in root.iter("Valute"):
        if (valute.findtext("CharCode") or "").strip().upper() != wanted:
            continue
        value = _positive_number(valute.findtext("Value"), f"{wanted}: Value")
        # Nominal по умолчанию не подставляем: у йены он 100, и молчаливая
        # единица дала бы курс, завышенный в сто раз.
        nominal = _positive_number(valute.findtext("Nominal"), f"{wanted}: Nominal")
        return _to_kopecks(value, nominal, wanted)

    raise RateError(f"в ответе ЦБ нет валюты {wanted}")


async def fetch_cbr(client, char_code: str = "USD") -> int:
    """Запрашивает и разбирает. client — aiohttp.ClientSession или httpx.AsyncClient.

    От клиента нужны только `await client.get(url)` и ответ, у которого есть
    `raise_for_status()` и тело в байтах. Оба клиента это дают, поэтому модуль
    не тянет за собой ни одной зависимости.

    Любая сетевая ошибка или неверный ответ → RateError.
    """
    try:
        response = await client.get(CBR_URL)
    except Exception as exc:  # клиент чужой, тип его исключений нам не подконтролен
        raise RateError(f"запрос к ЦБ не удался: {exc}") from exc

    try:
        response.raise_for_status()
    except Exception as exc:
        raise RateError(f"ЦБ ответил ошибкой: {exc}") from exc

    raw = getattr(response, "content", _MISSING)
    if not isinstance(raw, (bytes, bytearray)):
        raise RateError("ответ ЦБ пришёл без тела в байтах")

    return parse_cbr_xml(_decode(bytes(raw), _header_encoding(response)), char_code)


def _header_encoding(response) -> str | None:
    """Кодировка из заголовка ответа, если сервер её назвал."""
    # httpx: charset_encoding — это ровно charset из Content-Type, без выдумок,
    # тогда как encoding подставляет utf-8, когда сервер промолчал.
    encoding = getattr(response, "charset_encoding", _MISSING)
    if encoding is _MISSING:
        encoding = getattr(response, "encoding", None)
    return encoding if isinstance(encoding, str) and encoding.strip() else None


def _decode(raw: bytes, header_encoding: str | None) -> str:
    """Декодирует тело по объявленной кодировке: заголовок, пролог, запасная."""
    candidates: list[str] = []
    for encoding in (header_encoding, _declared_encoding(raw), FALLBACK_ENCODING):
        if encoding and encoding.lower() not in {known.lower() for known in candidates}:
            candidates.append(encoding)

    for encoding in candidates:
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            # Неизвестное имя кодировки или враньё в заголовке — пробуем дальше.
            continue

    raise RateError("ответ ЦБ не удалось раскодировать")


def _declared_encoding(raw: bytes) -> str | None:
    match = _DECLARED_ENCODING.match(raw[:200])
    return match.group(1).decode("ascii") if match else None


def _positive_number(raw: str | None, field: str) -> Decimal:
    """Число ЦБ: запятая как разделитель, строго конечное и больше нуля."""
    if raw is None:
        raise RateError(f"{field}: поле отсутствует")
    text = raw.strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not text:
        raise RateError(f"{field}: пустое значение")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise RateError(f"{field}: нечисловое значение {raw!r}") from exc
    # Decimal спокойно принимает NaN и Infinity — отсекаем их руками.
    if not number.is_finite() or number <= 0:
        raise RateError(f"{field}: недопустимое значение {raw!r}")
    return number


def _to_kopecks(value: Decimal, nominal: Decimal, char_code: str) -> int:
    """Курс за единицу в копейках, с округлением половины вверх."""
    try:
        # Умножаем до деления: так *100 остаётся точным, а округление одно.
        kopecks = (value * 100 / nominal).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ArithmeticError) as exc:
        raise RateError(f"{char_code}: курс не считается ({value}/{nominal})") from exc
    if kopecks <= 0:
        raise RateError(f"{char_code}: курс округлился в ноль копеек")
    return int(kopecks)
