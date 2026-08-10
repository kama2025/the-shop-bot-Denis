"""Каталог: категории, товары, карточка товара, наличие, поиск."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import User
from bot.keyboards import user as user_kb
from bot.keyboards.theme import ICON
from bot.repo import catalog as catalog_repo
from bot.repo import promo as promo_repo
from bot.repo import stock as stock_repo
from bot.services import header as header_service
from bot.services import orders as orders_service
from bot.states.user import UserSG
from bot.services.texts import text_service
from bot.utils.money import format_kop
from bot.utils.pagination import paginate
from bot.utils.render import show

router = Router(name="user.catalog")

PER_PAGE = 8


async def _photo(session: AsyncSession, product=None):
    if product is not None:
        if product.image_file_id:
            return product.image_file_id
        if product.image_path:
            from pathlib import Path

            from aiogram.types import FSInputFile

            path = Path(product.image_path)
            if path.exists():
                return FSInputFile(str(path))
    return await header_service.photo(session)


@router.callback_query(F.data.startswith("u:cats:"))
async def open_categories(call: CallbackQuery, session: AsyncSession, **_: object) -> None:
    await call.answer()
    page = int(call.data.split(":")[2])
    categories = await catalog_repo.list_categories(session, only_active=True)
    if not categories:
        await show(
            call,
            await text_service.get(session, "catalog_empty"),
            user_kb.simple_back(),
            await header_service.photo(session),
        )
        return

    chunk = paginate(categories, page, PER_PAGE)
    await show(
        call,
        await text_service.get(session, "catalog_title"),
        user_kb.categories(chunk.items, chunk.page, chunk.pages),
        await header_service.photo(session),
    )


@router.callback_query(F.data.startswith("u:cat:"))
async def open_category(call: CallbackQuery, session: AsyncSession, **_: object) -> None:
    await call.answer()
    _, _, category_id_raw, page_raw = call.data.split(":", 3)
    category_id, page = int(category_id_raw), int(page_raw)

    category = await catalog_repo.get_category(session, category_id)
    if category is None or not category.is_active:
        await call.answer("Категория недоступна", show_alert=True)
        return

    products = await catalog_repo.list_products(session, category_id, only_active=True)
    header = await text_service.get(session, "category_title", category=html.escape(category.title))
    if not products:
        text = header + "\n\n" + await text_service.get(session, "category_empty")
        await show(call, text, user_kb.simple_back("u:cats:0"), await header_service.photo(session))
        return

    chunk = paginate(products, page, PER_PAGE)
    stock = await catalog_repo.stock_counts(session, [p.id for p in chunk.items])
    await show(
        call,
        header,
        user_kb.products(chunk.items, stock, category_id, chunk.page, chunk.pages),
        await header_service.photo(session),
    )


@router.callback_query(F.data.startswith("u:prod:"))
async def open_product(
    call: CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    state: FSMContext,
    **_: object,
) -> None:
    await call.answer()
    _, _, product_id_raw, qty_raw = call.data.split(":", 3)
    product_id, qty = int(product_id_raw), int(qty_raw)

    product = await catalog_repo.get_product(session, product_id)
    if product is None or not product.is_active:
        await call.answer("Товар недоступен", show_alert=True)
        return

    in_stock = await stock_repo.available_count(session, product.id)
    if in_stock == 0:
        await show(
            call,
            await text_service.get(session, "product_out_of_stock"),
            user_kb.simple_back(f"u:cat:{product.category_id}:0"),
            await _photo(session, product),
        )
        return

    qty = max(1, min(qty, settings.max_qty_per_order, in_stock))

    promo = await _active_promo(session, state, user.tg_id, product)
    calc = orders_service.quote(product, qty, promo)

    text = await text_service.get(
        session,
        "product_card",
        title=html.escape(product.title),
        description=html.escape(product.description or ""),
        price=format_kop(product.price_kop),
        stock=in_stock,
        qty=qty,
        total=format_kop(calc.total_kop),
    )
    if promo is not None:
        text += f"\n{ICON['promo']} Промокод <b>{html.escape(promo.code)}</b> применён"

    await show(
        call,
        text,
        user_kb.product_card(
            product,
            qty,
            calc.total_kop,
            in_stock,
            settings.max_qty_per_order,
            back_data=f"u:cat:{product.category_id}:0",
        ),
        await _photo(session, product),
    )


async def _active_promo(session: AsyncSession, state: FSMContext, user_id: int, product):
    """Промокод, сохранённый пользователем в профиле.

    Проверяется заново на каждый показ: между вводом и покупкой он мог
    закончиться, и показывать зачёркнутую цену, которой уже нет, нельзя.
    """
    data = await state.get_data()
    code = data.get("promo_code")
    if not code:
        return None
    from bot.services import promo as promo_service

    check = await promo_service.validate(session, code, user_id, product=product)
    return check.promo if check.ok else None


@router.callback_query(F.data == "u:avail")
async def availability(call: CallbackQuery, session: AsyncSession, **_: object) -> None:
    await call.answer()
    categories = await catalog_repo.list_categories(session, only_active=True)
    lines = [await text_service.get(session, "availability_title"), ""]

    total = 0
    for category in categories:
        products = await catalog_repo.list_products(session, category.id, only_active=True)
        if not products:
            continue
        counts = await catalog_repo.stock_counts(session, [p.id for p in products])
        lines.append(f"<b>{html.escape(category.title)}</b>")
        for product in products:
            left = counts.get(product.id, 0)
            total += left
            mark = "🟢" if left > 3 else ("🟡" if left else "🔴")
            lines.append(
                f"{mark} {html.escape(product.title)} — {left} шт. · {format_kop(product.price_kop)}"
            )
        lines.append("")

    if total == 0 and len(lines) <= 2:
        lines.append(await text_service.get(session, "catalog_empty"))

    await show(call, "\n".join(lines).strip(), user_kb.simple_back())


@router.callback_query(F.data == "u:search")
async def ask_search(
    call: CallbackQuery, session: AsyncSession, state: FSMContext, **_: object
) -> None:
    await call.answer()
    await state.set_state(UserSG.search)
    await show(call, await text_service.get(session, "search_prompt"), user_kb.simple_back())


@router.message(UserSG.search)
async def do_search(
    message: Message, session: AsyncSession, state: FSMContext, **_: object
) -> None:
    query = (message.text or "").strip()
    await state.set_state(None)
    if len(query) < 2:
        await message.answer("Слишком короткий запрос — нужно хотя бы два символа.")
        return

    products = await catalog_repo.search_products(session, query, limit=20)
    if not products:
        await message.answer(
            await text_service.get(session, "search_empty", query=html.escape(query)),
            reply_markup=user_kb.simple_back(),
        )
        return

    counts = await catalog_repo.stock_counts(session, [p.id for p in products])
    await message.answer(
        f"{ICON['search']} Найдено: {len(products)}",
        reply_markup=user_kb.products(products, counts, products[0].category_id, 0, 1),
    )
