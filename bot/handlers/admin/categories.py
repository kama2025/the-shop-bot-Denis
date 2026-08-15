"""Админка: категории."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.admin.common import guard
from bot.keyboards import admin as admin_kb
from bot.repo import audit as audit_repo
from bot.repo import catalog as catalog_repo
from bot.services.access import Actor
from bot.db.models import CategoryAccent
from bot.states.admin import CategorySG
from bot.utils.pagination import paginate
from bot.utils.render import show

router = Router(name="admin.categories")

PER_PAGE = 8


@router.callback_query(F.data.startswith("a:cats:"))
async def list_categories(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    page = int(call.data.split(":")[2])
    items = await catalog_repo.list_categories(session, only_active=False)
    chunk = paginate(items, page, PER_PAGE)
    text = f"📂 <b>Категории</b> — всего {chunk.total}"
    if chunk.is_empty:
        text += "\n\nПока ни одной. Нажмите «Новая категория»."
    await show(call, text, admin_kb.categories(chunk.items, chunk.page, chunk.pages))


@router.callback_query(F.data.startswith("a:cat:"))
async def category_card(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    category_id = int(call.data.split(":")[2])
    category = await catalog_repo.get_category(session, category_id)
    if category is None:
        await call.answer("Категория удалена", show_alert=True)
        return

    products_count = await catalog_repo.count_products_in_category(session, category_id)
    text = (
        f"📂 <b>{html.escape(category.title)}</b>\n\n"
        f"Описание: {html.escape(category.description or '—')}\n"
        f"Товаров: {products_count}\n"
        f"Порядок: {category.sort_order}\n"
        f"Цвет: {CategoryAccent.TITLES.get(CategoryAccent.normalize(category.accent), '—')}\n"
        f"Статус: {'🟢 включена' if category.is_active else '🔴 выключена'}"
    )
    await show(call, text, admin_kb.category_card(category, products_count))


@router.callback_query(F.data == "a:cat_add")
async def ask_title(
    call: CallbackQuery, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    await state.set_state(CategorySG.title)
    await show(call, "Отправьте название новой категории:", admin_kb.confirm("noop", "a:cats:0", yes_text="…"))


@router.message(CategorySG.title)
async def create_category(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return
    title = (message.text or "").strip()
    if not 1 <= len(title) <= 128:
        await message.answer("Название должно быть от 1 до 128 символов. Попробуйте ещё раз.")
        return

    # Категория ещё не создаётся: сначала цвет. Создать её здесь и покрасить
    # следующим шагом значило бы, что брошенный на цвете мастер оставляет
    # в каталоге категорию, которую никто не заказывал.
    await state.update_data(new_category_title=title)
    await state.set_state(CategorySG.accent)
    await message.answer(
        f"Название: <b>{html.escape(title)}</b>\n\n"
        "Выберите <b>цвет</b> категории. Им будут выкрашены и её кнопка, "
        "и кнопки товаров внутри.\n\n"
        "<i>Цветов ровно четыре — больше Telegram у кнопок не поддерживает.</i>",
        reply_markup=admin_kb.accent_picker("a:cats:0", "a:cat_newaccent:"),
    )


@router.callback_query(F.data.startswith("a:cat_newaccent:"), CategorySG.accent)
async def create_with_accent(
    call: CallbackQuery, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(call, actor):
        return
    accent = CategoryAccent.normalize(call.data.split(":")[2])
    data = await state.get_data()
    title = (data.get("new_category_title") or "").strip()
    if not title:
        await state.clear()
        await call.answer("Название потерялось, начните заново", show_alert=True)
        return

    category = await catalog_repo.create_category(session, title, accent=accent)
    await audit_repo.record(
        session, actor.user_id, "category.create", "category", category.id,
        {"title": title, "accent": accent},
    )
    await state.clear()
    await call.answer("Категория создана")

    items = await catalog_repo.list_categories(session, only_active=False)
    chunk = paginate(items, 0, PER_PAGE)
    await show(
        call,
        f"✅ Категория «{html.escape(title)}» создана — "
        f"{CategoryAccent.TITLES[accent]}\n\n"
        f"📂 <b>Категории</b> — всего {chunk.total}",
        admin_kb.categories(chunk.items, chunk.page, chunk.pages),
    )


@router.callback_query(F.data.startswith("a:cat_accent:"))
async def ask_accent(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    """Смена цвета у существующей категории."""
    if not await guard(call, actor):
        return
    category_id = int(call.data.split(":")[2])
    category = await catalog_repo.get_category(session, category_id)
    if category is None:
        await call.answer("Категория удалена", show_alert=True)
        return
    await call.answer()
    await show(
        call,
        f"🎨 Цвет категории «{html.escape(category.title)}»\n\n"
        f"Сейчас: {CategoryAccent.TITLES.get(CategoryAccent.normalize(category.accent), '—')}\n\n"
        "Им красятся кнопка категории и кнопки товаров внутри.",
        admin_kb.accent_picker(f"a:cat:{category_id}", f"a:cat_setaccent:{category_id}:"),
    )


@router.callback_query(F.data.startswith("a:cat_setaccent:"))
async def set_accent(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    parts = call.data.split(":")
    category_id, accent = int(parts[2]), CategoryAccent.normalize(parts[3])
    category = await catalog_repo.get_category(session, category_id)
    if category is None:
        await call.answer("Категория удалена", show_alert=True)
        return

    before = category.accent
    category.accent = accent
    await audit_repo.record(
        session, actor.user_id, "category.accent", "category", category_id,
        {"before": before, "after": accent},
    )
    await call.answer(f"Цвет: {CategoryAccent.TITLES[accent]}")
    call.data = f"a:cat:{category_id}"
    await category_card(call, session, actor)


@router.callback_query(F.data.startswith("a:cat_edit:"))
async def ask_edit(
    call: CallbackQuery, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    _, _, category_id, field = call.data.split(":", 3)
    await state.update_data(category_id=int(category_id))
    if field == "title":
        await state.set_state(CategorySG.edit_title)
        await show(call, "Отправьте новое название:", admin_kb.confirm("noop", f"a:cat:{category_id}", yes_text="…"))
    else:
        await state.set_state(CategorySG.edit_description)
        await show(
            call,
            "Отправьте новое описание (или «-», чтобы очистить):",
            admin_kb.confirm("noop", f"a:cat:{category_id}", yes_text="…"),
        )


@router.message(CategorySG.edit_title)
async def save_title(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return
    data = await state.get_data()
    category = await catalog_repo.get_category(session, int(data["category_id"]))
    title = (message.text or "").strip()
    if category is None or not 1 <= len(title) <= 128:
        await message.answer("Не сохранил: категория не найдена или название неподходящей длины.")
        return
    before = category.title
    category.title = title
    await audit_repo.record(
        session, actor.user_id, "category.rename", "category", category.id,
        {"before": before, "after": title},
    )
    await state.clear()
    await message.answer(f"✅ Название изменено на «{html.escape(title)}».")


@router.message(CategorySG.edit_description)
async def save_description(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return
    data = await state.get_data()
    category = await catalog_repo.get_category(session, int(data["category_id"]))
    if category is None:
        await message.answer("Категория не найдена.")
        await state.clear()
        return
    raw = (message.text or "").strip()
    category.description = None if raw == "-" else raw
    await audit_repo.record(session, actor.user_id, "category.describe", "category", category.id)
    await state.clear()
    await message.answer("✅ Описание обновлено.")


@router.callback_query(F.data.startswith("a:cat_toggle:"))
async def toggle(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    category_id = int(call.data.split(":")[2])
    category = await catalog_repo.get_category(session, category_id)
    if category is None:
        await call.answer("Категория удалена", show_alert=True)
        return
    category.is_active = not category.is_active
    await audit_repo.record(
        session, actor.user_id, "category.toggle", "category", category_id,
        {"is_active": category.is_active},
    )
    await call.answer("🟢 Включена" if category.is_active else "🔴 Выключена")
    await category_card(call, session, actor)


@router.callback_query(F.data.startswith("a:cat_move:"))
async def move(call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object) -> None:
    if not await guard(call, actor):
        return
    parts = call.data.split(":")
    category_id, direction = int(parts[2]), int(parts[3])
    moved = await catalog_repo.swap_category_order(session, category_id, direction)
    await call.answer("Готово" if moved else "Уже с краю")
    call.data = f"a:cat:{category_id}"
    await category_card(call, session, actor)


@router.callback_query(F.data.startswith("a:cat_del:"))
async def ask_delete(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    category_id = int(call.data.split(":")[2])
    category = await catalog_repo.get_category(session, category_id)
    if category is None:
        await call.answer("Категория удалена", show_alert=True)
        return

    # Предупреждение с числами: «внутри что-то есть» без цифр никого не
    # останавливает.
    products = await catalog_repo.count_products_in_category(session, category_id)

    if products:
        text = (
            f"⚠️ <b>Внутри категории «{html.escape(category.title)}» есть товары.</b>\n\n"
            f"Товаров: <b>{products}</b>\n\n"
            "Категорию с товарами удалить нельзя — сначала перенесите или удалите товары.\n"
            "Если нужно просто убрать её из магазина — выключите."
        )
        await show(call, text, admin_kb.confirm(f"a:cat_toggle:{category_id}", f"a:cat:{category_id}", yes_text="🔘 Выключить"))
        return

    text = (
        f"Удалить категорию «{html.escape(category.title)}»?\n\n"
        "Внутри нет товаров, так что удаление безопасно."
    )
    await show(call, text, admin_kb.confirm(f"a:cat_del_ok:{category_id}", f"a:cat:{category_id}"))


@router.callback_query(F.data.startswith("a:cat_del_ok:"))
async def do_delete(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    category_id = int(call.data.split(":")[2])

    # Проверяем ещё раз: между показом предупреждения и нажатием кто-то мог
    # добавить товар.
    products = await catalog_repo.count_products_in_category(session, category_id)
    if products:
        await call.answer("В категории появились товары — удаление отменено", show_alert=True)
        return

    await catalog_repo.delete_category(session, category_id)
    await audit_repo.record(session, actor.user_id, "category.delete", "category", category_id)
    await call.answer("Категория удалена")
    call.data = "a:cats:0"
    await list_categories(call, session, actor)
