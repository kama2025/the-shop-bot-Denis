"""Фоновая сверка незавершённых платежей.

Нужен по двум причинам.

1. Без домена и HTTPS callback'и вообще не приходят — на разработке и на первом
   этапе прод-запуска поллер остаётся единственным автоматическим путём.
2. Даже с вебхуком callback может не дойти: у Platega три повтора с интервалом
   пять минут, и если в этот момент бот перезапускался, подтверждение потеряно
   навсегда. Покупатель заплатил, товара нет, и виноват «глючный бот».

Поллер не выдаёт товар сам — он вызывает ту же `confirm_order`, что и остальные
пути.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.base import utcnow
from bot.db.session import session_scope
from bot.payments.registry import PaymentRegistry
from bot.repo import orders as orders_repo
from bot.services import payments as payments_service

log = logging.getLogger(__name__)


async def sweep(
    session_factory: async_sessionmaker[AsyncSession],
    registry: PaymentRegistry,
    min_age_seconds: int = 45,
    limit: int = 50,
) -> list[payments_service.ConfirmResult]:
    """Проверяет заказы, ждущие оплаты дольше `min_age_seconds`.

    Задержка нужна, чтобы не гонять провайдера сразу после выставления счёта:
    покупатель ещё даже не открыл страницу оплаты.
    """
    if not registry.any_enabled:
        return []

    older_than = utcnow() - timedelta(seconds=min_age_seconds)
    async with session_scope(session_factory) as session:
        pending = await orders_repo.stale_pending(session, older_than, limit)
        order_ids = [order.id for order in pending]

    results: list[payments_service.ConfirmResult] = []
    for order_id in order_ids:
        try:
            async with session_scope(session_factory) as session:
                result = await payments_service.confirm_order(
                    session, registry, order_id, source="poller"
                )
            if result.outcome not in (
                payments_service.Outcome.PENDING,
                payments_service.Outcome.UNAVAILABLE,
            ):
                results.append(result)
        except Exception:  # noqa: BLE001 — один заказ не должен ронять весь обход
            log.exception("Ошибка сверки заказа %s", order_id)
        # Провайдера не долбим очередью запросов подряд.
        await asyncio.sleep(0.2)

    return results
