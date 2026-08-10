"""Точка входа.

Порядок запуска выбран так, чтобы падать рано и громко: сначала проверяются
настройки, потом база, потом Redis. Бот, стартовавший с половиной окружения,
выглядит рабочим и ломается на первом покупателе.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis
from sqlalchemy import text as sql_text

from bot.config import get_settings
from bot.db.session import make_engine, make_session_factory, session_scope
from bot.handlers import build_router
from bot.logger import setup_logging
from bot.middlewares.context import ContextMiddleware
from bot.middlewares.gate import GateMiddleware
from bot.middlewares.throttle import ThrottleMiddleware
from bot.payments.registry import PaymentRegistry
from bot.repo import users as users_repo
from bot.scheduler import jobs
from bot.services import settings_store as settings_module
from bot.services import texts as texts_module
from bot.services.subscription import SubscriptionService

log = logging.getLogger(__name__)


async def _check_database(session_factory) -> None:
    async with session_scope(session_factory) as session:
        await session.execute(sql_text("SELECT 1"))


async def _connect_redis(settings) -> Redis | None:
    """Подключается к Redis. Без него бот работает, но хуже — и говорит об этом."""
    try:
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        await redis.ping()
        log.info("Redis подключён: %s:%s", settings.redis_host, settings.redis_port)
        return redis
    except Exception as exc:  # noqa: BLE001
        log.error(
            "Redis недоступен (%s). Состояние диалогов будет храниться в памяти "
            "и потеряется при перезапуске, кеш подписки и антиспам отключены.",
            exc,
        )
        return None


async def _seed(session_factory, owner_ids: list[int]) -> None:
    async with session_scope(session_factory) as session:
        added_texts = await texts_module.seed(session)
        added_settings = await settings_module.seed(session)
        await users_repo.ensure_owners(session, owner_ids)
    if added_texts or added_settings:
        log.info("Добавлено текстов: %s, настроек: %s", added_texts, added_settings)


async def main() -> int:
    settings = get_settings()
    setup_logging(settings.log_dir, settings.log_level)

    problems = settings.validate_runtime()
    if problems:
        for problem in problems:
            log.error("Настройка: %s", problem)
        print("\n✗ Бот не запущен. Исправьте .env:", file=sys.stderr)
        for problem in problems:
            print(f"  • {problem}", file=sys.stderr)
        return 2

    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    try:
        await _check_database(session_factory)
        log.info("База подключена: %s:%s/%s", settings.db_host, settings.db_port, settings.db_name)
    except Exception as exc:  # noqa: BLE001
        log.exception("База недоступна")
        print(f"\n✗ Бот не запущен: база недоступна ({exc})", file=sys.stderr)
        print("  Проверьте, что миграции накатаны: alembic upgrade head", file=sys.stderr)
        return 3

    redis = await _connect_redis(settings)
    storage = RedisStorage(redis) if redis is not None else MemoryStorage()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(storage=storage)

    registry = PaymentRegistry(settings)
    subscription = SubscriptionService(redis, settings.subscription_cache_seconds)

    await _seed(session_factory, settings.owner_ids)

    dispatcher.update.outer_middleware(ContextMiddleware(session_factory))
    dispatcher.message.middleware(ThrottleMiddleware(redis))
    dispatcher.callback_query.middleware(ThrottleMiddleware(redis))
    dispatcher.message.middleware(GateMiddleware(subscription))
    dispatcher.callback_query.middleware(GateMiddleware(subscription))

    dispatcher.include_router(build_router())

    dispatcher.workflow_data.update(
        settings=settings,
        registry=registry,
        subscription=subscription,
        session_factory=session_factory,
    )

    scheduler = jobs.setup(bot, session_factory, registry)
    webhook_runner = None

    if settings.webhook_enabled:
        from bot.payments import webhook as webhook_module

        app = webhook_module.build_app(settings, session_factory, registry)
        webhook_runner = await webhook_module.start(
            app, settings.webhook_host, settings.webhook_port
        )
        log.info("Адрес callback Platega: %s", settings.platega_callback_url())
        log.info("Адрес callback CryptoBot: %s", settings.cryptobot_callback_url())
    else:
        log.info(
            "Приём callback'ов выключен: статус платежей проверяется поллером "
            "и кнопкой «Проверить оплату»."
        )

    me = await bot.get_me()
    log.info("Бот запущен: @%s (id %s)", me.username, me.id)
    scheduler.start()

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        if webhook_runner is not None:
            await webhook_runner.cleanup()
        await registry.close()
        if redis is not None:
            await redis.aclose()
        await bot.session.close()
        await engine.dispose()
        log.info("Бот остановлен")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
