"""Фоновые задания.

Три задания.

* `expire_orders` — закрывает счета, по которым не заплатили. Оплаченные заказы
  сюда не попадают: деньги, за которые работа не сделана, по таймауту не сгорают.
* `poll_payments` — досверяет платежи. Callback может не дойти; покупатель,
  оплативший в момент перезапуска бота, иначе останется без заказа.
* `refresh_rate` — тянет курс ЦБ. Без него магазин работает на последнем
  известном курсе, а на первом запуске не работает вовсе.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.session import session_scope
from bot.payments import poller
from bot.payments.registry import PaymentRegistry
from bot.services import currency as currency_service
from bot.services import orders as orders_service
from bot.services import payments as payments_service
from bot.services import rates as rates_service
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
        if not result.accepted_now or result.order is None:
            continue
        # Поллер нашёл оплату раньше покупателя — просим реквизиты сами.
        async with session_scope(session_factory) as session:
            text = await text_service.get(
                session,
                "ask_credentials",
                order_id=result.order.id,
                title=result.order.product_title,
                token=result.order.token or "",
            )
        try:
            await bot.send_message(result.order.user_id, text)
        except TelegramAPIError as exc:
            log.warning("Не смогли попросить реквизиты: %s", exc)


async def refresh_rate(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Обновляет курс ЦБ.

    Неудача не считается аварией: в базе остаётся прошлое значение, и магазин
    продолжает работать на нём. Аварией было бы продавать по выдуманному курсу.
    """
    import aiohttp

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as client:
            rate_kop = await rates_service.fetch_cbr(client)
    except rates_service.RateError as exc:
        log.warning("Курс ЦБ не обновлён: %s", exc)
        return
    except Exception as exc:  # noqa: BLE001 — задание не должно ронять планировщик
        log.warning("Курс ЦБ не обновлён, неожиданная ошибка: %s", exc)
        return

    async with session_scope(session_factory) as session:
        await currency_service.rate_store.store(session, rate_kop)
    log.info("Курс ЦБ обновлён: %s коп. за $1", rate_kop)


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
    scheduler.add_job(
        refresh_rate,
        "interval",
        hours=1,
        args=(session_factory,),
        id="refresh_rate",
        max_instances=1,
        coalesce=True,
        # Первый запуск — сразу: без курса магазин не продаёт, и ждать час
        # после старта нельзя.
        next_run_time=datetime.now(timezone.utc),
    )
    return scheduler
