"""магазин становится сервисом: цена в долларах, токен заказа, реквизиты

Убирает склад и типы выдачи, объединяет роли администраторов, вводит курс
валют и снимки цены в заказе.

Revision ID: b7e4c9a10f32
Revises: c1d507b48429
Create Date: 2026-08-15
"""
from __future__ import annotations

import secrets
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e4c9a10f32"
down_revision: str | None = "c1d507b48429"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


BACKFILL_RATE_KOP = 9000
"""Курс для переноса старых цен: 90,00 ₽ за доллар.

Число объявлено здесь, а не берётся из ЦБ, ровно по одной причине: миграция
обязана давать один и тот же результат при каждом прогоне, иначе откат и
повторный накат разъедутся, а `check-migrations.sh` перестанет что-либо
доказывать. Боевых данных на момент миграции нет — база живёт только на
машине разработчика, — поэтому точность переноса значения не имеет,
а воспроизводимость имеет.
"""

TOKEN_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def _token() -> str:
    raw = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def upgrade() -> None:
    # --- курс валют ---------------------------------------------------------
    op.create_table(
        "exchange_rates",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=8), nullable=False),
        sa.Column("rate_kop", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=32), server_default="cbr", nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_exchange_rates_code_fetched_at", "exchange_rates", ["code", "fetched_at"]
    )

    # --- товар: цена в центах ----------------------------------------------
    op.add_column(
        "products",
        sa.Column("price_usd_cents", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.execute(
        f"UPDATE products SET price_usd_cents = ROUND(price_kop * 100 / {BACKFILL_RATE_KOP})"
    )
    # Товар с нулевой ценой ломает расчёт наценки процентом, а такие строки
    # могли получиться из копеечных цен. Ставим минимальную цену в один цент.
    op.execute("UPDATE products SET price_usd_cents = 1 WHERE price_usd_cents <= 0")
    # Умолчание было нужно только чтобы добавить NOT NULL-колонку к существующим
    # строкам. Оставлять его нельзя: `INSERT` без цены молча создал бы
    # бесплатный товар.
    op.alter_column(
        "products",
        "price_usd_cents",
        existing_type=sa.BigInteger(),
        existing_nullable=False,
        server_default=None,
    )
    op.drop_column("products", "price_kop")
    op.drop_column("products", "delivery_type")

    # --- заказ: токен, реквизиты, снимки цены -------------------------------
    op.add_column("orders", sa.Column("token", sa.String(length=16), nullable=True))
    op.add_column("orders", sa.Column("account_login", sa.String(length=255), nullable=True))
    op.add_column("orders", sa.Column("account_password", sa.String(length=255), nullable=True))
    op.add_column("orders", sa.Column("credentials_at", sa.DateTime(), nullable=True))
    op.add_column("orders", sa.Column("delivered_by", sa.BigInteger(), nullable=True))
    op.add_column(
        "orders",
        sa.Column("price_usd_cents", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "orders", sa.Column("rate_kop", sa.BigInteger(), nullable=False, server_default="0")
    )
    op.add_column(
        "orders", sa.Column("markup_pct", sa.Integer(), nullable=False, server_default="0")
    )
    op.create_unique_constraint("uq_orders_token", "orders", ["token"])

    # `awaiting_credentials` — двадцать символов, в String(16) он не помещается
    # и обрезался бы молча. Расширяем колонку ДО переименования статусов.
    op.alter_column(
        "orders",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=32),
        existing_nullable=False,
        existing_server_default="new",
    )
    op.execute("UPDATE orders SET status = 'awaiting_credentials' WHERE status = 'awaiting'")

    # Заказы, уже оплаченные на момент наката, ждут реквизитов — но токена у них
    # нет, а покупатель без токена не может назвать свой заказ. Выдаём каждому.
    connection = op.get_bind()
    pending = connection.execute(
        sa.text(
            "SELECT id FROM orders "
            "WHERE token IS NULL AND status IN ('paid', 'awaiting_credentials', 'in_work')"
        )
    ).fetchall()
    used: set[str] = set()
    for (order_id,) in pending:
        token = _token()
        while token in used:
            token = _token()
        used.add(token)
        connection.execute(
            sa.text("UPDATE orders SET token = :token WHERE id = :id"),
            {"token": token, "id": int(order_id)},
        )

    op.drop_column("orders", "delivery_type")

    # --- склад --------------------------------------------------------------
    # Порядок обязателен: order_items ссылается на stock_items внешним ключом,
    # stock_items — на stock_batches. Удаление в обратном порядке не пройдёт.
    op.drop_table("order_items")
    op.drop_table("stock_items")
    op.drop_table("stock_batches")

    # --- роли администраторов ----------------------------------------------
    op.drop_column("admins", "role")


def downgrade() -> None:
    op.add_column(
        "admins",
        sa.Column("role", sa.String(length=16), nullable=False, server_default="admin"),
    )

    op.create_table(
        "stock_batches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("admin_id", sa.BigInteger(), nullable=True),
        sa.Column("items_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_stock_batches_product_id", "stock_batches", ["product_id"])

    op.create_table(
        "stock_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("file_id", sa.String(length=255), nullable=True),
        sa.Column("file_kind", sa.String(length=16), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="available", nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("reserved_until", sa.DateTime(), nullable=True),
        sa.Column("sold_at", sa.DateTime(), nullable=True),
        sa.Column("defect_reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["stock_batches.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_stock_items_product_id_status", "stock_items", ["product_id", "status"]
    )
    op.create_index(
        "ix_stock_items_status_reserved_until", "stock_items", ["status", "reserved_until"]
    )
    op.create_index("ix_stock_items_order_id", "stock_items", ["order_id"])

    op.create_table(
        "order_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("stock_item_id", sa.Integer(), nullable=False),
        sa.Column("replaced_by_item_id", sa.Integer(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["stock_item_id"], ["stock_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "order_id", "stock_item_id", name="uq_order_items_order_id_stock_item_id"
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"])

    op.add_column(
        "orders",
        sa.Column("delivery_type", sa.String(length=16), server_default="text", nullable=False),
    )
    op.execute("UPDATE orders SET status = 'awaiting' WHERE status = 'awaiting_credentials'")
    # `in_work` в старой схеме соответствия не имеет: ближайшее по смыслу —
    # «оплачен, ждёт администратора».
    op.execute("UPDATE orders SET status = 'awaiting' WHERE status = 'in_work'")
    op.alter_column(
        "orders",
        "status",
        existing_type=sa.String(length=32),
        type_=sa.String(length=16),
        existing_nullable=False,
        existing_server_default="new",
    )
    op.drop_constraint("uq_orders_token", "orders", type_="unique")
    op.drop_column("orders", "markup_pct")
    op.drop_column("orders", "rate_kop")
    op.drop_column("orders", "price_usd_cents")
    op.drop_column("orders", "delivered_by")
    op.drop_column("orders", "credentials_at")
    op.drop_column("orders", "account_password")
    op.drop_column("orders", "account_login")
    op.drop_column("orders", "token")

    op.add_column(
        "products",
        sa.Column("delivery_type", sa.String(length=16), server_default="text", nullable=False),
    )
    op.add_column(
        "products", sa.Column("price_kop", sa.BigInteger(), nullable=False, server_default="0")
    )
    op.execute(
        f"UPDATE products SET price_kop = ROUND(price_usd_cents * {BACKFILL_RATE_KOP} / 100)"
    )
    op.drop_column("products", "price_usd_cents")

    op.drop_index("ix_exchange_rates_code_fetched_at", table_name="exchange_rates")
    op.drop_table("exchange_rates")
