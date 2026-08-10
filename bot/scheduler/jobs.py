"""Фоновые задания.

Два задания, оба обязательные.

* `expire_orders` — освобождает просроченные резервы. Без него позиции склада
  зависают в резерве навсегда, остаток тает, а товар «есть, но не продаётся».
* `poll_payments` — досверяет платежи. Callback может не дойти; покупатель,
  оплативший в момент перезапуска бота, иначе останется без товара.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.session import session_scope
from bot.payments import poller
from bot.payments.registry import PaymentRegistry
from bot.services import delivery as delivery_service
from bot.services import dispatch as dispatch_service
from bot.services import orders as orders_service
from bot.services import payments as payments_service
from bot.services.texts import text_service

log = logging.getLogger(__name__)


async def expire_orders(
    bot: Bot, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_scope(session_factory) as session:
        expired = await orders_service.expire_stale(session)
        if not expired:
            return
        text = await text_service.get(session, "payment_expired")
        rows = [(order_id,) for order_id in expired]

    log.info("Истекло заказов: %s", len(rows))
    async with session_scope(session_factory) as session:
        from bot.repo import orders as orders_repo

        for (order_id,) in rows:
            order = await orders_repo.get(session, order_id)
            if order is None:
                continue
            try:
                await bot.send_message(order.user_id, f"{text}\n\nЗаказ #{order.id}")
            except TelegramAPIError:
                pass


async def poll_payments(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    registry: PaymentRegistry,
) -> None:
    results = await poller.sweep(session_factory, registry)
    for result in results:
        if result.outcome != payments_service.Outcome.DELIVERED or result.order is None:
            continue
        # Поллер нашёл оплату раньше покупателя — сообщаем сами.
        async with session_scope(session_factory) as session:
            text = await text_service.get(
                session,
                "delivery",
                order_id=result.order.id,
                title=result.order.product_title,
                qty=result.order.qty,
                items=delivery_service.format_items(result.items),
            )
        try:
            await bot.send_message(result.order.user_id, text)
            await dispatch_service.send_items(bot, result.order.user_id, result.items)
        except TelegramAPIError as exc:
            log.warning("Не доставили товар в чат: %s", exc)


def setup(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    registry: PaymentRegistry,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        expire_orders,
        "interval",
        minutes=1,
        args=(bot, session_factory),
        id="expire_orders",
        max_instances=1,
        coalesce=True,
    )
    if registry.any_enabled:
        scheduler.add_job(
            poll_payments,
            "interval",
            seconds=45,
            args=(bot, session_factory, registry),
            id="poll_payments",
            max_instances=1,
            coalesce=True,
        )
    return scheduler
