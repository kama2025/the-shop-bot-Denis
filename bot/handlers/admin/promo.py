"""Админка: промокоды."""

from __future__ import annotations

import html
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.admin.common import guard
from bot.keyboards import admin as admin_kb
from bot.keyboards.theme import DANGER, SUCCESS, btn
from bot.repo import audit as audit_repo
from bot.repo import catalog as catalog_repo
from bot.repo import promo as promo_repo
from bot.services import promo as promo_service
from bot.services.access import Actor
from bot.states.admin import PromoSG
from bot.utils.money import (
    DISCOUNT_FIXED,
    DISCOUNT_PERCENT,
    PriceParseError,
    format_kop,
    parse_price_to_kop,
)
from bot.utils.pagination import paginate
from bot.utils.render import show

router = Router(name="admin.promo")

PER_PAGE = 8


@router.callback_query(F.data.startswith("a:promos:"))
async def list_promos(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    page = int(call.data.split(":")[2])
    items = await promo_repo.list_all(session)
    chunk = paginate(items, page, PER_PAGE)
    text = f"🎟 <b>Промокоды</b> — всего {chunk.total}"
    if chunk.is_empty:
        text += "\n\nПока ни одного."
    await show(call, text, admin_kb.promos(chunk.items, chunk.page, chunk.pages))


@router.callback_query(F.data.startswith("a:promo:"))
async def promo_card(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    promo_id = int(call.data.split(":")[2])
    await _render(call, session, promo_id)


async def _render(event, session: AsyncSession, promo_id: int) -> None:
    promo = await promo_repo.get(session, promo_id)
    if promo is None:
        await show(event, "Промокод удалён.", admin_kb.confirm("noop", "a:promos:0", yes_text="…"))
        return

    scopes = await promo_repo.scopes_of(session, promo_id)
    if not scopes:
        scope_text = "весь магазин"
    else:
        names: list[str] = []
        for scope in scopes:
            if scope.category_id:
                category = await catalog_repo.get_category(session, scope.category_id)
                names.append(f"категория «{category.title}»" if category else "категория (удалена)")
            if scope.product_id:
                product = await catalog_repo.get_product(session, scope.product_id)
                names.append(f"товар «{product.title}»" if product else "товар (удалён)")
        scope_text = ", ".join(names)

    limit = promo.usage_limit if promo.usage_limit is not None else "∞"
    per_user = promo.per_user_limit if promo.per_user_limit is not None else "∞"
    until = promo.valid_until.strftime("%d.%m.%Y %H:%M") if promo.valid_until else "бессрочно"

    text = (
        f"🎟 <b>{html.escape(promo.code)}</b>\n\n"
        f"Скидка: <b>{promo_service.describe_discount(promo)}</b>\n"
        f"Использований: {promo.used_count} из {limit}\n"
        f"На пользователя: {per_user}\n"
        f"Минимальная сумма: {format_kop(promo.min_order_kop)}\n"
        f"Действует до: {until}\n"
        f"Область: {html.escape(scope_text)}\n"
        f"Статус: {'🟢 активен' if promo.is_active else '🔴 выключен'}"
    )
    await show(event, text, admin_kb.promo_card(promo))


# --- создание ---------------------------------------------------------------


@router.callback_query(F.data == "a:promo_add")
async def ask_code(call: CallbackQuery, actor: Actor, state: FSMContext, **_: object) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    await state.set_state(PromoSG.code)
    await show(
        call,
        "Отправьте код промокода (латиница и цифры, например <code>PROMO2026</code>):",
        admin_kb.confirm("noop", "a:promos:0", yes_text="…"),
    )


@router.message(PromoSG.code)
async def got_code(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return
    code = promo_repo.normalize_code(message.text or "")
    if not 2 <= len(code) <= 64 or not code.replace("_", "").replace("-", "").isalnum():
        await message.answer("Код должен быть 2–64 символа, только буквы, цифры, «-» и «_».")
        return
    if await promo_repo.get_by_code(session, code) is not None:
        await message.answer("Такой промокод уже есть. Придумайте другой.")
        return

    await state.update_data(code=code)
    await state.set_state(PromoSG.discount_type)
    await message.answer(
        f"Код: <b>{html.escape(code)}</b>\nКакая скидка?",
        reply_markup=admin_kb.kb(
            [
                [btn("📊 Процент", callback_data="a:promo_type:percent", style=SUCCESS)],
                [btn("💵 Фиксированная сумма", callback_data="a:promo_type:fixed", style=SUCCESS)],
                [btn("❌ Отмена", callback_data="a:promos:0", style=DANGER)],
            ]
        ),
    )


@router.callback_query(F.data.startswith("a:promo_type:"), PromoSG.discount_type)
async def got_type(call: CallbackQuery, actor: Actor, state: FSMContext, **_: object) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    kind = call.data.split(":")[2]
    discount_type = DISCOUNT_PERCENT if kind == "percent" else DISCOUNT_FIXED
    await state.update_data(discount_type=discount_type)
    await state.set_state(PromoSG.discount_value)
    prompt = (
        "Отправьте размер скидки в процентах (например 10):"
        if discount_type == DISCOUNT_PERCENT
        else "Отправьте размер скидки в рублях (например 100):"
    )
    await show(call, prompt, admin_kb.confirm("noop", "a:promos:0", yes_text="…"))


@router.message(PromoSG.discount_value)
async def got_value(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return
    data = await state.get_data()
    discount_type = data["discount_type"]
    value = await _parse_discount(message, discount_type)
    if value is None:
        return

    promo = await promo_repo.create(session, data["code"], discount_type, value, actor.user_id)
    await audit_repo.record(
        session, actor.user_id, "promo.create", "promo", promo.id,
        {"code": promo.code, "type": discount_type, "value": value},
    )
    await state.clear()
    await message.answer(
        f"✅ Промокод <b>{html.escape(promo.code)}</b> создан "
        f"({promo_service.describe_discount(promo)}).\n"
        "По умолчанию: без срока, без общего лимита, 1 использование на пользователя, "
        "действует на весь магазин. Настройте в карточке."
    )
    await _render(message, session, promo.id)


async def _parse_discount(message: Message, discount_type: str) -> int | None:
    raw = (message.text or "").strip()
    if discount_type == DISCOUNT_PERCENT:
        if not raw.replace("%", "").strip().isdigit():
            await message.answer("Нужно целое число процентов, например 10.")
            return None
        percent = int(raw.replace("%", "").strip())
        if not 1 <= percent <= 100:
            await message.answer("Процент должен быть от 1 до 100.")
            return None
        return percent
    try:
        kop = parse_price_to_kop(raw)
    except PriceParseError as exc:
        await message.answer(f"Не понял сумму: {exc}")
        return None
    if kop <= 0:
        await message.answer("Сумма должна быть больше нуля.")
        return None
    return kop


# --- правка -----------------------------------------------------------------


EDIT_PROMPTS = {
    "value": ("Отправьте новый размер скидки:", PromoSG.edit_value),
    "limit": ("Общий лимит использований (число или «-» для безлимита):", PromoSG.edit_limit),
    "per_user": ("Лимит на одного пользователя (число или «-» для безлимита):", PromoSG.edit_per_user),
    "min_order": ("Минимальная сумма заказа в рублях (0 — без ограничения):", PromoSG.edit_min_order),
    "until": ("Дата окончания в формате ДД.ММ.ГГГГ (или «-» — бессрочно):", PromoSG.edit_until),
}


@router.callback_query(F.data.startswith("a:promo_edit:"))
async def ask_edit(call: CallbackQuery, actor: Actor, state: FSMContext, **_: object) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    _, _, promo_id, field = call.data.split(":", 3)
    prompt, target = EDIT_PROMPTS[field]
    await state.update_data(promo_id=int(promo_id))
    await state.set_state(target)
    await show(call, prompt, admin_kb.confirm("noop", f"a:promo:{promo_id}", yes_text="…"))


async def _current(session: AsyncSession, state: FSMContext):
    data = await state.get_data()
    promo_id = data.get("promo_id")
    return await promo_repo.get(session, int(promo_id)) if promo_id else None


@router.message(PromoSG.edit_value)
async def save_value(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return
    promo = await _current(session, state)
    if promo is None:
        await message.answer("Промокод не найден.")
        await state.clear()
        return
    value = await _parse_discount(message, promo.discount_type)
    if value is None:
        return
    promo.discount_value = value
    await audit_repo.record(session, actor.user_id, "promo.value", "promo", promo.id, {"value": value})
    await state.clear()
    await _render(message, session, promo.id)


@router.message(PromoSG.edit_limit)
async def save_limit(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return
    promo = await _current(session, state)
    if promo is None:
        await state.clear()
        return
    raw = (message.text or "").strip()
    if raw == "-":
        promo.usage_limit = None
    elif raw.isdigit() and int(raw) > 0:
        if int(raw) < promo.used_count:
            await message.answer(
                f"Лимит меньше уже сделанных использований ({promo.used_count}). "
                "Такой лимит сразу закроет промокод — если так и задумано, укажите его ещё раз."
            )
        promo.usage_limit = int(raw)
    else:
        await message.answer("Нужно положительное число или «-».")
        return
    await state.clear()
    await _render(message, session, promo.id)


@router.message(PromoSG.edit_per_user)
async def save_per_user(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return
    promo = await _current(session, state)
    if promo is None:
        await state.clear()
        return
    raw = (message.text or "").strip()
    if raw == "-":
        promo.per_user_limit = None
    elif raw.isdigit() and int(raw) > 0:
        promo.per_user_limit = int(raw)
    else:
        await message.answer("Нужно положительное число или «-».")
        return
    await state.clear()
    await _render(message, session, promo.id)


@router.message(PromoSG.edit_min_order)
async def save_min_order(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return
    promo = await _current(session, state)
    if promo is None:
        await state.clear()
        return
    try:
        promo.min_order_kop = parse_price_to_kop(message.text or "0")
    except PriceParseError as exc:
        await message.answer(f"Не понял сумму: {exc}")
        return
    await state.clear()
    await _render(message, session, promo.id)


@router.message(PromoSG.edit_until)
async def save_until(
    message: Message, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    if not await guard(message, actor):
        await state.clear()
        return
    promo = await _current(session, state)
    if promo is None:
        await state.clear()
        return
    raw = (message.text or "").strip()
    if raw == "-":
        promo.valid_until = None
    else:
        try:
            promo.valid_until = datetime.strptime(raw, "%d.%m.%Y").replace(hour=23, minute=59)
        except ValueError:
            await message.answer("Формат даты: ДД.ММ.ГГГГ, например 31.12.2026.")
            return
    await state.clear()
    await _render(message, session, promo.id)


# --- действия ---------------------------------------------------------------


@router.callback_query(F.data.startswith("a:promo_toggle:"))
async def toggle(call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object) -> None:
    if not await guard(call, actor):
        return
    promo_id = int(call.data.split(":")[2])
    promo = await promo_repo.get(session, promo_id)
    if promo is None:
        await call.answer("Промокод удалён", show_alert=True)
        return
    promo.is_active = not promo.is_active
    await audit_repo.record(
        session, actor.user_id, "promo.toggle", "promo", promo_id, {"is_active": promo.is_active}
    )
    await call.answer("🟢 Включён" if promo.is_active else "🔴 Выключен")
    await _render(call, session, promo_id)


@router.callback_query(F.data.startswith("a:promo_stats:"))
async def promo_stats(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    promo_id = int(call.data.split(":")[2])
    promo = await promo_repo.get(session, promo_id)
    stats = await promo_repo.usage_stats(session, promo_id)
    text = (
        f"📈 <b>Статистика {html.escape(promo.code if promo else '')}</b>\n\n"
        f"Использований: {stats['uses']}\n"
        f"Уникальных покупателей: {stats['users']}\n"
        f"Выручка с промокодом: {format_kop(stats['revenue_kop'])}\n"
        f"Сумма скидок: {format_kop(stats['discount_kop'])}"
    )
    await show(call, text, admin_kb.confirm("noop", f"a:promo:{promo_id}", yes_text="…"))


@router.callback_query(F.data.startswith("a:promo_scope:"))
async def ask_scope(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    promo_id = int(call.data.split(":")[2])
    categories = await catalog_repo.list_categories(session, only_active=False)
    await show(
        call,
        "🎯 На что действует промокод?\n\n«Весь магазин» снимает все ограничения.",
        admin_kb.promo_scope(promo_id, categories),
    )


@router.callback_query(F.data.startswith("a:promo_scope_all:"))
async def scope_all(call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object) -> None:
    if not await guard(call, actor):
        return
    promo_id = int(call.data.split(":")[2])
    await promo_repo.clear_scopes(session, promo_id)
    await call.answer("Действует на весь магазин")
    await _render(call, session, promo_id)


@router.callback_query(F.data.startswith("a:promo_scope_cat:"))
async def scope_category(
    call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object
) -> None:
    if not await guard(call, actor):
        return
    parts = call.data.split(":")
    promo_id, category_id = int(parts[2]), int(parts[3])
    await promo_repo.clear_scopes(session, promo_id)
    await promo_repo.set_scope(session, promo_id, category_id=category_id)
    await call.answer("Область обновлена")
    await _render(call, session, promo_id)


@router.callback_query(F.data.startswith("a:promo_del:"))
async def ask_delete(call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object) -> None:
    if not await guard(call, actor):
        return
    await call.answer()
    promo_id = int(call.data.split(":")[2])
    promo = await promo_repo.get(session, promo_id)
    if promo is None:
        await call.answer("Промокод удалён", show_alert=True)
        return
    stats = await promo_repo.usage_stats(session, promo_id)
    text = (
        f"Удалить промокод <b>{html.escape(promo.code)}</b>?\n\n"
        f"Использований: {stats['uses']}. История применения в заказах сохранится, "
        "но статистика по промокоду пропадёт.\n\n"
        "Если нужно просто перестать его принимать — выключите."
    )
    await show(call, text, admin_kb.confirm(f"a:promo_del_ok:{promo_id}", f"a:promo:{promo_id}"))


@router.callback_query(F.data.startswith("a:promo_del_ok:"))
async def do_delete(call: CallbackQuery, session: AsyncSession, actor: Actor, **_: object) -> None:
    if not await guard(call, actor):
        return
    promo_id = int(call.data.split(":")[2])
    await promo_repo.remove(session, promo_id)
    await audit_repo.record(session, actor.user_id, "promo.delete", "promo", promo_id)
    await call.answer("Промокод удалён")
    call.data = "a:promos:0"
    await list_promos(call, session, actor)
