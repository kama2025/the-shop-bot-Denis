"""Выдача товара.

Единственное место, где купленное попадает покупателю. Функция идемпотентна:
сколько бы раз её ни вызвали и в каком бы порядке ни пришли callback, нажатие
кнопки и опрос поллера — товар выдаётся один раз.

Три типа выдачи, задаются при создании товара:

* `text`   — позиция склада это текст: ссылка, ключ, логин с паролем;
* `file`   — позиция склада это файл: архив, документ, картинка;
* `manual` — склада нет, после оплаты администратор связывается с покупателем.

Тип берётся из **заказа**, а не из товара: товар могли переименовать или
переключить после продажи, а заказ обязан завершиться так, как был оформлен.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import utcnow
from bot.db.models import DeliveryType, Order, OrderStatus, StockItem, StockStatus
from bot.logger import payment_log
from bot.repo import orders as orders_repo
from bot.repo import stock as stock_repo


@dataclass(frozen=True)
class DeliveredItem:
    """Одна выданная позиция."""

    content: str
    file_id: str | None = None
    file_kind: str | None = None
    file_name: str | None = None

    @property
    def is_file(self) -> bool:
        return bool(self.file_id)


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    items: list[DeliveredItem]
    already_delivered: bool = False
    shortage: bool = False
    manual: bool = False

    @property
    def contents(self) -> list[str]:
        return [item.content for item in self.items]


def _to_item(row: StockItem) -> DeliveredItem:
    return DeliveredItem(
        content=row.content,
        file_id=row.file_id,
        file_kind=row.file_kind,
        file_name=row.file_name,
    )


async def deliver(session: AsyncSession, order: Order) -> DeliveryResult:
    """Завершает оплаченный заказ.

    Вызывающий обязан держать заказ под блокировкой строки
    (`orders_repo.get_for_update`) — иначе идемпотентность держится на удаче.
    """
    if order.status == OrderStatus.DELIVERED:
        return DeliveryResult(
            ok=True, items=await items_of(session, order.id), already_delivered=True
        )

    if order.delivery_type == DeliveryType.MANUAL:
        return await _await_manual(session, order)

    if order.status not in (OrderStatus.PAID, OrderStatus.DELIVERED):
        return DeliveryResult(ok=False, items=[])

    rows = [
        row
        for row in await stock_repo.items_of_order(session, order.id)
        if row.status in (StockStatus.RESERVED, StockStatus.SOLD)
    ]

    if len(rows) < order.qty:
        # Оплата прошла, а склада не хватило: позиции могли забраковать между
        # резервом и оплатой. Молчать нельзя — и покупателю, и админам нужно
        # узнать об этом сразу.
        payment_log.error(
            "Не хватает позиций для выдачи",
            extra={
                "order_id": order.id,
                "user_id": order.user_id,
                "need": order.qty,
                "have": len(rows),
            },
        )
        return DeliveryResult(ok=False, items=[], shortage=True)

    await stock_repo.mark_sold(session, order.id)
    for row in rows:
        if row.sold_at is None:
            row.sold_at = utcnow()

    existing = {link.stock_item_id for link in await orders_repo.items_of(session, order.id)}
    fresh = [row.id for row in rows if row.id not in existing]
    if fresh:
        await orders_repo.add_items(session, order.id, fresh)

    order.status = OrderStatus.DELIVERED
    order.delivered_at = utcnow()
    await session.flush()

    payment_log.info(
        "Товар выдан",
        extra={
            "order_id": order.id,
            "user_id": order.user_id,
            "qty": order.qty,
            "type": order.delivery_type,
            "total_kop": order.total_kop,
        },
    )
    return DeliveryResult(ok=True, items=[_to_item(row) for row in rows])


async def _await_manual(session: AsyncSession, order: Order) -> DeliveryResult:
    """Ставит заказ в очередь на ручную выдачу.

    Заказ не считается выданным: он висит в `awaiting`, пока администратор не
    отправит покупателю то, что тот купил. Помечать такой заказ выданным сразу
    после оплаты нельзя — тогда он потеряется среди завершённых, и человек
    останется без товара.
    """
    if order.status == OrderStatus.AWAITING:
        return DeliveryResult(ok=True, items=[], manual=True, already_delivered=True)

    order.status = OrderStatus.AWAITING
    await session.flush()
    payment_log.info(
        "Заказ ждёт ручной выдачи",
        extra={
            "order_id": order.id,
            "user_id": order.user_id,
            "product": order.product_title,
            "qty": order.qty,
        },
    )
    return DeliveryResult(ok=True, items=[], manual=True)


async def complete_manual(
    session: AsyncSession,
    order: Order,
    admin_id: int,
    item: DeliveredItem,
) -> DeliveryResult:
    """Закрывает заказ ручной выдачи тем, что администратор отправил покупателю.

    Отправленное записывается позицией склада со статусом «продана»: история
    покупок и карточка заказа тогда работают одинаково для всех типов, и
    «что именно выдали» можно посмотреть спустя месяц.
    """
    if order.status == OrderStatus.DELIVERED:
        return DeliveryResult(
            ok=True, items=await items_of(session, order.id), already_delivered=True
        )
    if order.status not in (OrderStatus.AWAITING, OrderStatus.PAID):
        return DeliveryResult(ok=False, items=[])

    row = StockItem(
        product_id=order.product_id,
        content=item.content,
        file_id=item.file_id,
        file_kind=item.file_kind,
        file_name=item.file_name,
        status=StockStatus.SOLD,
        order_id=order.id,
        sold_at=utcnow(),
    )
    session.add(row)
    await session.flush()
    await orders_repo.add_items(session, order.id, [row.id])

    order.status = OrderStatus.DELIVERED
    order.delivered_at = utcnow()
    await session.flush()

    payment_log.info(
        "Ручная выдача завершена",
        extra={"order_id": order.id, "user_id": order.user_id, "admin_id": admin_id},
    )
    return DeliveryResult(ok=True, items=[_to_item(row)])


async def items_of(session: AsyncSession, order_id: int) -> list[DeliveredItem]:
    """Что было выдано по заказу — для истории покупок и карточки в админке."""
    result: list[DeliveredItem] = []
    for link in await orders_repo.items_of(session, order_id):
        row = await session.get(StockItem, link.stock_item_id)
        if row is not None:
            result.append(_to_item(row))
    return result


async def contents_of(session: AsyncSession, order_id: int) -> list[str]:
    return [item.content for item in await items_of(session, order_id)]


def format_items(items: list[DeliveredItem]) -> str:
    """Оформляет выданное в сообщение.

    Текст каждой позиции отдельным блоком `<code>`: так его удобно скопировать
    одним касанием, и Telegram не превращает ссылку в предпросмотр. Файлы
    отправляются отдельными сообщениями, здесь они только перечислены.
    """
    if not items:
        return "—"

    blocks: list[str] = []
    for index, item in enumerate(items, start=1):
        prefix = f"<b>{index}.</b> " if len(items) > 1 else ""
        if item.is_file:
            name = _escape(item.file_name or "файл")
            note = f"\n<code>{_escape(item.content)}</code>" if item.content.strip() else ""
            blocks.append(f"{prefix}📎 {name} — отправлен отдельным сообщением{note}")
        else:
            body = f"<code>{_escape(item.content)}</code>"
            blocks.append(f"{prefix}\n{body}" if prefix else body)
    return "\n\n".join(blocks)


def format_contents(contents: list[str]) -> str:
    """Совместимость со старым вызовом: список строк без файлов."""
    return format_items([DeliveredItem(content=text) for text in contents])


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
