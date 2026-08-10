"""Правила промокодов.

Разделение намеренное: `validate` только считает и ничего не меняет, `consume`
только списывает. Проверка при показе цены и списание при оплате — разные
моменты времени, и между ними промокод может кончиться.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import utcnow
from bot.db.models import Product, PromoCode
from bot.repo import promo as promo_repo
from bot.utils.money import DISCOUNT_FIXED, DISCOUNT_PERCENT, format_kop

# Причины отказа совпадают с ключами текстов — чтобы хендлер не переводил
# коды в сообщения вручную и не забыл ни одну ветку.
REASON_NOT_FOUND = "promo_invalid"
REASON_INACTIVE = "promo_invalid"
REASON_EXPIRED = "promo_expired"
REASON_EXHAUSTED = "promo_exhausted"
REASON_USER_LIMIT = "promo_already_used"
REASON_MIN_ORDER = "promo_min_order"
REASON_SCOPE = "promo_wrong_scope"


@dataclass(frozen=True)
class PromoCheck:
    ok: bool
    promo: PromoCode | None = None
    reason: str | None = None
    min_order_kop: int = 0

    @property
    def promo_id(self) -> int | None:
        return self.promo.id if self.promo else None


def describe_discount(promo: PromoCode) -> str:
    if promo.discount_type == DISCOUNT_PERCENT:
        return f"{promo.discount_value}%"
    return format_kop(int(promo.discount_value))


async def scope_allows(session: AsyncSession, promo: PromoCode, product: Product) -> bool:
    """Пустая область действия означает «весь магазин»."""
    scopes = await promo_repo.scopes_of(session, promo.id)
    if not scopes:
        return True
    for scope in scopes:
        if scope.product_id is not None and scope.product_id == product.id:
            return True
        if scope.category_id is not None and scope.category_id == product.category_id:
            return True
    return False


async def validate(
    session: AsyncSession,
    code: str,
    user_id: int,
    product: Product | None = None,
    subtotal_kop: int | None = None,
) -> PromoCheck:
    promo = await promo_repo.get_by_code(session, code)
    if promo is None:
        return PromoCheck(False, reason=REASON_NOT_FOUND)
    if not promo.is_active:
        return PromoCheck(False, promo=promo, reason=REASON_INACTIVE)

    now = utcnow()
    if promo.valid_from and now < promo.valid_from:
        return PromoCheck(False, promo=promo, reason=REASON_EXPIRED)
    if promo.valid_until and now > promo.valid_until:
        return PromoCheck(False, promo=promo, reason=REASON_EXPIRED)

    if promo.usage_limit is not None and promo.used_count >= promo.usage_limit:
        return PromoCheck(False, promo=promo, reason=REASON_EXHAUSTED)

    if promo.per_user_limit is not None:
        used = await promo_repo.uses_by_user(session, promo.id, user_id)
        if used >= promo.per_user_limit:
            return PromoCheck(False, promo=promo, reason=REASON_USER_LIMIT)

    if subtotal_kop is not None and subtotal_kop < promo.min_order_kop:
        return PromoCheck(
            False, promo=promo, reason=REASON_MIN_ORDER, min_order_kop=promo.min_order_kop
        )

    if product is not None and not await scope_allows(session, promo, product):
        return PromoCheck(False, promo=promo, reason=REASON_SCOPE)

    return PromoCheck(True, promo=promo)


def discount_parts(promo: PromoCode) -> tuple[str, int]:
    return promo.discount_type, int(promo.discount_value)


async def consume(session: AsyncSession, promo_id: int, user_id: int, order_id: int) -> bool:
    """Списывает использование. Возвращает False, если промокод уже кончился."""
    return await promo_repo.consume(session, promo_id, user_id, order_id)


async def release(session: AsyncSession, promo_id: int, order_id: int) -> None:
    await promo_repo.release(session, promo_id, order_id)


def is_valid_type(value: str) -> bool:
    return value in (DISCOUNT_PERCENT, DISCOUNT_FIXED)
