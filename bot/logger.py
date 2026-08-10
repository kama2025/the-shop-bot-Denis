"""Логирование.

Два потока:

* общий — в консоль и `logs/bot.log`;
* платёжный — отдельным файлом `logs/payments.log`, чтобы разбор спорного
  платежа не приходилось выкапывать из общего шума.

Формат — JSON: строка лога должна быть машиночитаемой, иначе при разборе
инцидента её начинают читать глазами и обрезают самое важное.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path
from typing import Any

_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(log_dir: str | Path = "logs", level: str = "INFO") -> None:
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)

    formatter = JsonFormatter()

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    general = logging.handlers.RotatingFileHandler(
        directory / "bot.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    general.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(console)
    root.addHandler(general)

    payments = logging.getLogger("payments")
    payments.handlers.clear()
    payment_file = logging.handlers.RotatingFileHandler(
        directory / "payments.log", maxBytes=10 * 1024 * 1024, backupCount=20, encoding="utf-8"
    )
    payment_file.setFormatter(formatter)
    payments.addHandler(payment_file)
    payments.setLevel(logging.INFO)
    # propagate=True оставлен намеренно: платёжные события должны быть видны
    # и в общем журнале тоже, иначе при разборе инцидента теряется хронология.

    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiomysql").setLevel(logging.WARNING)


payment_log = logging.getLogger("payments")
