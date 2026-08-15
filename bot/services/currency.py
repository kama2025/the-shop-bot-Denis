"""Какой курс действует прямо сейчас.

Разделение с `bot.services.rates` намеренное: там — как достать число из
внешнего мира, здесь — какое число считать действующим. Первое ходит в сеть и
ничего не знает о базе, второе не ходит в сеть вовсе.

Источник истины — таблица `exchange_rates`. Поверх неё короткий кеш в памяти:
курс ЦБ меняется раз в сутки, а карточка товара пересчитывается на каждом
показе, и бить в базу ради значения, которое заведомо то же самое, незачем.

Redis здесь не используется. Бот — один процесс, и общий кеш между процессами
ему не нужен; лишняя движущаяся часть на пути к цене — это лишний способ
однажды остаться без цены.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy.ext.asyncio import AsyncSession

from bot.repo import rates as rates_repo

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300


class RateStore:
    def __init__(self, ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
        self._rate_kop: int | None = None
        self._loaded_at = 0.0
        self._ttl = ttl_seconds

    def invalidate(self) -> None:
        self._loaded_at = 0.0

    async def get(self, session: AsyncSession, code: str = "USD") -> int | None:
        """Действующий курс в копейках за единицу валюты.

        `None` означает «курса нет вообще» — ни свежего, ни старого. Это не то
        же самое, что «курс устарел»: устаревший курс возвращается как есть.
        Один день расхождения с ЦБ дешевле, чем остановленный магазин.
        """
        if self._rate_kop is not None and (time.monotonic() - self._loaded_at) < self._ttl:
            return self._rate_kop

        row = await rates_repo.latest(session, code)
        if row is None:
            # Не кешируем отсутствие: как только планировщик принесёт курс,
            # продажи должны открыться сразу, а не через пять минут.
            return None

        self._rate_kop = int(row.rate_kop)
        self._loaded_at = time.monotonic()
        return self._rate_kop

    async def store(
        self, session: AsyncSession, rate_kop: int, code: str = "USD", source: str = "cbr"
    ) -> None:
        await rates_repo.add(session, rate_kop, code=code, source=source)
        self._rate_kop = int(rate_kop)
        self._loaded_at = time.monotonic()


rate_store = RateStore()


async def current_usd_kop(session: AsyncSession) -> int | None:
    return await rate_store.get(session, "USD")
