"""Настройки бота.

Всё читается из окружения. В коде нет ни одного секрета — только имена
переменных и значения по умолчанию, которые безопасно видеть посторонним.

Настройки, которые владелец меняет сам (тексты, картинка, тайминги показа),
живут не здесь, а в базе — см. `bot.services.settings`. Здесь только то, без
чего процесс не стартует.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


def _parse_int_list(raw: str | list[int] | None) -> list[int]:
    """Разбирает "1, 2,3" в [1, 2, 3]. Пустые куски игнорирует."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [int(x) for x in raw]
    return [int(part) for part in str(raw).replace(";", ",").split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram ----------------------------------------------------------
    bot_token: str = Field(default="", alias="BOT_TOKEN")
    owner_ids_raw: str = Field(default="", alias="OWNER_IDS")

    # --- База --------------------------------------------------------------
    db_host: str = Field(default="127.0.0.1", alias="DB_HOST")
    db_port: int = Field(default=3307, alias="DB_PORT")
    db_user: str = Field(default="shopbot", alias="DB_USER")
    db_password: str = Field(default="", alias="DB_PASSWORD")
    db_name: str = Field(default="shopbot", alias="DB_NAME")

    # --- Redis -------------------------------------------------------------
    redis_host: str = Field(default="127.0.0.1", alias="REDIS_HOST")
    redis_port: int = Field(default=6380, alias="REDIS_PORT")
    redis_db: int = Field(default=0, alias="REDIS_DB")
    redis_password: str = Field(default="", alias="REDIS_PASSWORD")

    # --- Platega -----------------------------------------------------------
    platega_enabled: bool = Field(default=False, alias="PLATEGA_ENABLED")
    platega_base_url: str = Field(default="https://app.platega.io", alias="PLATEGA_BASE_URL")
    platega_merchant_id: str = Field(default="", alias="PLATEGA_MERCHANT_ID")
    platega_secret: str = Field(default="", alias="PLATEGA_SECRET")
    platega_methods_raw: str = Field(default="2,11", alias="PLATEGA_METHODS")
    platega_return_url: str = Field(default="https://t.me", alias="PLATEGA_RETURN_URL")
    platega_failed_url: str = Field(default="https://t.me", alias="PLATEGA_FAILED_URL")

    # --- kassa.ai ----------------------------------------------------------
    # Провайдер, на который магазин переходит с Platega. Пока у сервиса нет
    # публичной документации API, интеграция не заполнена — см. bot/payments/kassa.py.
    kassa_enabled: bool = Field(default=False, alias="KASSA_ENABLED")
    kassa_base_url: str = Field(default="https://kassa.ai", alias="KASSA_BASE_URL")
    kassa_merchant_id: str = Field(default="", alias="KASSA_MERCHANT_ID")
    kassa_secret: str = Field(default="", alias="KASSA_SECRET")
    kassa_return_url: str = Field(default="https://t.me", alias="KASSA_RETURN_URL")
    kassa_failed_url: str = Field(default="https://t.me", alias="KASSA_FAILED_URL")

    # --- CryptoBot ---------------------------------------------------------
    cryptobot_enabled: bool = Field(default=False, alias="CRYPTOBOT_ENABLED")
    cryptobot_base_url: str = Field(default="https://pay.crypt.bot/api", alias="CRYPTOBOT_BASE_URL")
    cryptobot_token: str = Field(default="", alias="CRYPTOBOT_TOKEN")

    # --- Приём callback'ов -------------------------------------------------
    webhook_enabled: bool = Field(default=False, alias="WEBHOOK_ENABLED")
    webhook_host: str = Field(default="0.0.0.0", alias="WEBHOOK_HOST")
    webhook_port: int = Field(default=8080, alias="WEBHOOK_PORT")
    webhook_public_url: str = Field(default="", alias="WEBHOOK_PUBLIC_URL")
    webhook_secret_path: str = Field(default="", alias="WEBHOOK_SECRET_PATH")

    # --- Правила магазина --------------------------------------------------
    order_reserve_minutes: int = Field(default=20, alias="ORDER_RESERVE_MINUTES")
    max_qty_per_order: int = Field(default=10, alias="MAX_QTY_PER_ORDER")
    subscription_cache_seconds: int = Field(default=300, alias="SUBSCRIPTION_CACHE_SECONDS")
    broadcast_rate: int = Field(default=20, alias="BROADCAST_RATE")

    # --- Логи --------------------------------------------------------------
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_dir: str = Field(default="logs", alias="LOG_DIR")

    @field_validator("platega_base_url", "cryptobot_base_url", "webhook_public_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    # --- Производные -------------------------------------------------------

    @property
    def owner_ids(self) -> list[int]:
        return _parse_int_list(self.owner_ids_raw)

    @property
    def platega_methods(self) -> list[int]:
        return _parse_int_list(self.platega_methods_raw)

    @property
    def database_url(self) -> str:
        from urllib.parse import quote_plus

        password = quote_plus(self.db_password)
        return (
            f"mysql+aiomysql://{self.db_user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )

    @property
    def sync_database_url(self) -> str:
        """Синхронный адрес — нужен Alembic и скриптам проверки схемы."""
        from urllib.parse import quote_plus

        password = quote_plus(self.db_password)
        return (
            f"mysql+pymysql://{self.db_user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )

    @property
    def redis_url(self) -> str:
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def media_dir(self) -> Path:
        return BASE_DIR / "media"

    @property
    def export_dir(self) -> Path:
        return BASE_DIR / "exports"

    def platega_callback_url(self) -> str:
        return f"{self.webhook_public_url}/pay/{self.webhook_secret_path}/platega"

    def cryptobot_callback_url(self) -> str:
        return f"{self.webhook_public_url}/pay/{self.webhook_secret_path}/cryptobot"

    def kassa_callback_url(self) -> str:
        return f"{self.webhook_public_url}/pay/{self.webhook_secret_path}/kassa"

    def validate_runtime(self) -> list[str]:
        """Проверяет настройки перед стартом.

        Возвращает список проблем. Пустой список — можно стартовать.
        Молча стартовать со сломанной конфигурацией нельзя: бот поднимется,
        а платежи будут падать в проде, и никто не поймёт почему.
        """
        problems: list[str] = []

        if not self.bot_token or "PLACEHOLDER" in self.bot_token:
            problems.append("BOT_TOKEN не задан")
        if not self.owner_ids:
            problems.append("OWNER_IDS пуст — в админку не сможет войти никто")
        if not self.db_password:
            problems.append("DB_PASSWORD не задан")

        if self.platega_enabled:
            if not self.platega_merchant_id or "PLACEHOLDER" in self.platega_merchant_id:
                problems.append("PLATEGA_ENABLED=true, но PLATEGA_MERCHANT_ID не задан")
            if not self.platega_secret or "PLACEHOLDER" in self.platega_secret:
                problems.append("PLATEGA_ENABLED=true, но PLATEGA_SECRET не задан")
            if not self.platega_methods:
                problems.append("PLATEGA_ENABLED=true, но PLATEGA_METHODS пуст")

        if self.kassa_enabled:
            # Отказ намеренно жёсткий и без обходного пути. Включённый провайдер,
            # у которого не заполнены методы, — это магазин, где кнопка оплаты
            # есть, а оплаты нет. Пусть лучше бот не стартует у нас, чем
            # сломается у первого покупателя.
            problems.append(
                "KASSA_ENABLED=true, но интеграция kassa.ai не реализована: "
                "у сервиса нет публичной документации API. Запросите её у "
                "заказчика и заполните bot/payments/kassa.py, а пока держите "
                "KASSA_ENABLED=false"
            )

        if self.cryptobot_enabled and (
            not self.cryptobot_token or "PLACEHOLDER" in self.cryptobot_token
        ):
            problems.append("CRYPTOBOT_ENABLED=true, но CRYPTOBOT_TOKEN не задан")

        if self.webhook_enabled:
            if not self.webhook_public_url:
                problems.append("WEBHOOK_ENABLED=true, но WEBHOOK_PUBLIC_URL не задан")
            elif not self.webhook_public_url.startswith("https://"):
                problems.append(
                    "WEBHOOK_PUBLIC_URL должен начинаться с https:// — "
                    "Platega не отправляет callback на http"
                )
            if not self.webhook_secret_path or "PLACEHOLDER" in self.webhook_secret_path:
                problems.append("WEBHOOK_ENABLED=true, но WEBHOOK_SECRET_PATH не задан")

        if self.max_qty_per_order < 1:
            problems.append("MAX_QTY_PER_ORDER должен быть не меньше 1")
        if self.order_reserve_minutes < 1:
            problems.append("ORDER_RESERVE_MINUTES должен быть не меньше 1")
        if self.broadcast_rate < 1:
            problems.append("BROADCAST_RATE должен быть не меньше 1")

        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
