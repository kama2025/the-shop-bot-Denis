"""убрать внутренний баланс, добавить цвет категории

Revision ID: d3f81a2b64c7
Revises: b7e4c9a10f32
Create Date: 2026-08-15
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3f81a2b64c7"
down_revision: str | None = "b7e4c9a10f32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_ACCENT = "success"


def upgrade() -> None:
    # --- цвет категории -----------------------------------------------------
    op.add_column(
        "categories",
        sa.Column("accent", sa.String(length=16), nullable=False, server_default=DEFAULT_ACCENT),
    )

    # --- внутренний баланс уходит -------------------------------------------
    # Заказы на пополнение теряют смысл вместе с балансом: товара у них нет,
    # а деньги зачислялись на счёт, которого больше не будет. Помечаем их
    # возвращёнными — это единственное состояние, которое честно говорит
    # «деньги приняты, обязательство осталось за магазином».
    op.execute("UPDATE orders SET status = 'refunded' WHERE kind = 'topup'")
    op.drop_column("orders", "kind")

    op.drop_table("balance_txns")
    op.drop_column("users", "balance_kop")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column("balance_kop", sa.BigInteger(), nullable=False, server_default="0"),
    )

    op.create_table(
        "balance_txns",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("amount_kop", sa.BigInteger(), nullable=False),
        sa.Column("balance_after_kop", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("admin_id", sa.BigInteger(), nullable=True),
        sa.Column("comment", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_balance_txns_user_id_created_at", "balance_txns", ["user_id", "created_at"]
    )

    op.add_column(
        "orders",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="purchase"),
    )
    # Обратно пополнения не восстанавливаются: какие из возвращённых заказов
    # были пополнениями, знать уже неоткуда. Откат возвращает схему, а не
    # утраченный смысл строк — и это честнее, чем угадывать.

    op.drop_column("categories", "accent")
