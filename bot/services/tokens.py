"""Токен заказа.

Токен — то, что покупатель называет вслух и переписывает руками. Поэтому
алфавит без символов, которые люди путают: нет нуля и буквы «O», нет единицы,
«I» и «L». Остаётся 31 символ.

Формат `XXXX-XXXX`: дефис посередине читается и диктуется заметно легче, чем
восемь символов подряд.

Восемь символов из 31 — это 31⁸ ≈ 8,5 × 10¹¹ вариантов. При десятках тысяч
заказов вероятность совпадения ничтожна, но не нулевая, поэтому уникальность
обеспечивается индексом в базе, а не расчётом на удачу.
"""

from __future__ import annotations

import secrets

ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
"""Без 0, O, 1, I, L — их путают при диктовке и при наборе."""

GROUP = 4
GROUPS = 2
LENGTH = GROUP * GROUPS


class TokenCollision(Exception):
    """Не удалось подобрать свободный токен."""


def generate() -> str:
    """Один случайный токен. Уникальность здесь не проверяется."""
    raw = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
    return "-".join(raw[i : i + GROUP] for i in range(0, LENGTH, GROUP))


def is_valid(token: str) -> bool:
    """Проверка формы — для поиска заказа по введённому вручную токену."""
    parts = token.strip().upper().split("-")
    if len(parts) != GROUPS:
        return False
    return all(
        len(part) == GROUP and all(char in ALPHABET for char in part) for part in parts
    )


def normalize(token: str) -> str:
    """Приводит введённое человеком к каноническому виду.

    Люди набирают в нижнем регистре, забывают дефис или ставят пробел вместо
    него. Отказывать из-за этого — злить покупателя на ровном месте.
    """
    raw = token.strip().upper().replace("-", "").replace(" ", "")
    if len(raw) != LENGTH:
        return token.strip().upper()
    return "-".join(raw[i : i + GROUP] for i in range(0, LENGTH, GROUP))


async def generate_unique(exists, attempts: int = 5) -> str:
    """Подбирает токен, которого ещё нет.

    `exists` — асинхронная функция `(token) -> bool`.

    Если за отведённые попытки свободный токен не нашёлся, возбуждается
    исключение. Отдать заведомо занятый токен нельзя: по нему администратор
    ищет заказ, и два заказа с одним номером означают, что работу сделают
    не тому.
    """
    for _ in range(attempts):
        candidate = generate()
        if not await exists(candidate):
            return candidate
    raise TokenCollision(f"Не удалось подобрать свободный токен за {attempts} попыток")
