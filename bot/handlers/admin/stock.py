"""Админка: склад — заливка позиций, партии, брак."""

from __future__ import annotations

import html

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import DeliveryType, StockItem
from bot.handlers.admin.common import guard
from bot.keyboards import admin as admin_kb
from bot.repo import audit as audit_repo
from bot.repo import catalog as catalog_repo
from bot.repo import stock as stock_repo
from bot.services import dispatch as dispatch_service
from bot.services import notify as notify_service
from bot.services import refunds as refunds_service
from bot.services.access import Actor
from bot.services.settings_store import settings_store
from bot.services.stock_input import parse_batch, preview
from bot.states.admin import StockSG
from bot.utils.render import show

router = Router(name="admin.stock")



@router.callback_query(F.data.startswith("a:stock:"))
async def stock_card(
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

    counts = await stock_repo.counts_by_status(session, product_id)
    batches = await stock_repo.list_batches(session, product_id)

    if product.delivery_type == DeliveryType.MANUAL:
        await show(
            call,
            f"📊 <b>Склад: {html.escape(product.title)}</b>\n\n"
            f"🙋 Тип выдачи — <b>{DeliveryType.TITLES[DeliveryType.MANUAL]}</b>.\n"
            "Складом этот товар не пользуется: после оплаты заказ попадает в "
            "«Заказы» со статусом «Ждёт выдачи», а вам приходит уведомление "
            "с контактом покупателя.\n\n"
            f"Выдано вручную: {counts['sold']}",
            admin_kb.confirm("noop", f"a:prod:{product_id}", yes_text="…"),
        )
        return

    text = (
        f"📊 <b>Склад: {html.escape(product.title)}</b>\n"
        f"Тип выдачи: {DeliveryType.TITLES.get(product.delivery_type, product.delivery_type)}\n\n"
        f"🟢 Свободно: <b>{counts['available']}</b>\n"
        f"🟡 В резерве: {counts['reserved']}\n"
        f"🔵 Продано: {counts['sold']}\n"
        f"🔴 Брак: {counts['defective']}\n\n"
        f"Завозов: {len(batches)}"
    )
    await show(call, text, admin_kb.stock_card(product, batches))


@router.callback_query(F.data.startswith("a:stock_add:"))
async def ask_items(
    call: CallbackQuery, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    product_id = int(call.data.split(":")[2])
    product = await catalog_repo.get_product(session, product_id)
    if product is None:
        await call.answer("Товар удалён", show_alert=True)
        return

    if product.delivery_type == DeliveryType.MANUAL:
        await show(
            call,
            "🙋 У этого товара ручная выдача — склад ему не нужен.\n\n"
            "После оплаты вам придёт уведомление с контактом покупателя, "
            "а заказ будет ждать в разделе «Заказы» со статусом «Ждёт выдачи».",
            admin_kb.confirm("noop", f"a:stock:{product_id}", yes_text="…"),
        )
        return

    if product.delivery_type == DeliveryType.FILE:
        await state.set_state(StockSG.files)
        await state.update_data(product_id=product_id, files=[])
        await show(
            call,
            "📎 <b>Заливка файлов</b>\n\n"
            "Присылайте файлы по одному — документы, архивы, картинки, видео.\n"
            "Подпись к файлу, если она есть, покупатель получит вместе с ним.\n\n"
            "Когда закончите, нажмите «Готово».",
            admin_kb.kb(
                [
                    [
                        admin_kb.btn(
                            "✅ Готово", callback_data="a:stock_files_done", style=admin_kb.SUCCESS
                        )
                    ],
                    admin_kb.back_row(f"a:stock:{product_id}"),
                ]
            ),
        )
        return

    await state.set_state(StockSG.items)
    await state.update_data(product_id=product_id)
    await show(
        call,
        "📥 <b>Заливка позиций</b>\n\n"
        "Отправьте позиции одним сообщением. Позиции разделяются "
        "<b>пустой строкой</b>, внутри позиции может быть сколько угодно строк.\n\n"
        "Пример:\n"
        "<code>https://example.com/gift/AAA\n\n"
        "https://example.com/gift/BBB\n\n"
        "login: user@mail.com\n"
        "pass: qwerty\n"
        "Срок: 18 мес.</code>\n\n"
        "Это три позиции. Одинаковые позиции внутри одной пачки будут отброшены.",
        admin_kb.confirm("noop", f"a:stock:{product_id}", yes_text="…"),
    )


@router.message(StockSG.items)
async def preview_items(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return

    parsed = parse_batch(message.text or message.caption or "")
    if not parsed.items:
        await message.answer("Ни одной позиции не распознал. Проверьте формат и пришлите снова.")
        return

    data = await state.get_data()
    product = await catalog_repo.get_product(session, int(data["product_id"]))
    if product is None:
        await message.answer("Товар не найден.")
        await state.clear()
        return

    await state.update_data(items=parsed.items)
    await state.set_state(StockSG.confirm)

    lines = [
        f"📥 <b>Проверьте пачку для «{html.escape(product.title)}»</b>",
        "",
        f"Распознано позиций: <b>{parsed.count}</b>",
    ]
    if parsed.duplicates:
        lines.append(f"Отброшено дубликатов: {len(parsed.duplicates)}")
    lines += ["", "<code>" + html.escape(preview(parsed.items)) + "</code>"]

    await message.answer(
        "\n".join(lines),
        reply_markup=admin_kb.confirm(
            "a:stock_confirm", f"a:stock:{product.id}", yes_text=f"✅ Добавить {parsed.count} шт"
        ),
    )


@router.message(StockSG.files)
async def collect_file(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return

    item = dispatch_service.extract_file(message)
    if item is None:
        await message.answer(
            "Это не файл. Пришлите документ, архив, картинку или видео — "
            "либо нажмите «Готово», если закончили."
        )
        return

    data = await state.get_data()
    files = list(data.get("files") or [])
    files.append(
        {
            "content": item.content,
            "file_id": item.file_id,
            "file_kind": item.file_kind,
            "file_name": item.file_name,
        }
    )
    await state.update_data(files=files)
    await message.answer(
        f"📎 Принято: <b>{html.escape(item.file_name or 'файл')}</b> "
        f"(всего {len(files)}).\nПрисылайте ещё или нажмите «Готово».",
        reply_markup=admin_kb.kb(
            [
                [
                    admin_kb.btn(
                        f"✅ Готово — добавить {len(files)} шт",
                        callback_data="a:stock_files_done",
                        style=admin_kb.SUCCESS,
                    )
                ]
            ]
        ),
    )


@router.callback_query(F.data == "a:stock_files_done", StockSG.files)
async def commit_files(
    call: CallbackQuery,
    session: AsyncSession,
    actor: Actor,
    state: FSMContext,
    bot: Bot,
    **_: object,
) -> None:
    if not await guard(call, actor):
        return
    data = await state.get_data()
    files = data.get("files") or []
    product = await catalog_repo.get_product(session, int(data["product_id"]))
    if product is None or not files:
        await call.answer("Нечего добавлять", show_alert=True)
        await state.clear()
        return

    batch = await stock_repo.add_batch(session, product.id, [], actor.user_id)
    for entry in files:
        session.add(
            StockItem(
                product_id=product.id,
                batch_id=batch.id,
                content=entry.get("content") or "",
                file_id=entry["file_id"],
                file_kind=entry.get("file_kind"),
                file_name=entry.get("file_name"),
            )
        )
    batch.items_count = len(files)
    await session.flush()

    await audit_repo.record(
        session, actor.user_id, "stock.add_files", "product", product.id,
        {"batch_id": batch.id, "count": len(files)},
    )
    await state.clear()
    await call.answer(f"Добавлено {len(files)} файлов")

    if await settings_store.get_bool(session, "restock_announce", True):
        await notify_service.notify_admins(
            bot,
            session,
            f"📥 Завоз: {html.escape(product.title)} +{len(files)} файлов "
            f"(добавил <code>{actor.user_id}</code>)",
            exclude=actor.user_id,
        )

    call.data = f"a:stock:{product.id}"
    await stock_card(call, session, actor)


@router.callback_query(F.data == "a:stock_confirm", StockSG.confirm)
async def commit_items(
    call: CallbackQuery,
    session: AsyncSession,
    actor: Actor,
    state: FSMContext,
    bot: Bot,
    **_: object,
) -> None:
    if not await guard(call, actor):
        return
    data = await state.get_data()
    items = data.get("items") or []
    product = await catalog_repo.get_product(session, int(data["product_id"]))
    if product is None or not items:
        await call.answer("Нечего добавлять", show_alert=True)
        await state.clear()
        return

    batch = await stock_repo.add_batch(session, product.id, items, actor.user_id)
    await audit_repo.record(
        session, actor.user_id, "stock.add", "product", product.id,
        {"batch_id": batch.id, "count": len(items)},
    )
    await state.clear()
    await call.answer(f"Добавлено {len(items)} шт")

    if await settings_store.get_bool(session, "restock_announce", True):
        await notify_service.notify_admins(
            bot,
            session,
            f"📥 Завоз: {html.escape(product.title)} +{len(items)} шт "
            f"(добавил <code>{actor.user_id}</code>)",
            exclude=actor.user_id,
        )

    call.data = f"a:stock:{product.id}"
    await stock_card(call, session, actor)


@router.callback_query(F.data.startswith("a:batch:"))
async def batch_card(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    batch_id = int(call.data.split(":")[2])
    batch = await stock_repo.get_batch(session, batch_id)
    if batch is None:
        await call.answer("Партия не найдена", show_alert=True)
        return
    product = await catalog_repo.get_product(session, batch.product_id)
    text = (
        f"📥 <b>Завоз #{batch.id}</b>\n\n"
        f"Товар: {html.escape(product.title if product else '—')}\n"
        f"Дата: {batch.created_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"Позиций залито: {batch.items_count}\n"
        f"Залил: <code>{batch.admin_id}</code>"
    )
    await show(call, text, admin_kb.batch_card(batch))


@router.callback_query(F.data.startswith("a:batch_reject:"))
async def ask_reject(
    call: CallbackQuery, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    batch_id = int(call.data.split(":")[2])
    await state.set_state(StockSG.reject_reason)
    await state.update_data(batch_id=batch_id)
    await show(
        call,
        "Опишите причину брака — она попадёт в журнал и в карточки позиций:",
        admin_kb.confirm("noop", f"a:batch:{batch_id}", yes_text="…"),
    )


@router.message(StockSG.reject_reason)
async def do_reject(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return
    reason = (message.text or "").strip() or "без причины"
    data = await state.get_data()
    batch_id = int(data["batch_id"])

    affected = await refunds_service.reject_batch(session, batch_id, actor.user_id, reason)
    await audit_repo.record(
        session, actor.user_id, "stock.reject_batch", "batch", batch_id,
        {"items": affected, "reason": reason},
    )
    await state.clear()
    await message.answer(
        f"🚫 Партия #{batch_id} забракована: снято с продажи {affected} позиций.\n"
        "Проданные позиции не тронуты — по ним делайте возврат или замену в заказе."
    )


@router.callback_query(F.data.startswith("a:stock_purge:"))
async def purge_defective(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    product_id = int(call.data.split(":")[2])
    removed = await stock_repo.purge_defective(session, product_id)
    await audit_repo.record(
        session, actor.user_id, "stock.purge_defective", "product", product_id, {"removed": removed}
    )
    await call.answer(f"Удалено бракованных: {removed}")
    call.data = f"a:stock:{product_id}"
    await stock_card(call, session, actor)
