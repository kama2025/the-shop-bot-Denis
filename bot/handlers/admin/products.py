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
from bot.repo import stock as stock_repo
from bot.services.access import Actor
from bot.states.admin import ProductSG
from bot.utils.money import PriceParseError, format_kop, parse_price_to_kop
from bot.utils.pagination import paginate
from bot.utils.render import show

router = Router(name="admin.products")

PER_PAGE = 8
SECTION = "products"


@router.callback_query(F.data.startswith("a:prods:"))
async def list_products(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor, SECTION, "list"):
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
    counts = await catalog_repo.stock_counts(session, [p.id for p in chunk.items])
    text = f"📦 <b>Товары: {html.escape(category.title)}</b> — всего {chunk.total}"
    if chunk.is_empty:
        text += "\n\nПока ни одного."
    await show(
        call, text, admin_kb.products(chunk.items, counts, category_id, chunk.page, chunk.pages)
    )


@router.callback_query(F.data.startswith("a:prod:"))
async def product_card(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor, SECTION, "view"):
        return
    await call.answer()
    product_id = int(call.data.split(":")[2])
    product = await catalog_repo.get_product(session, product_id)
    if product is None:
        await call.answer("Товар удалён", show_alert=True)
        return

    counts = await stock_repo.counts_by_status(session, product_id)
    category = await catalog_repo.get_category(session, product.category_id)
    text = (
        f"📦 <b>{html.escape(product.title)}</b>\n\n"
        f"Категория: {html.escape(category.title if category else '—')}\n"
        f"Цена: {format_kop(product.price_kop)}\n"
        f"Порядок: {product.sort_order}\n"
        f"Статус: {'🟢 в продаже' if product.is_active else '🔴 снят'}\n"
        f"Картинка: {'есть' if (product.image_file_id or product.image_path) else 'нет'}\n\n"
        f"<b>Склад:</b> свободно {counts['available']}, в резерве {counts['reserved']}, "
        f"продано {counts['sold']}, брак {counts['defective']}\n\n"
        f"Описание:\n{html.escape((product.description or '—')[:600])}"
    )
    await show(call, text, admin_kb.product_card(product))


# --- создание ---------------------------------------------------------------


@router.callback_query(F.data.startswith("a:prod_add:"))
async def ask_title(
    call: CallbackQuery, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(call, actor, SECTION, "create"):
        return
    await call.answer()
    category_id = int(call.data.split(":")[2])
    await state.set_state(ProductSG.title)
    await state.update_data(category_id=category_id)
    await show(
        call,
        "Шаг 1 из 4. Отправьте название товара:",
        admin_kb.confirm("noop", f"a:prods:{category_id}:0", yes_text="…"),
    )


@router.message(ProductSG.title)
async def got_title(
    message: Message, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor, SECTION, "create"):
        await state.clear()
        return
    title = (message.text or "").strip()
    if not 1 <= len(title) <= 255:
        await message.answer("Название должно быть от 1 до 255 символов.")
        return
    await state.update_data(title=title)
    await state.set_state(ProductSG.description)
    await message.answer("Шаг 2 из 4. Отправьте описание (или «-», чтобы пропустить):")


@router.message(ProductSG.description)
async def got_description(
    message: Message, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor, SECTION, "create"):
        await state.clear()
        return
    raw = (message.text or "").strip()
    await state.update_data(description=None if raw == "-" else raw)
    await state.set_state(ProductSG.price)
    await message.answer("Шаг 3 из 4. Отправьте цену в рублях (например 90 или 90,50):")


@router.message(ProductSG.price)
async def got_price(
    message: Message, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor, SECTION, "create"):
        await state.clear()
        return
    try:
        price_kop = parse_price_to_kop(message.text or "")
    except PriceParseError as exc:
        await message.answer(f"Не понял цену: {exc}. Попробуйте ещё раз.")
        return
    if price_kop <= 0:
        await message.answer("Цена должна быть больше нуля.")
        return
    await state.update_data(price_kop=price_kop)
    await state.set_state(ProductSG.image)
    await message.answer("Шаг 4 из 4. Пришлите картинку товара или напишите «-», чтобы пропустить.")


@router.message(ProductSG.image)
async def got_image(
    message: Message,
    session: AsyncSession,
    actor: Actor,
    state: FSMContext,
    settings: Settings,
    **_: object,
) -> None:
    if not await guard(message, actor, SECTION, "create"):
        await state.clear()
        return
    data = await state.get_data()
    image_path, image_file_id = await _extract_image(message, settings, data["title"])
    if image_path is None and image_file_id is None and (message.text or "").strip() != "-":
        await message.answer("Пришлите картинку или напишите «-».")
        return

    product = await catalog_repo.create_product(
        session,
        category_id=int(data["category_id"]),
        title=data["title"],
        description=data.get("description"),
        price_kop=int(data["price_kop"]),
        image_path=image_path,
        image_file_id=image_file_id,
    )
    await audit_repo.record(
        session, actor.user_id, "product.create", "product", product.id,
        {"title": product.title, "price_kop": product.price_kop},
    )
    await state.clear()
    await message.answer(
        f"✅ Товар «{html.escape(product.title)}» создан за {format_kop(product.price_kop)}.\n"
        "Теперь залейте позиции на склад — без них товар не продаётся.",
        reply_markup=admin_kb.product_card(product),
    )


async def _extract_image(
    message: Message, settings: Settings, title: str
) -> tuple[str | None, str | None]:
    if not message.photo:
        return None, None
    largest = message.photo[-1]
    directory = settings.media_dir / "products"
    directory.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in title if ch.isalnum() or ch in " -_")[:40].strip() or "product"
    target = directory / f"{safe}-{largest.file_unique_id}.jpg"
    await message.bot.download(largest, destination=target)
    return str(target), largest.file_id


# --- правка -----------------------------------------------------------------


FIELD_PROMPTS = {
    "title": ("Отправьте новое название:", ProductSG.edit_title),
    "desc": ("Отправьте новое описание (или «-», чтобы очистить):", ProductSG.edit_description),
    "price": ("Отправьте новую цену в рублях:", ProductSG.edit_price),
    "image": ("Пришлите новую картинку (или «-», чтобы удалить):", ProductSG.edit_image),
}


@router.callback_query(F.data.startswith("a:prod_edit:"))
async def ask_edit(
    call: CallbackQuery, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(call, actor, SECTION, "act"):
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
    if not await guard(message, actor, SECTION, "act"):
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
    if not await guard(message, actor, SECTION, "act"):
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
    if not await guard(message, actor, SECTION, "act"):
        await state.clear()
        return
    product = await _current(session, state)
    if product is None:
        await message.answer("Товар не найден.")
        await state.clear()
        return
    try:
        price_kop = parse_price_to_kop(message.text or "")
    except PriceParseError as exc:
        await message.answer(f"Не понял цену: {exc}")
        return
    if price_kop <= 0:
        await message.answer("Цена должна быть больше нуля.")
        return

    before = product.price_kop
    product.price_kop = price_kop
    await audit_repo.record(
        session, actor.user_id, "product.reprice", "product", product.id,
        {"before_kop": before, "after_kop": price_kop},
    )
    await state.clear()
    # Уже оформленные заказы сохраняют старую цену: в заказе лежит её снимок.
    await message.answer(
        f"✅ Цена изменена: {format_kop(before)} → {format_kop(price_kop)}.\n"
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
    if not await guard(message, actor, SECTION, "act"):
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
    if not await guard(call, actor, SECTION, "act"):
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
    if not await guard(call, actor, SECTION, "act"):
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
    if not await guard(call, actor, SECTION, "act"):
        return
    await call.answer()
    product_id = int(call.data.split(":")[2])
    categories = await catalog_repo.list_categories(session, only_active=False)
    await show(call, "Выберите новую категорию:", admin_kb.category_picker(categories, product_id))


@router.callback_query(F.data.startswith("a:prod_setcat:"))
async def set_category(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor, SECTION, "act"):
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
    if not await guard(call, actor, SECTION, "act"):
        return
    await call.answer()
    product_id = int(call.data.split(":")[2])
    product = await catalog_repo.get_product(session, product_id)
    if product is None:
        await call.answer("Товар удалён", show_alert=True)
        return

    counts = await stock_repo.counts_by_status(session, product_id)
    text = (
        f"⚠️ Удалить товар «{html.escape(product.title)}»?\n\n"
        f"Вместе с ним удалятся позиции склада: свободных {counts['available']}, "
        f"в резерве {counts['reserved']}, бракованных {counts['defective']}.\n"
        f"Проданных позиций: {counts['sold']} — они останутся в истории заказов.\n\n"
        "Если товар нужно просто убрать из магазина — лучше выключить его."
    )
    await show(call, text, admin_kb.confirm(f"a:prod_del_ok:{product_id}", f"a:prod:{product_id}"))


@router.callback_query(F.data.startswith("a:prod_del_ok:"))
async def do_delete(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor, SECTION, "act"):
        return
    product_id = int(call.data.split(":")[2])
    product = await catalog_repo.get_product(session, product_id)
    if product is None:
        await call.answer("Товар уже удалён", show_alert=True)
        return
    category_id = product.category_id

    # Проданные позиции связаны с order_items внешним ключом RESTRICT, поэтому
    # физическое удаление товара с историей продаж не пройдёт. Это не ошибка,
    # а защита истории: такой товар выключают, а не удаляют.
    counts = await stock_repo.counts_by_status(session, product_id)
    if counts["sold"]:
        product.is_active = False
        await call.answer("Есть продажи — товар выключен, а не удалён", show_alert=True)
        await audit_repo.record(session, actor.user_id, "product.disable_instead_delete", "product", product_id)
        call.data = f"a:prod:{product_id}"
        await product_card(call, session, actor)
        return

    await catalog_repo.delete_product(session, product_id)
    await audit_repo.record(session, actor.user_id, "product.delete", "product", product_id)
    await call.answer("Товар удалён")
    call.data = f"a:prods:{category_id}:0"
    await list_products(call, session, actor)
