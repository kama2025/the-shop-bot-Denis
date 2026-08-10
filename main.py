#!/usr/bin/env python3
"""Запуск бота.

    python main.py

Ровно то же самое делает `python -m bot` — это один и тот же код, а не второй
способ запуска. Здесь только делегирование: файл в корне привычнее, а вся
логика старта живёт в `bot/__main__.py`.

Второй точки входа со своей логикой быть не должно. Две копии запуска
неизбежно разъезжаются, и однажды прод поднимается той, в которой забыли
что-нибудь важное — например, синхронизацию меню команд или проверку базы.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Чтобы `python main.py` работал из любого каталога, а не только из корня.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot.__main__ import main  # noqa: E402

if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nОстановлено с клавиатуры.")
