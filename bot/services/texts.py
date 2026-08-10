"""Тексты бота.

Все тексты живут в базе и правятся владельцем через админку. Здесь — только
значения по умолчанию для первичного наполнения и механизм подстановки.

Подстановка нарочно не использует `str.format`: владелец однажды напишет в
тексте фигурную скобку, и `format` уронит хендлер. Неизвестный placeholder
остаётся в тексте как есть.
"""

from __future__ import annotations

import re
import time

from sqlalchemy.ext.asyncio import AsyncSession

from bot.repo import content as content_repo

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_CACHE_TTL_SECONDS = 30


def render(template: str, **values: object) -> str:
    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return str(values[key])
        return match.group(0)

    return _PLACEHOLDER.sub(substitute, template)


DEFAULT_TEXTS: dict[str, dict] = {
    "welcome": {
        "title": "Приветствие",
        "hint": "Первое сообщение после /start. Доступно: {name}",
        "value": (
            "🤍 <b>Добро пожаловать!</b>\n\n"
            "Здесь вы можете быстро и безопасно купить нужный товар.\n"
            "Выберите раздел ниже 👇"
        ),
    },
    "shop_menu": {
        "title": "Заголовок главного меню",
        "hint": "Показывается при возврате в главное меню",
        "value": "🏠 <b>Главное меню</b>\n\nВыберите раздел ниже 👇",
    },
    "subscription_required": {
        "title": "Требуется подписка",
        "hint": "Показывается, пока пользователь не подписан на каналы",
        "value": (
            "🔒 <b>Для доступа подпишитесь на наш канал</b>, затем нажмите «Проверить подписку»."
        ),
    },
    "subscription_failed": {
        "title": "Подписка не найдена",
        "hint": "Ответ на «Проверить подписку», если подписки всё ещё нет",
        "value": "❌ Подписка не найдена. Подпишитесь и нажмите «Проверить подписку» ещё раз.",
    },
    "subscription_ok": {
        "title": "Подписка подтверждена",
        "value": "✅ Спасибо за подписку! Магазин открыт.",
    },
    "catalog_title": {
        "title": "Заголовок каталога",
        "value": "📂 <b>Все категории</b>\n\nВыберите категорию:",
    },
    "catalog_empty": {
        "title": "Каталог пуст",
        "value": "Каталог пока пуст. Загляните позже 🙌",
    },
    "category_title": {
        "title": "Заголовок категории",
        "hint": "Доступно: {category}",
        "value": "🗂 <b>Категория:</b> {category}",
    },
    "category_empty": {
        "title": "Пустая категория",
        "value": "В этой категории пока нет товаров.",
    },
    "product_card": {
        "title": "Карточка товара",
        "hint": "Доступно: {title}, {description}, {price}, {stock}, {qty}, {total}",
        "value": (
            "🧾 <b>{title}</b>\n\n"
            "💲 <b>Цена:</b> {price}\n"
            "📦 <b>В наличии:</b> {stock} шт.\n\n"
            "{description}\n\n"
            "🧮 Выбрано: <b>{qty} шт.</b>\n"
            "💰 К оплате: <b>{total}</b>\n\n"
            "Выберите количество:"
        ),
    },
    "product_out_of_stock": {
        "title": "Товара нет в наличии",
        "value": "😔 Товара сейчас нет в наличии. Мы сообщим о завозе в канале.",
    },
    "stock_shortage": {
        "title": "Не хватает на складе",
        "hint": "Доступно: {available}",
        "value": "😔 В наличии осталось только <b>{available} шт.</b> Уменьшите количество.",
    },
    "availability_title": {
        "title": "Раздел «Наличие»",
        "value": "📊 <b>Наличие товаров</b>",
    },
    "order_summary": {
        "title": "Оформление заказа",
        "hint": "Доступно: {order_id}, {title}, {qty}, {subtotal}, {discount}, {total}, {promo}",
        "value": (
            "🧾 <b>Оформление заказа #{order_id}</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📦 <b>Товар:</b> {title}\n"
            "🔢 <b>Количество:</b> {qty} шт.\n"
            "💵 <b>Сумма:</b> {subtotal}\n"
            "{discount}"
            "━━━━━━━━━━━━━━━━━━\n"
            "💰 <b>К оплате: {total}</b>\n\n"
            "Выберите способ оплаты:"
        ),
    },
    "order_discount_line": {
        "title": "Строка скидки в заказе",
        "hint": "Доступно: {discount}, {promo}",
        "value": "🎟 <b>Скидка ({promo}):</b> −{discount}\n",
    },
    "payment_created": {
        "title": "Счёт выставлен",
        "hint": "Доступно: {order_id}, {total}, {minutes}",
        "value": (
            "💳 <b>Заказ #{order_id}</b>\n"
            "💰 К оплате: <b>{total}</b>\n\n"
            "Нажмите «Оплатить», а после оплаты — «Проверить оплату».\n"
            "⏳ Счёт действует {minutes} мин."
        ),
    },
    "payment_not_confirmed": {
        "title": "Оплата не подтверждена",
        "value": (
            "⏳ Оплата пока не подтверждена.\n"
            "Если вы только что заплатили — подождите минуту и нажмите ещё раз."
        ),
    },
    "payment_canceled": {
        "title": "Платёж отменён",
        "value": "🚫 Платёж отменён. Товар не списан, можно оформить заказ заново.",
    },
    "payment_expired": {
        "title": "Счёт истёк",
        "value": "⌛ Время оплаты истекло, заказ отменён. Оформите новый — товар вернулся в продажу.",
    },
    "delivery": {
        "title": "Выдача товара",
        "hint": "Доступно: {order_id}, {title}, {qty}, {items}",
        "value": (
            "✅ <b>Оплата получена. Заказ #{order_id}</b>\n\n"
            "📦 <b>{title}</b> — {qty} шт.\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "{items}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Спасибо за покупку! 🤍"
        ),
    },
    "delivery_manual": {
        "title": "Товар выдаёт администратор",
        "hint": "Показывается после оплаты товара с ручной выдачей. Доступно: {order_id}, {title}, {qty}, {support}",
        "value": (
            "✅ <b>Оплата получена. Заказ #{order_id}</b>\n\n"
            "📦 <b>{title}</b> — {qty} шт.\n\n"
            "🙋 Этот товар выдаёт администратор вручную — он уже получил "
            "уведомление и свяжется с вами в ближайшее время.\n"
            "Если ответа долго нет, напишите {support} и укажите номер заказа."
        ),
    },
    "delivery_manual_done": {
        "title": "Ручная выдача завершена",
        "hint": "Доступно: {order_id}, {title}",
        "value": (
            "📦 <b>Заказ #{order_id}</b> — {title}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "{items}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Спасибо за покупку! 🤍"
        ),
    },
    "delivery_shortage": {
        "title": "Оплата прошла, товара нет",
        "hint": "Показывается, если склад опустел между оплатой и выдачей",
        "value": (
            "⚠️ Оплата получена, но выдать товар автоматически не удалось.\n"
            "Администратор уже уведомлён и свяжется с вами. "
            "Деньги в любом случае вернутся вам на баланс."
        ),
    },
    "profile": {
        "title": "Профиль",
        "hint": "Доступно: {name}, {user_id}, {balance}, {orders}, {spent}, {since}",
        "value": (
            "👤 <b>Профиль</b>\n\n"
            "🆔 <b>ID:</b> <code>{user_id}</code>\n"
            "💼 <b>Баланс:</b> {balance}\n"
            "🛒 <b>Покупок:</b> {orders}\n"
            "💸 <b>Потрачено:</b> {spent}\n"
            "📅 <b>С нами с:</b> {since}"
        ),
    },
    "purchases_empty": {
        "title": "Нет покупок",
        "value": "Здесь пока пусто. Ваши покупки появятся после первой оплаты.",
    },
    "promo_prompt": {
        "title": "Запрос промокода",
        "value": "🎟 Отправьте промокод одним сообщением.",
    },
    "promo_applied": {
        "title": "Промокод принят",
        "hint": "Доступно: {code}, {discount}",
        "value": "✅ Промокод <b>{code}</b> принят. Скидка: {discount}.",
    },
    "promo_invalid": {
        "title": "Промокод не найден",
        "value": "❌ Такого промокода нет или он больше не действует.",
    },
    "promo_expired": {"title": "Промокод истёк", "value": "❌ Срок действия промокода истёк."},
    "promo_exhausted": {
        "title": "Промокод исчерпан",
        "value": "❌ Промокод исчерпал лимит использований.",
    },
    "promo_already_used": {
        "title": "Промокод уже использован",
        "value": "❌ Вы уже использовали этот промокод.",
    },
    "promo_min_order": {
        "title": "Промокод: мало сумма",
        "hint": "Доступно: {min_order}",
        "value": "❌ Промокод действует от суммы {min_order}.",
    },
    "promo_wrong_scope": {
        "title": "Промокод: не тот товар",
        "value": "❌ Промокод не действует на этот товар.",
    },
    "promo_cleared": {"title": "Промокод снят", "value": "🎟 Промокод снят."},
    "topup_prompt": {
        "title": "Пополнение баланса",
        "hint": "Доступно: {min_amount}",
        "value": "💼 Отправьте сумму пополнения в рублях (минимум {min_amount}).",
    },
    "search_prompt": {
        "title": "Запрос поиска",
        "value": "🔎 Отправьте название товара или его часть.",
    },
    "search_empty": {
        "title": "Поиск: ничего не найдено",
        "hint": "Доступно: {query}",
        "value": "По запросу «{query}» ничего не нашлось.",
    },
    "info": {
        "title": "Раздел «Информация»",
        "value": (
            "ℹ️ <b>Информация</b>\n\n"
            "• Товар выдаётся автоматически сразу после подтверждения оплаты.\n"
            "• Гарантия действует 2 часа с момента выдачи.\n"
            "• По вопросам обращайтесь в поддержку.\n\n"
            "Команды: /terms — условия, /paysupport — вопросы по оплате."
        ),
    },
    "terms": {
        "title": "Условия (/terms)",
        "value": (
            "📄 <b>Условия</b>\n\n"
            "1. Товар — цифровой, выдаётся автоматически после подтверждения оплаты.\n"
            "2. Гарантия действует 2 часа с момента выдачи.\n"
            "3. Возврат производится на внутренний баланс.\n"
            "4. Продавец не несёт ответственности за нарушение покупателем правил "
            "сервисов, к которым относится товар."
        ),
    },
    "paysupport": {
        "title": "Поддержка по оплате (/paysupport)",
        "hint": "Доступно: {support}",
        "value": (
            "💬 <b>Вопросы по оплате</b>\n\n"
            "Если оплата прошла, но товар не пришёл — нажмите «Проверить оплату» в заказе.\n"
            "Если не помогло, напишите {support} и укажите номер заказа."
        ),
    },
    "user_blocked": {
        "title": "Пользователь заблокирован",
        "value": "🚫 Доступ к магазину закрыт. По вопросам обратитесь в поддержку.",
    },
    "shop_closed": {
        "title": "Магазин закрыт",
        "hint": "Показывается, когда в настройках включён режим обслуживания",
        "value": "🛠 Магазин временно закрыт на обслуживание. Загляните чуть позже.",
    },
    "error_generic": {
        "title": "Общая ошибка",
        "value": "😔 Что-то пошло не так. Попробуйте ещё раз или напишите в поддержку.",
    },
}


class TextService:
    """Кеширует тексты в процессе, чтобы не ходить в базу на каждое сообщение.

    Кеш короткий и сбрасывается явно при правке из админки: владелец, изменивший
    текст, должен увидеть результат сразу, иначе он решит, что кнопка не работает.
    """

    def __init__(self, ttl_seconds: int = _CACHE_TTL_SECONDS) -> None:
        self._cache: dict[str, str] = {}
        self._loaded_at: float = 0.0
        self._ttl = ttl_seconds

    def invalidate(self) -> None:
        self._loaded_at = 0.0

    async def _ensure(self, session: AsyncSession) -> None:
        if self._cache and (time.monotonic() - self._loaded_at) < self._ttl:
            return
        self._cache = await content_repo.texts_map(session)
        self._loaded_at = time.monotonic()

    async def get(self, session: AsyncSession, key: str, **values: object) -> str:
        await self._ensure(session)
        template = self._cache.get(key)
        if template is None:
            default = DEFAULT_TEXTS.get(key)
            template = default["value"] if default else key
        return render(template, **values)


text_service = TextService()


async def seed(session: AsyncSession) -> int:
    added = await content_repo.seed_texts(session, DEFAULT_TEXTS)
    if added:
        text_service.invalidate()
    return added
