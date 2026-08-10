"""Работа с состоянием диалога.

Промокод живёт в данных FSM и обязан переживать выход в главное меню: человек
вводит его в профиле, а покупает через несколько экранов. Обычный `state.clear()`
стирает и состояние, и данные — промокод молча пропадает, а покупатель видит
полную цену и считает, что его обманули.
"""

from __future__ import annotations

from aiogram.fsm.context import FSMContext

KEEP_KEYS = ("promo_code",)


async def soft_reset(state: FSMContext) -> None:
    """Сбрасывает шаг диалога, сохраняя то, что должно жить дольше."""
    data = await state.get_data()
    keep = {key: data[key] for key in KEEP_KEYS if key in data and data[key]}
    await state.clear()
    if keep:
        await state.update_data(**keep)
