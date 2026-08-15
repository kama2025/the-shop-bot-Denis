"""Выкладка товара — мастер из шести шагов.

Товар создаётся **только на последнем шаге**. Всё, что администратор ввёл до
этого, живёт в состоянии FSM и исчезает вместе с ним. Причина простая: товар,
созданный после третьего шага и брошенный на четвёртом, попадает в каталог без
цены или без картинки — и его увидит покупатель.

Редактирование существующего товара мастером не идёт: там правится одно поле,
и гнать человека через шесть экранов ради новой цены незачем.
"""

from __future__ import annotations

import html
import logging
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.admin.common import guard
from bot.keyboards import admin as admin_kb
from bot.repo import audit as audit_repo
from bot.repo import catalog as catalog_repo
from bot.services import pricing
from bot.services.access import Actor
from bot.states.admin import ProductWizardSG
from bot.utils.fsm import soft_reset
from bot.utils.render import show

log = logging.getLogger(__name__)

router = Router(name="admin.product_wizard")

MAX_TITLE = 255
MAX_PRICE_USD = Decimal("100000")


def parse_price_usd(raw: str) -> int:
    """Переводит введённое человеком в центы.

    Принимает `20`, `19.99`, `19,99` и суммы с пробелами внутри — люди пишут
    цену как придётся. Не принимает ноль и отрицательное: бесплатный товар в
    магазине, который берёт наценку процентом, лишён смысла и ломает расчёт.
    """
    cleaned = raw.strip().replace(" ", "").replace(" ", "").replace(",", ".").lstrip("$")
    if not cleaned:
        raise ValueError("пустая цена")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("не похоже на число") from exc
    # `Decimal` разбирает "nan" и "inf" как совершенно законные значения, а вот
    # сравнение NaN с нулём возбуждает `InvalidOperation` — не `ValueError`,
    # который ловит хендлер. Мастер падал бы с необработанным исключением прямо
    # на шаге цены. Отсекаем до первого сравнения.
    if not value.is_finite():
        raise ValueError("не похоже на число")

    if value <= 0:
        raise ValueError("цена должна быть больше нуля")
    if value > MAX_PRICE_USD:
        raise ValueError(f"слишком много, максимум ${MAX_PRICE_USD:,.0f}")
    cents = int((value * 100).quantize(Decimal("1")))
    if cents <= 0:
        raise ValueError("цена должна быть больше нуля")
    return cents


# --- шаг 1: категория -------------------------------------------------------


@router.callback_query(F.data.startswith("a:prod_add:"))
async def start_wizard(
    call: CallbackQuery, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(call, actor):
        return
    category_id = int(call.data.split(":")[2])
    category = await catalog_repo.get_category(session, category_id)
    if category is None:
        await call.answer("Категория не найдена", show_alert=True)
        return

    await soft_reset(state)
    await state.set_state(ProductWizardSG.title)
    await state.update_data(wizard_category_id=category_id)
    await call.answer()
    await show(
        call,
        f"🆕 <b>Выкладка товара</b>\n"
        f"Категория: {html.escape(category.title)}\n\n"
        f"Шаг 1 из 4 — пришлите <b>название</b> товара.",
        admin_kb.confirm("noop", f"a:prods:{category_id}:0", yes_text="…"),
    )


# --- шаг 2: название --------------------------------------------------------


@router.message(ProductWizardSG.title)
async def take_title(
    message: Message, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not actor.is_admin:
        return
    title = (message.text or "").strip()
    if not title:
        await message.answer("Название пустое. Пришлите текст.")
        return
    if len(title) > MAX_TITLE:
        await message.answer(f"Слишком длинное название — максимум {MAX_TITLE} символов.")
        return

    await state.update_data(wizard_title=title)
    await state.set_state(ProductWizardSG.image)
    await message.answer(
        "Шаг 2 из 4 — пришлите <b>картинку</b> товара одним фото.\n"
        "Она будет видна покупателю в карточке."
    )


# --- шаг 3: картинка --------------------------------------------------------


@router.message(ProductWizardSG.image, F.photo)
async def take_image(
    message: Message, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not actor.is_admin:
        return
    # Последний размер — самый большой из присланных Telegram.
    file_id = message.photo[-1].file_id
    await state.update_data(wizard_image_file_id=file_id)
    await state.set_state(ProductWizardSG.price)
    await message.answer(
        "Шаг 3 из 4 — пришлите <b>цену в долларах</b>.\n"
        "Например <code>20</code> или <code>19.99</code>.\n\n"
        "Покупатель увидит её в долларах, а заплатит рублями по курсу ЦБ."
    )


@router.message(ProductWizardSG.image)
async def image_expected(message: Message, actor: Actor, **_: object) -> None:
    if not actor.is_admin:
        return
    await message.answer("Нужна именно картинка — пришлите фото.")


# --- шаг 4: цена ------------------------------------------------------------


@router.message(ProductWizardSG.price)
async def take_price(
    message: Message, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not actor.is_admin:
        return
    try:
        cents = parse_price_usd(message.text or "")
    except ValueError as exc:
        await message.answer(f"Не понял цену: {exc}. Пришлите число, например <code>19.99</code>.")
        return

    await state.update_data(wizard_price_cents=cents)
    await state.set_state(ProductWizardSG.description)
    await message.answer(
        f"Цена: <b>{pricing.format_usd(cents)}</b>\n\n"
        "Шаг 4 из 4 — пришлите <b>описание</b> товара.\n"
        "Если описание не нужно, отправьте <code>-</code>.",
    )


# --- шаг 5: описание и предпросмотр -----------------------------------------


@router.message(ProductWizardSG.description)
async def take_description(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not actor.is_admin:
        return
    raw = (message.text or "").strip()
    description = None if raw == "-" else raw
    await state.update_data(wizard_description=description)
    await state.set_state(ProductWizardSG.confirm)

    data = await state.get_data()
    category = await catalog_repo.get_category(session, int(data["wizard_category_id"]))
    preview = (
        "👀 <b>Предпросмотр</b>\n\n"
        f"📂 {html.escape(category.title if category else '—')}\n"
        f"📦 <b>{html.escape(data['wizard_title'])}</b>\n"
        f"💵 {pricing.format_usd(int(data['wizard_price_cents']))}\n\n"
        f"{html.escape(description or 'Без описания')}\n\n"
        "Сохранить товар?"
    )
    await message.answer_photo(
        data["wizard_image_file_id"],
        caption=preview,
        reply_markup=admin_kb.confirm("a:prod_save", "a:prod_wizard_cancel", yes_text="💾 Сохранить"),
    )


# --- шаг 6: сохранение ------------------------------------------------------


@router.callback_query(F.data == "a:prod_save", ProductWizardSG.confirm)
async def save_product(
    call: CallbackQuery, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(call, actor):
        return
    data = await state.get_data()
    required = ("wizard_category_id", "wizard_title", "wizard_price_cents", "wizard_image_file_id")
    if not all(data.get(key) for key in required):
        # Состояние потерялось — например, бот перезапустили посреди мастера.
        # Создавать товар из половины данных нельзя.
        await soft_reset(state)
        await call.answer("Данные мастера потерялись, начните заново", show_alert=True)
        return

    product = await catalog_repo.create_product(
        session,
        category_id=int(data["wizard_category_id"]),
        title=str(data["wizard_title"]),
        description=data.get("wizard_description"),
        price_usd_cents=int(data["wizard_price_cents"]),
        image_file_id=str(data["wizard_image_file_id"]),
    )
    await audit_repo.record(
        session, actor.user_id, "product.create", "product", product.id, {"via": "wizard"}
    )
    await soft_reset(state)
    await call.answer("Товар выложен")
    await show(
        call,
        f"✅ Товар <b>{html.escape(product.title)}</b> выложен — "
        f"{pricing.format_usd(product.price_usd_cents)}.",
        admin_kb.confirm("noop", f"a:prods:{product.category_id}:0", yes_text="…"),
    )


@router.callback_query(F.data == "a:prod_wizard_cancel")
async def cancel_wizard(
    call: CallbackQuery, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(call, actor):
        return
    data = await state.get_data()
    category_id = data.get("wizard_category_id")
    await soft_reset(state)
    await call.answer("Отменено")
    back = f"a:prods:{category_id}:0" if category_id else "a:cats:0"
    await show(call, "🚫 Выкладка отменена, товар не создан.", admin_kb.confirm("noop", back, yes_text="…"))
