"""Админка: товары."""

from __future__ import annotations

import html
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.handlers.admin.common import guard
from bot.keyboards import admin as admin_kb
from bot.repo import audit as audit_repo
from bot.repo import catalog as catalog_repo
from bot.services.access import Actor
from bot.states.admin import ProductSG
from bot.utils.money import PriceParseError, format_kop, parse_price_to_kop
from bot.utils.pagination import paginate
from bot.utils.render import show

router = Router(name="admin.products")

PER_PAGE = 8


@router.callback_query(F.data.startswith("a:prods:"))
async def list_products(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    _, _, category_id_raw, page_raw = call.data.split(":", 3)
    category_id, page = int(category_id_raw), int(page_raw)

    category = await catalog_repo.get_category(session, category_id)
    if category is None:
        await call.answer("Категория удалена", show_alert=True)
        return

    items = await catalog_repo.list_products(session, category_id, only_active=False)
    chunk = paginate(items, page, PER_PAGE)
    text = f"📦 <b>Товары: {html.escape(category.title)}</b> — всего {chunk.total}"
    if chunk.is_empty:
        text += "\n\nПока ни одного."
    await show(call, text, admin_kb.products(chunk.items, category_id, chunk.page, chunk.pages))


@router.callback_query(F.data.startswith("a:prod:"))
async def product_card(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    product_id = int(call.data.split(":")[2])
    product = await catalog_repo.get_product(session, product_id)
    if product is None:
        await call.answer("Товар удалён", show_alert=True)
        return

    category = await catalog_repo.get_category(session, product.category_id)
    text = (
        f"📦 <b>{html.escape(product.title)}</b>\n\n"
        f"Категория: {html.escape(category.title if category else '—')}\n"
        f"Цена: {format_usd(product.price_usd_cents)}\n"
        f"Порядок: {product.sort_order}\n"
        f"Статус: {'🟢 в продаже' if product.is_active else '🔴 снят'}\n"
        f"Картинка: {'есть' if (product.image_file_id or product.image_path) else 'нет'}\n\n"
        f"Описание:\n{html.escape((product.description or '—')[:600])}"
    )
    await show(call, text, admin_kb.product_card(product))


# --- создание ---------------------------------------------------------------


@router.callback_query(F.data.startswith("a:prod_edit:"))
async def ask_edit(
    call: CallbackQuery, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    _, _, product_id, field = call.data.split(":", 3)
    prompt, target_state = FIELD_PROMPTS[field]
    await state.update_data(product_id=int(product_id))
    await state.set_state(target_state)
    await show(call, prompt, admin_kb.confirm("noop", f"a:prod:{product_id}", yes_text="…"))


@router.message(ProductSG.edit_title)
async def save_title(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return
    product = await _current(session, state)
    title = (message.text or "").strip()
    if product is None or not 1 <= len(title) <= 255:
        await message.answer("Не сохранил: товар не найден или название неподходящей длины.")
        return
    product.title = title
    await audit_repo.record(session, actor.user_id, "product.rename", "product", product.id)
    await state.clear()
    await message.answer("✅ Название обновлено.", reply_markup=admin_kb.product_card(product))


@router.message(ProductSG.edit_description)
async def save_description(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return
    product = await _current(session, state)
    if product is None:
        await message.answer("Товар не найден.")
        await state.clear()
        return
    raw = (message.text or "").strip()
    product.description = None if raw == "-" else raw
    await audit_repo.record(session, actor.user_id, "product.describe", "product", product.id)
    await state.clear()
    await message.answer("✅ Описание обновлено.", reply_markup=admin_kb.product_card(product))


@router.message(ProductSG.edit_price)
async def save_price(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return
    product = await _current(session, state)
    if product is None:
        await message.answer("Товар не найден.")
        await state.clear()
        return
    try:
        from bot.handlers.admin.product_wizard import parse_price_usd

        price_cents = parse_price_usd(message.text or "")
    except PriceParseError as exc:
        await message.answer(f"Не понял цену: {exc}")
        return
    if price_cents <= 0:
        await message.answer("Цена должна быть больше нуля.")
        return

    before = product.price_usd_cents
    product.price_usd_cents = price_cents
    await audit_repo.record(
        session, actor.user_id, "product.reprice", "product", product.id,
        {"before_cents": before, "after_cents": price_cents},
    )
    await state.clear()
    # Уже оформленные заказы сохраняют старую цену: в заказе лежит её снимок.
    await message.answer(
        f"✅ Цена изменена: {format_usd(before)} → {format_usd(price_cents)}.\n"
        "Заказы, оформленные раньше, сохраняют прежнюю цену.",
        reply_markup=admin_kb.product_card(product),
    )


@router.message(ProductSG.edit_image)
async def save_image(
    message: Message,
    session: AsyncSession,
    actor: Actor,
    state: FSMContext,
    settings: Settings,
    **_: object,
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return
    product = await _current(session, state)
    if product is None:
        await message.answer("Товар не найден.")
        await state.clear()
        return

    if (message.text or "").strip() == "-":
        product.image_path = None
        product.image_file_id = None
        await state.clear()
        await message.answer("✅ Картинка удалена.", reply_markup=admin_kb.product_card(product))
        return

    image_path, image_file_id = await _extract_image(message, settings, product.title)
    if image_path is None:
        await message.answer("Пришлите картинку или напишите «-».")
        return
    product.image_path = image_path
    product.image_file_id = image_file_id
    await audit_repo.record(session, actor.user_id, "product.image", "product", product.id)
    await state.clear()
    await message.answer("✅ Картинка обновлена.", reply_markup=admin_kb.product_card(product))


async def _current(session: AsyncSession, state: FSMContext):
    data = await state.get_data()
    product_id = data.get("product_id")
    if product_id is None:
        return None
    return await catalog_repo.get_product(session, int(product_id))


# --- действия ---------------------------------------------------------------


@router.callback_query(F.data.startswith("a:prod_toggle:"))
async def toggle(call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object) -> None:
    if not await guard(call, actor):
        return
    product_id = int(call.data.split(":")[2])
    product = await catalog_repo.get_product(session, product_id)
    if product is None:
        await call.answer("Товар удалён", show_alert=True)
        return
    product.is_active = not product.is_active
    await audit_repo.record(
        session, actor.user_id, "product.toggle", "product", product_id,
        {"is_active": product.is_active},
    )
    await call.answer("🟢 В продаже" if product.is_active else "🔴 Снят с продажи")
    call.data = f"a:prod:{product_id}"
    await product_card(call, session, actor)


@router.callback_query(F.data.startswith("a:prod_move:"))
async def move(call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object) -> None:
    if not await guard(call, actor):
        return
    parts = call.data.split(":")
    product_id, direction = int(parts[2]), int(parts[3])
    moved = await catalog_repo.swap_product_order(session, product_id, direction)
    await call.answer("Готово" if moved else "Уже с краю")
    call.data = f"a:prod:{product_id}"
    await product_card(call, session, actor)


@router.callback_query(F.data.startswith("a:prod_cat:"))
async def ask_category(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    product_id = int(call.data.split(":")[2])
    categories = await catalog_repo.list_categories(session, only_active=False)
    await show(call, "Выберите новую категорию:", admin_kb.category_picker(categories, product_id))


@router.callback_query(F.data.startswith("a:prod_setcat:"))
async def set_category(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    parts = call.data.split(":")
    product_id, category_id = int(parts[2]), int(parts[3])
    product = await catalog_repo.get_product(session, product_id)
    if product is None:
        await call.answer("Товар удалён", show_alert=True)
        return
    product.category_id = category_id
    await audit_repo.record(
        session, actor.user_id, "product.move_category", "product", product_id,
        {"category_id": category_id},
    )
    await call.answer("Категория изменена")
    call.data = f"a:prod:{product_id}"
    await product_card(call, session, actor)


@router.callback_query(F.data.startswith("a:prod_del:"))
async def ask_delete(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    product_id = int(call.data.split(":")[2])
    product = await catalog_repo.get_product(session, product_id)
    if product is None:
        await call.answer("Товар удалён", show_alert=True)
        return

    text = (
        f"⚠️ Удалить товар «{html.escape(product.title)}»?\n\n"
        "В заказах останется название на момент покупки, но сам товар исчезнет "
        "из каталога навсегда.\n\n"
        "Если товар нужно просто убрать из магазина — лучше выключить его."
    )
    await show(call, text, admin_kb.confirm(f"a:prod_del_ok:{product_id}", f"a:prod:{product_id}"))


@router.callback_query(F.data.startswith("a:prod_del_ok:"))
async def do_delete(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    product_id = int(call.data.split(":")[2])
    product = await catalog_repo.get_product(session, product_id)
    if product is None:
        await call.answer("Товар уже удалён", show_alert=True)
        return
    category_id = product.category_id

    await catalog_repo.delete_product(session, product_id)
    await audit_repo.record(session, actor.user_id, "product.delete", "product", product_id)
    await call.answer("Товар удалён")
    call.data = f"a:prods:{category_id}:0"
    await list_products(call, session, actor)
