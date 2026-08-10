"""Старт, главное меню, проверка подписки, сервисные команды."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User
from bot.keyboards import user as user_kb
from bot.services import header as header_service
from bot.services.access import Actor
from bot.services.settings_store import settings_store
from bot.services.subscription import SubscriptionService
from bot.services.texts import text_service
from bot.utils.fsm import soft_reset
from bot.utils.render import show

router = Router(name="user.start")


async def render_menu(
    event: Message | CallbackQuery, session: AsyncSession, actor: Actor
) -> None:
    text = await text_service.get(session, "shop_menu")
    photo = await header_service.photo(session)
    sent = await show(event, text, user_kb.main_menu(actor.is_admin), photo)
    if sent is not None:
        await header_service.remember(session, sent)


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    session: AsyncSession,
    user: User,
    actor: Actor,
    state: FSMContext,
    subscription: SubscriptionService,
    **_: object,
) -> None:
    await soft_reset(state)

    result = await subscription.check(session, message.bot, user.tg_id, use_cache=False)
    if not result.subscribed and not actor.is_admin:
        text = await text_service.get(session, "subscription_required")
        photo = await header_service.photo(session)
        await show(message, text, user_kb.subscription(result.missing), photo)
        return

    welcome = await text_service.get(session, "welcome", name=user.first_name or "друг")
    photo = await header_service.photo(session)
    sent = await show(message, welcome, user_kb.main_menu(actor.is_admin), photo)
    if sent is not None:
        await header_service.remember(session, sent)


@router.callback_query(F.data == "u:sub_check")
async def check_subscription(
    call: CallbackQuery,
    session: AsyncSession,
    user: User,
    actor: Actor,
    subscription: SubscriptionService,
    **_: object,
) -> None:
    # Кеш здесь намеренно не используется: человек только что подписался, и
    # ответить ему «всё ещё нет» из-за кеша — верный способ получить жалобу.
    await subscription.forget(user.tg_id)
    result = await subscription.check(session, call.bot, user.tg_id, use_cache=False)

    if not result.subscribed:
        text = await text_service.get(session, "subscription_failed")
        await call.answer(text[:200], show_alert=True)
        photo = await header_service.photo(session)
        await show(
            call,
            await text_service.get(session, "subscription_required"),
            user_kb.subscription(result.missing),
            photo,
        )
        return

    await call.answer(await text_service.get(session, "subscription_ok"))
    await render_menu(call, session, actor)


@router.callback_query(F.data == "u:menu")
async def open_menu(
    call: CallbackQuery, session: AsyncSession, actor: Actor, state: FSMContext, **_: object
) -> None:
    await soft_reset(state)
    await call.answer()
    await render_menu(call, session, actor)


@router.callback_query(F.data == "noop")
async def noop(call: CallbackQuery, **_: object) -> None:
    await call.answer()


@router.callback_query(F.data == "u:info")
async def open_info(call: CallbackQuery, session: AsyncSession, **_: object) -> None:
    await call.answer()
    support = await settings_store.get(session, "support_contact") or "@support"
    text = await text_service.get(session, "info", support=support)
    await show(call, text, user_kb.simple_back(), await header_service.photo(session))


@router.message(Command("terms"))
async def cmd_terms(message: Message, session: AsyncSession, **_: object) -> None:
    await message.answer(await text_service.get(session, "terms"))


@router.message(Command("paysupport", "support"))
async def cmd_paysupport(message: Message, session: AsyncSession, **_: object) -> None:
    support = await settings_store.get(session, "support_contact") or "@support"
    await message.answer(await text_service.get(session, "paysupport", support=support))


@router.message(Command("id"))
async def cmd_id(message: Message, user: User, **_: object) -> None:
    await message.answer(f"Ваш Telegram ID: <code>{user.tg_id}</code>")
