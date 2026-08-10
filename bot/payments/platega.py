"""Клиент Platega.

Документация: https://docs.platega.io/

Важное про безопасность: callback от Platega «аутентифицируется» тем, что она
присылает нам наш же `X-Secret` в заголовке. Криптографической подписи нет,
то есть знание секрета = возможность подделать подтверждение. Поэтому callback
в этом проекте служит только сигналом «сходи проверь», а решение принимается
по ответу `GET /transaction/{id}`.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from bot.payments.base import (
    Invoice,
    PaymentMethod,
    PayStatus,
    ProviderError,
    StatusResult,
    kop_to_units,
    units_to_kop,
)

log = logging.getLogger(__name__)

NAME = "platega"

# Коды из документации Platega.
METHOD_TITLES: dict[int, tuple[str, str]] = {
    2: ("СБП", "🏦"),
    3: ("ЕРИП", "🏛"),
    11: ("Банковская карта", "💳"),
    12: ("Международная карта", "🌍"),
    13: ("Криптовалюта", "🪙"),
    14: ("SberPay", "🟢"),
}

STATUS_MAP = {
    "PENDING": PayStatus.PENDING,
    "CONFIRMED": PayStatus.CONFIRMED,
    "CANCELED": PayStatus.CANCELED,
    "CANCELLED": PayStatus.CANCELED,
    "CHARGEBACKED": PayStatus.CHARGEBACKED,
}


class PlategaProvider:
    name = NAME

    def __init__(
        self,
        base_url: str,
        merchant_id: str,
        secret: str,
        method_codes: list[int],
        return_url: str,
        failed_url: str,
        timeout_seconds: int = 20,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._merchant_id = merchant_id
        self._secret = secret
        self._method_codes = method_codes
        self._return_url = return_url
        self._failed_url = failed_url
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None

    # --- служебное ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "X-MerchantId": self._merchant_id,
            "X-Secret": self._secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def verify_callback_secret(self, merchant_id: str | None, secret: str | None) -> bool:
        """Сверяет заголовки callback'а.

        Слабая проверка — это всё, что даёт Platega. Она отсекает случайный шум,
        но не заменяет запрос настоящего статуса.
        """
        import hmac

        if not merchant_id or not secret:
            return False
        return hmac.compare_digest(merchant_id, self._merchant_id) and hmac.compare_digest(
            secret, self._secret
        )

    # --- интерфейс провайдера ---------------------------------------------

    def methods(self) -> list[PaymentMethod]:
        result: list[PaymentMethod] = []
        for code in self._method_codes:
            title, emoji = METHOD_TITLES.get(code, (f"Способ {code}", "💳"))
            result.append(
                PaymentMethod(
                    provider=NAME,
                    code=f"{NAME}:{code}",
                    title=title,
                    emoji=emoji,
                    style="success",
                )
            )
        return result

    async def create_invoice(
        self,
        order_id: int,
        amount_kop: int,
        description: str,
        method_code: str,
        user_id: int,
        username: str | None = None,
    ) -> Invoice:
        try:
            payment_method = int(method_code.split(":", 1)[1])
        except (IndexError, ValueError) as exc:
            raise ProviderError(f"Некорректный код способа оплаты: {method_code!r}") from exc

        body = {
            "paymentMethod": payment_method,
            "paymentDetails": {"amount": kop_to_units(amount_kop), "currency": "RUB"},
            "description": description[:255],
            "return": self._return_url,
            "failedUrl": self._failed_url,
            # Свой номер заказа кладём в payload: по нему callback находит заказ,
            # даже если идентификатор транзакции почему-то разошёлся.
            "payload": str(order_id),
            "metadata": {"userId": str(user_id), "userName": username or ""},
        }

        data = await self._request("POST", "/transaction/process", json=body)
        txn_id = data.get("transactionId") or data.get("id")
        pay_url = data.get("redirect") or data.get("payformUrl")
        if not txn_id or not pay_url:
            raise ProviderError(f"Platega не вернула ссылку на оплату: {data}")
        return Invoice(txn_id=str(txn_id), pay_url=str(pay_url), raw=data)

    async def fetch_status(self, txn_id: str) -> StatusResult:
        data = await self._request("GET", f"/transaction/{txn_id}")
        raw_status = str(data.get("status", "")).upper()
        details = data.get("paymentDetails") or {}
        amount = details.get("amount") if isinstance(details, dict) else None
        return StatusResult(
            status=STATUS_MAP.get(raw_status, PayStatus.UNKNOWN),
            amount_kop=units_to_kop(amount) if amount is not None else None,
            currency=(details.get("currency") if isinstance(details, dict) else None),
            payload=data.get("payload"),
            raw=data,
        )

    # --- транспорт ---------------------------------------------------------

    async def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        url = f"{self._base_url}{path}"
        session = await self._http()
        last_error: Exception | None = None

        # Две попытки: сетевая икота не должна отправлять покупателя обратно
        # в меню, но и висеть бесконечно нельзя.
        for attempt in (1, 2):
            try:
                async with session.request(
                    method, url, json=json, headers=self._headers()
                ) as response:
                    text = await response.text()
                    if response.status >= 400:
                        raise ProviderError(
                            f"Platega {method} {path} → HTTP {response.status}: {text[:300]}"
                        )
                    if not text.strip():
                        raise ProviderError(f"Platega {method} {path} → пустой ответ")
                    try:
                        return await response.json(content_type=None)
                    except Exception as exc:
                        raise ProviderError(
                            f"Platega {method} {path} → не JSON: {text[:300]}"
                        ) from exc
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                log.warning(
                    "Platega недоступна, попытка %s: %s", attempt, exc, extra={"path": path}
                )
                if attempt == 1:
                    await asyncio.sleep(1.5)

        raise ProviderError(f"Platega {method} {path} недоступна: {last_error}")
