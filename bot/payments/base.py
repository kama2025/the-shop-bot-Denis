"""Общий интерфейс платёжного провайдера.

Провайдеры отличаются всем — адресами, форматом, способом подтверждения, — но
магазину от них нужно ровно три вещи: список способов, выставить счёт, узнать
настоящий статус. Всё остальное прячется за этим интерфейсом, поэтому третий
провайдер (например Telegram Stars) добавляется файлом, а не правкой заказов.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class PayStatus:
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELED = "canceled"
    CHARGEBACKED = "chargebacked"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PaymentMethod:
    """Способ оплаты, как его видит покупатель."""

    provider: str
    code: str          # полный код, например "platega:2"
    title: str
    style: str = "success"
    emoji: str = "💳"


@dataclass(frozen=True)
class Invoice:
    txn_id: str
    pay_url: str
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StatusResult:
    status: str
    amount_kop: int | None = None
    currency: str | None = None
    payload: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def is_confirmed(self) -> bool:
        return self.status == PayStatus.CONFIRMED

    @property
    def is_final_failure(self) -> bool:
        return self.status in (PayStatus.CANCELED, PayStatus.CHARGEBACKED)


class ProviderError(Exception):
    """Провайдер ответил ошибкой или не ответил."""


class PaymentProvider(Protocol):
    name: str

    def methods(self) -> list[PaymentMethod]: ...

    async def create_invoice(
        self,
        order_id: int,
        amount_kop: int,
        description: str,
        method_code: str,
        user_id: int,
        username: str | None = None,
    ) -> Invoice: ...

    async def fetch_status(self, txn_id: str) -> StatusResult: ...

    async def close(self) -> None: ...


def kop_to_units(amount_kop: int) -> float:
    """Копейки → рубли для внешнего API.

    Возвращается float, потому что этого требуют оба API. Внутри магазина float
    для денег не используется — только на границе.
    """
    return round(int(amount_kop) / 100, 2)


def units_to_kop(amount: float | str | int) -> int:
    from decimal import Decimal

    return int((Decimal(str(amount)) * 100).quantize(Decimal("1")))
