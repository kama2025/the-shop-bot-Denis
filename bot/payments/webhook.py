"""Приём callback'ов от провайдеров.

Общее правило для обоих: **callback не является подтверждением оплаты**. Он
только сообщает «по этой транзакции что-то произошло». Дальше вызывается
`confirm_order`, которая сама спрашивает провайдера о настоящем статусе.

Поэтому даже поддельный callback ничего не выдаёт: он лишь заставит бота
сделать лишний запрос к провайдеру, который ответит «PENDING».

Адрес содержит секретный кусок пути (`WEBHOOK_SECRET_PATH`) — чтобы адрес нельзя
было подобрать простым перебором и завалить бота проверками.
"""

from __future__ import annotations

import logging

from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.session import session_scope
from bot.logger import payment_log
from bot.payments.registry import PaymentRegistry
from bot.repo import orders as orders_repo
from bot.services import payments as payments_service

log = logging.getLogger(__name__)

# Ключ, под которым в приложении лежат зависимости.
DEPS = web.AppKey("deps", dict)


def build_app(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    registry: PaymentRegistry,
    on_result=None,
) -> web.Application:
    app = web.Application()
    app[DEPS] = {
        "settings": settings,
        "session_factory": session_factory,
        "registry": registry,
        "on_result": on_result,
    }

    prefix = f"/pay/{settings.webhook_secret_path}"
    app.router.add_post(f"{prefix}/platega", _platega_handler)
    app.router.add_post(f"{prefix}/cryptobot", _cryptobot_handler)
    app.router.add_get("/healthz", _health)
    return app


async def _health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _platega_handler(request: web.Request) -> web.Response:
    deps = request.app[DEPS]
    registry: PaymentRegistry = deps["registry"]
    provider = registry.platega()
    if provider is None:
        return web.json_response({"ok": False, "error": "platega disabled"}, status=503)

    if not provider.verify_callback_secret(
        request.headers.get("X-MerchantId"), request.headers.get("X-Secret")
    ):
        payment_log.warning(
            "Callback Platega с неверными заголовками",
            extra={"remote": request.remote},
        )
        return web.json_response({"ok": False}, status=403)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "bad json"}, status=400)

    txn_id = str(body.get("id") or "")
    payload = str(body.get("payload") or "")
    payment_log.info(
        "Callback Platega принят",
        extra={"txn_id": txn_id, "payload": payload, "status": body.get("status")},
    )

    order_id = await _resolve_order_id(deps["session_factory"], "platega", txn_id, payload)
    if order_id is None:
        # Отвечаем 200: заказ мог быть удалён, а повторы Platega ничего не
        # исправят и только зашумят журнал.
        payment_log.warning(
            "Callback Platega не сопоставлен с заказом",
            extra={"txn_id": txn_id, "payload": payload},
        )
        return web.json_response({"ok": True})

    await _confirm(deps, order_id)
    return web.json_response({"ok": True})


async def _cryptobot_handler(request: web.Request) -> web.Response:
    deps = request.app[DEPS]
    registry: PaymentRegistry = deps["registry"]
    provider = registry.cryptobot()
    if provider is None:
        return web.json_response({"ok": False, "error": "cryptobot disabled"}, status=503)

    raw = await request.read()
    if not provider.verify_signature(raw, request.headers.get("crypto-pay-api-signature")):
        payment_log.warning("Callback CryptoBot с неверной подписью", extra={"remote": request.remote})
        return web.json_response({"ok": False}, status=403)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "bad json"}, status=400)

    invoice = body.get("payload") or {}
    txn_id = str(invoice.get("invoice_id") or "")
    order_payload = str(invoice.get("payload") or "")
    payment_log.info(
        "Callback CryptoBot принят",
        extra={
            "update_type": body.get("update_type"),
            "txn_id": txn_id,
            "payload": order_payload,
        },
    )

    order_id = await _resolve_order_id(
        deps["session_factory"], "cryptobot", txn_id, order_payload
    )
    if order_id is None:
        payment_log.warning(
            "Callback CryptoBot не сопоставлен с заказом",
            extra={"txn_id": txn_id, "payload": order_payload},
        )
        return web.json_response({"ok": True})

    await _confirm(deps, order_id)
    return web.json_response({"ok": True})


async def _resolve_order_id(
    session_factory: async_sessionmaker[AsyncSession],
    provider: str,
    txn_id: str,
    payload: str,
) -> int | None:
    """Находит заказ по идентификатору транзакции, а если не вышло — по payload.

    Два пути нужны, потому что при повторных попытках провайдер иногда
    присылает не тот идентификатор, который мы сохранили, зато `payload` мы
    формировали сами.
    """
    async with session_scope(session_factory) as session:
        if txn_id:
            order = await orders_repo.by_provider_txn(session, provider, txn_id)
            if order is not None:
                return order.id
        if payload.isdigit():
            order = await orders_repo.get(session, int(payload))
            if order is not None:
                return order.id
    return None


async def _confirm(deps: dict, order_id: int) -> None:
    async with session_scope(deps["session_factory"]) as session:
        result = await payments_service.confirm_order(
            session, deps["registry"], order_id, source="callback"
        )
    on_result = deps.get("on_result")
    if on_result is not None:
        await on_result(result)


async def start(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("Webhook-сервер слушает %s:%s", host, port)
    return runner
