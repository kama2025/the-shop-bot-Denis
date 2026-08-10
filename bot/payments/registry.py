"""Набор включённых провайдеров.

Провайдер, у которого нет ключей, не включается. Показать покупателю кнопку
оплаты, которая заведомо не сработает, хуже, чем не показать её вовсе: кнопка,
после которой ничего не меняется, читается как поломка.
"""

from __future__ import annotations

import logging

from bot.config import Settings
from bot.payments.base import PaymentMethod, PaymentProvider
from bot.payments.cryptobot import CryptoBotProvider
from bot.payments.platega import PlategaProvider

log = logging.getLogger(__name__)

BALANCE_METHOD = PaymentMethod(
    provider="balance",
    code="balance",
    title="С баланса",
    emoji="💼",
    style="primary",
)


class PaymentRegistry:
    def __init__(self, settings: Settings) -> None:
        self._providers: dict[str, PaymentProvider] = {}
        self._settings = settings

        if settings.platega_enabled and settings.platega_merchant_id and settings.platega_secret:
            self._providers["platega"] = PlategaProvider(
                base_url=settings.platega_base_url,
                merchant_id=settings.platega_merchant_id,
                secret=settings.platega_secret,
                method_codes=settings.platega_methods,
                return_url=settings.platega_return_url,
                failed_url=settings.platega_failed_url,
            )
            log.info("Platega включена, методы: %s", settings.platega_methods)
        else:
            log.info("Platega выключена")

        if settings.cryptobot_enabled and settings.cryptobot_token:
            self._providers["cryptobot"] = CryptoBotProvider(
                base_url=settings.cryptobot_base_url,
                token=settings.cryptobot_token,
            )
            log.info("CryptoBot включён")
        else:
            log.info("CryptoBot выключен")

    def get(self, name: str) -> PaymentProvider | None:
        return self._providers.get(name)

    def platega(self) -> PlategaProvider | None:
        provider = self._providers.get("platega")
        return provider if isinstance(provider, PlategaProvider) else None

    def cryptobot(self) -> CryptoBotProvider | None:
        provider = self._providers.get("cryptobot")
        return provider if isinstance(provider, CryptoBotProvider) else None

    @property
    def enabled_names(self) -> list[str]:
        return list(self._providers)

    @property
    def any_enabled(self) -> bool:
        return bool(self._providers)

    def methods(self) -> list[PaymentMethod]:
        result: list[PaymentMethod] = []
        for provider in self._providers.values():
            result.extend(provider.methods())
        return result

    def provider_of(self, method_code: str) -> PaymentProvider | None:
        return self._providers.get(method_code.split(":", 1)[0])

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()
