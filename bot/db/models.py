"""Модели базы.

Соглашения:

* деньги — целое число копеек в полях `*_kop`;
* статусы — короткие строки, а не MySQL ENUM: ENUM меняется только миграцией
  с перестроением таблицы, и добавление одного статуса становится событием;
* время — наивный UTC;
* каждая таблица явно объявляет `utf8mb4_unicode_ci`, иначе коллация
  разъезжается с родительской таблицей и внешний ключ падает на ошибке 3780.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.db.base import TABLE_ARGS, Base, utcnow


# --- Справочники статусов ---------------------------------------------------


class OrderStatus:
    NEW = "new"          # создан, способ оплаты ещё не выбран
    PENDING = "pending"  # выставлен счёт, ждём оплату
    PAID = "paid"        # оплата подтверждена, реквизиты ещё не запрошены

    # Оплачено, ждём от покупателя логин и пароль от аккаунта.
    AWAITING_CREDENTIALS = "awaiting_credentials"
    # Реквизиты получены и отправлены администраторам, работа идёт.
    IN_WORK = "in_work"

    DELIVERED = "delivered"  # администратор подтвердил выполнение
    CANCELED = "canceled"    # отменён пользователем или админом
    EXPIRED = "expired"      # истёк срок на оплату
    REFUNDED = "refunded"    # деньги возвращены

    OPEN = (NEW, PENDING)
    # Оплачено, но не закрыто. Такой заказ нельзя ни отменить по таймауту,
    # ни считать завершённым — он ждёт человека.
    ACTIVE = (PAID, AWAITING_CREDENTIALS, IN_WORK)
    FINAL = (DELIVERED, CANCELED, EXPIRED, REFUNDED)
    ALL = (
        NEW,
        PENDING,
        PAID,
        AWAITING_CREDENTIALS,
        IN_WORK,
        DELIVERED,
        CANCELED,
        EXPIRED,
        REFUNDED,
    )

    TITLES = {
        NEW: "🆕 Создан",
        PENDING: "⏳ Ждёт оплаты",
        PAID: "💰 Оплачен",
        AWAITING_CREDENTIALS: "🔑 Ждёт логин и пароль",
        IN_WORK: "🛠 В работе",
        DELIVERED: "✅ Выполнен",
        CANCELED: "🚫 Отменён",
        EXPIRED: "⌛ Истёк",
        REFUNDED: "↩️ Возврат",
    }




class CategoryAccent:
    """Цвет кнопок категории и её товаров.

    Telegram принимает у кнопки ровно четыре стиля — проверено пробой к Bot API,
    любое другое значение отклоняется целиком: «can't parse InlineKeyboardButton:
    invalid button style», и сообщение не уходит вообще. Произвольный цвет
    (`#FF5722`, `accent`, `warning`) задать нельзя — это ограничение Telegram,
    а не проекта.

    Поэтому «акцентный цвет» — это выбор из четырёх, а не палитра.
    """

    NEUTRAL = "default"  # серый
    BLUE = "primary"
    GREEN = "success"
    RED = "danger"

    ALL = (GREEN, BLUE, RED, NEUTRAL)
    DEFAULT = GREEN

    TITLES = {
        GREEN: "🟢 Зелёный",
        BLUE: "🔵 Синий",
        RED: "🔴 Красный",
        NEUTRAL: "⚪️ Серый",
    }

    @classmethod
    def normalize(cls, value: str | None) -> str:
        """Приводит значение из базы к допустимому стилю.

        Возврат к умолчанию, а не отказ: в базе может оказаться что угодно —
        старая строка, ручная правка, — и категория из-за этого не должна
        переставать открываться. Проверка стиля живёт в `theme.btn`, но она
        возбуждает исключение, а здесь нужен именно мягкий откат.
        """
        return value if value in cls.ALL else cls.DEFAULT


class BroadcastStatus:
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    CANCELED = "canceled"


class TargetStatus:
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    BLOCKED = "blocked"


# --- Пользователи и доступ --------------------------------------------------


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_created_at", "created_at"), TABLE_ARGS)

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    lang: Mapped[str] = mapped_column(String(8), default="ru", server_default="ru")


    # Заблокирован магазином (решение админа)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    block_reason: Mapped[str | None] = mapped_column(String(255))
    # Заблокировал бота у себя (узнаём из ошибки 403 при рассылке)
    has_blocked_bot: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def display(self) -> str:
        if self.username:
            return f"@{self.username}"
        name = " ".join(filter(None, [self.first_name, self.last_name])).strip()
        return name or str(self.tg_id)


class Admin(Base):
    __tablename__ = "admins"
    __table_args__ = (UniqueConstraint("user_id", name="uq_admins_user_id"), TABLE_ARGS)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --- Каталог ----------------------------------------------------------------


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (Index("ix_categories_sort_order", "sort_order"), TABLE_ARGS)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # Цвет кнопки категории и кнопок её товаров. Одно из четырёх значений,
    # которые принимает Telegram, — см. CategoryAccent.
    accent: Mapped[str] = mapped_column(
        String(16), default=CategoryAccent.DEFAULT, server_default=CategoryAccent.DEFAULT
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    products: Mapped[list["Product"]] = relationship(back_populates="category")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        Index("ix_products_category_id_sort_order", "category_id", "sort_order"),
        Index("ix_products_title", "title"),
        TABLE_ARGS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # RESTRICT намеренно: удаление категории с товарами должно быть осознанным
    # действием админа, а не тихим каскадом.
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # Цена в центах. Товар стоит в долларах, а покупатель платит рублями по
    # курсу ЦБ — рублёвой цены у товара нет и быть не может: она разная в
    # каждый момент времени. Рубли считаются на показе и замораживаются
    # в заказе.
    price_usd_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    image_path: Mapped[str | None] = mapped_column(String(512))
    image_file_id: Mapped[str | None] = mapped_column(String(255))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    category: Mapped[Category] = relationship(back_populates="products")


# --- Курс валют -------------------------------------------------------------


class ExchangeRate(Base):
    """История курса ЦБ.

    Строки не перезаписываются, а добавляются. Курс, по которому продали
    вчера, нужен, чтобы через месяц разобрать спор по старому заказу — а
    единственная перезаписываемая строка эту историю стирает.
    """

    __tablename__ = "exchange_rates"
    __table_args__ = (Index("ix_exchange_rates_code_fetched_at", "code", "fetched_at"), TABLE_ARGS)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(8), nullable=False)
    # Копеек за одну единицу валюты. Целое: курс участвует в расчёте денег.
    rate_kop: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="cbr", server_default="cbr")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --- Заказы -----------------------------------------------------------------


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_user_id_created_at", "user_id", "created_at"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_created_at", "created_at"),
        # Один идентификатор транзакции провайдера — не больше одного заказа.
        # MySQL допускает несколько NULL в уникальном индексе, поэтому
        # неоплаченные заказы друг другу не мешают.
        UniqueConstraint("provider", "provider_txn_id", name="uq_orders_provider_provider_txn_id"),
        # Токен показывается покупателю и называется вслух. Двух одинаковых
        # быть не должно: по нему администратор находит заказ.
        UniqueConstraint("token", name="uq_orders_token"),
        TABLE_ARGS,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Выдаётся в момент подтверждения оплаты. До оплаты токена нет — называть
    # покупателю номер, за который ещё не заплачено, незачем.
    token: Mapped[str | None] = mapped_column(String(16))

    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    # Снимок названия на момент покупки: товар могут переименовать или удалить,
    # а в истории покупок и в отчёте должно остаться то, что человек купил.
    product_title: Mapped[str] = mapped_column(String(255), nullable=False)

    # Снимок цены и курса на момент создания заказа. Пересчитывать оплаченный
    # заказ нельзя: курс меняется, пока покупатель ходит за деньгами, и
    # доплачивать разницу он не должен. Сверка суммы с провайдером идёт
    # по этим числам, а не по текущей цене товара.
    price_usd_cents: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    rate_kop: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    markup_pct: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Реквизиты аккаунта, присланные покупателем после оплаты.
    account_login: Mapped[str | None] = mapped_column(String(255))
    account_password: Mapped[str | None] = mapped_column(String(255))

    qty: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    unit_price_kop: Mapped[int] = mapped_column(BigInteger, nullable=False)
    subtotal_kop: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discount_kop: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    total_kop: Mapped[int] = mapped_column(BigInteger, nullable=False)

    promo_id: Mapped[int | None] = mapped_column(
        ForeignKey("promo_codes.id", ondelete="SET NULL")
    )
    promo_code: Mapped[str | None] = mapped_column(String(64))

    provider: Mapped[str | None] = mapped_column(String(32))
    payment_method: Mapped[str | None] = mapped_column(String(64))
    provider_txn_id: Mapped[str | None] = mapped_column(String(128))
    pay_url: Mapped[str | None] = mapped_column(Text)

    # 32, а не 16: `awaiting_credentials` — двадцать символов. Колонка на 16
    # обрезала бы статус молча, и заказ переставал бы находиться по нему.
    status: Mapped[str] = mapped_column(
        String(32), default=OrderStatus.NEW, server_default=OrderStatus.NEW
    )

    reserve_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Когда покупатель прислал логин и пароль.
    credentials_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Когда администратор подтвердил выполнение.
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    delivered_by: Mapped[int | None] = mapped_column(BigInteger)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime)
    admin_note: Mapped[str | None] = mapped_column(String(255))


class Payment(Base):
    """Журнал общения с платёжным провайдером.

    Хранит сырой ответ: при разборе спорного платежа пересказ бесполезен,
    нужен исходный текст.
    """

    __tablename__ = "payments"
    __table_args__ = (
        Index("ix_payments_provider_provider_txn_id", "provider", "provider_txn_id"),
        Index("ix_payments_order_id", "order_id"),
        TABLE_ARGS,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_txn_id: Mapped[str | None] = mapped_column(String(128))
    amount_kop: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="RUB", server_default="RUB")
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    raw: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --- Промокоды --------------------------------------------------------------


class PromoCode(Base):
    __tablename__ = "promo_codes"
    __table_args__ = (UniqueConstraint("code", name="uq_promo_codes_code"), TABLE_ARGS)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    discount_type: Mapped[str] = mapped_column(String(16), nullable=False)  # percent | fixed
    # Для процентной скидки — целые проценты, для фиксированной — копейки.
    discount_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime)
    usage_limit: Mapped[int | None] = mapped_column(Integer)
    used_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    per_user_limit: Mapped[int | None] = mapped_column(Integer, default=1, server_default="1")
    min_order_kop: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PromoScope(Base):
    """Ограничение области действия промокода.

    Нет ни одной строки — промокод действует на весь магазин.
    """

    __tablename__ = "promo_scopes"
    __table_args__ = (Index("ix_promo_scopes_promo_id", "promo_id"), TABLE_ARGS)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    promo_id: Mapped[int] = mapped_column(
        ForeignKey("promo_codes.id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))


class PromoUse(Base):
    __tablename__ = "promo_uses"
    __table_args__ = (
        # Один заказ не может списать промокод дважды, чем бы ни было вызвано
        # повторное подтверждение оплаты.
        UniqueConstraint("promo_id", "order_id", name="uq_promo_uses_promo_id_order_id"),
        Index("ix_promo_uses_promo_id_user_id", "promo_id", "user_id"),
        TABLE_ARGS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    promo_id: Mapped[int] = mapped_column(
        ForeignKey("promo_codes.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --- Баланс -----------------------------------------------------------------


class TextEntry(Base):
    """Тексты бота. Ключ — стабильный, значение правит владелец."""

    __tablename__ = "texts"
    __table_args__ = TABLE_ARGS

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    hint: Mapped[str | None] = mapped_column(String(255))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)


class SettingEntry(Base):
    __tablename__ = "settings"
    __table_args__ = TABLE_ARGS

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    hint: Mapped[str | None] = mapped_column(String(255))
    value_type: Mapped[str] = mapped_column(String(16), default="str", server_default="str")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)


class Channel(Base):
    """Канал, подписка на который обязательна."""

    __tablename__ = "channels"
    __table_args__ = (Index("ix_channels_sort_order", "sort_order"), TABLE_ARGS)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # То, что передаётся в getChatMember: «@name» или «-1001234567890».
    chat_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    invite_url: Mapped[str] = mapped_column(String(512), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # Последняя ошибка проверки: бот не админ, канал удалён и т. п.
    last_error: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --- Рассылки ---------------------------------------------------------------


class Broadcast(Base):
    __tablename__ = "broadcasts"
    __table_args__ = (Index("ix_broadcasts_status", "status"), TABLE_ARGS)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(String(16), default="text", server_default="text")
    text: Mapped[str | None] = mapped_column(Text)
    file_id: Mapped[str | None] = mapped_column(String(255))
    buttons: Mapped[list | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(
        String(16), default=BroadcastStatus.DRAFT, server_default=BroadcastStatus.DRAFT
    )
    total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    sent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    blocked: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class BroadcastTarget(Base):
    """Получатель рассылки.

    Отдельная строка на каждого нужна, чтобы рассылку можно было остановить и
    продолжить: после перезапуска бот не начинает с начала и не шлёт дубли.
    """

    __tablename__ = "broadcast_targets"
    __table_args__ = (
        UniqueConstraint("broadcast_id", "user_id", name="uq_broadcast_targets_broadcast_id_user_id"),
        Index("ix_broadcast_targets_broadcast_id_status", "broadcast_id", "status"),
        TABLE_ARGS,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    broadcast_id: Mapped[int] = mapped_column(
        ForeignKey("broadcasts.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=TargetStatus.PENDING, server_default=TargetStatus.PENDING
    )
    error: Mapped[str | None] = mapped_column(String(255))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_created_at", "created_at"), TABLE_ARGS)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
