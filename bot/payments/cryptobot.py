"""Клиент Crypto Pay API (@CryptoBot).

Документация: https://help.send.tg/en/articles/10279948-crypto-pay-api

Счёт выставляется в рублях (`currency_type: fiat`, `fiat: RUB`) — покупатель
видит рублёвую цену, конвертацию в криптовалюту делает CryptoBot. Иначе курс
пришлось бы держать в магазине и обновлять вручную.

В отличие от Platega, вебхук здесь подписан HMAC-SHA256, и подпись проверяется
по-настоящему.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json as jsonlib
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

NAME = "cryptobot"

STATUS_MAP = {
    "active": PayStatus.PENDING,
    "paid": PayStatus.CONFIRMED,
    "expired": PayStatus.CANCELED,
}


class CryptoBotProvider:
    name = NAME

    def __init__(self, base_url: str, token: str, timeout_seconds: int = 20) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # --- вебхук ------------------------------------------------------------

    def verify_signature(self, body: bytes, signature: str | None) -> bool:
        """Проверяет подпись вебхука.

        Ключ — SHA-256 от токена приложения, подпись — HMAC-SHA-256 от тела
        запроса. Сравнение через `compare_digest`, а не `==`: обычное сравнение
        строк завершается на первом несовпавшем байте и утекает по времени.
        """
        if not signature:
            return False
        secret = hashlib.sha256(self._token.encode()).digest()
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    # --- интерфейс провайдера ---------------------------------------------

    def methods(self) -> list[PaymentMethod]:
        return [
            PaymentMethod(
                provider=NAME,
                code=f"{NAME}:crypto",
                title="CryptoBot",
                emoji="🪙",
                style="success",
            )
        ]

    async def create_invoice(
        self,
        order_id: int,
        amount_kop: int,
        description: str,
        method_code: str,
        user_id: int,
        username: str | None = None,
        expires_in: int = 1800,
    ) -> Invoice:
        body = {
            "currency_type": "fiat",
            "fiat": "RUB",
            "amount": f"{kop_to_units(amount_kop):.2f}",
            "description": description[:1024],
            "payload": str(order_id),
            "expires_in": expires_in,
            "allow_comments": False,
            "allow_anonymous": False,
        }
        data = await self._request("POST", "/createInvoice", json=body)
        result = data.get("result") or {}
        invoice_id = result.get("invoice_id")
        pay_url = (
            result.get("bot_invoice_url")
            or result.get("mini_app_invoice_url")
            or result.get("pay_url")
        )
        if not invoice_id or not pay_url:
            raise ProviderError(f"CryptoBot не вернул ссылку на оплату: {data}")
        return Invoice(txn_id=str(invoice_id), pay_url=str(pay_url), raw=result)

    async def fetch_status(self, txn_id: str) -> StatusResult:
        data = await self._request("GET", "/getInvoices", params={"invoice_ids": str(txn_id)})
        result = data.get("result") or {}
        items = result.get("items") if isinstance(result, dict) else None
        if not items:
            return StatusResult(status=PayStatus.UNKNOWN, raw=data)
        invoice = items[0]
        return self.parse_invoice(invoice)

    def parse_invoice(self, invoice: dict) -> StatusResult:
        raw_status = str(invoice.get("status", "")).lower()
        # Для фиатного счёта сумма лежит в `amount`, а поле `fiat` хранит валюту.
        amount = invoice.get("amount")
        currency = invoice.get("fiat") or invoice.get("asset")
        amount_kop: int | None = None
        if amount is not None and str(currency).upper() == "RUB":
            try:
                amount_kop = units_to_kop(amount)
            except Exception:  # noqa: BLE001 — сумма важна, но её отсутствие не фатально
                amount_kop = None
        return StatusResult(
            status=STATUS_MAP.get(raw_status, PayStatus.UNKNOWN),
            amount_kop=amount_kop,
            currency=str(currency) if currency else None,
            payload=invoice.get("payload"),
            raw=invoice,
        )

    # --- транспорт ---------------------------------------------------------

    async def _request(
        self, method: str, path: str, json: dict | None = None, params: dict | None = None
    ) -> dict:
        url = f"{self._base_url}{path}"
        headers = {"Crypto-Pay-API-Token": self._token, "Content-Type": "application/json"}
        session = await self._http()
        last_error: Exception | None = None

        for attempt in (1, 2):
            try:
                async with session.request(
                    method, url, json=json, params=params, headers=headers
                ) as response:
                    text = await response.text()
                    try:
                        data = jsonlib.loads(text)
                    except ValueError as exc:
                        raise ProviderError(
                            f"CryptoBot {path} → не JSON: {text[:300]}"
                        ) from exc
                    if not data.get("ok", False):
                        raise ProviderError(f"CryptoBot {path} → {data.get('error', data)}")
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                log.warning("CryptoBot недоступен, попытка %s: %s", attempt, exc)
                if attempt == 1:
                    await asyncio.sleep(1.5)

        raise ProviderError(f"CryptoBot {path} недоступен: {last_error}")
