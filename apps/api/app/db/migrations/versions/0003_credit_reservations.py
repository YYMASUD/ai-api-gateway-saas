"""Add credit_reservations table and users.spend_cap_monthly_microcredits.

Revision ID: 0003_credit_reservations
Revises: 0002_user_password_hash
Create Date: 2026-11-26

Phase 3 introduces a separate reservations table to track outstanding credit
holds. ``credit_balances.balance`` remains the total unspent wallet (equal to
the ledger sum); ``reserved`` tracks these holds and available = balance -
reserved. Reservations are released atomically when settled or refunded.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_credit_reservations"
down_revision: str | None = "0002_user_password_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
ZERO = sa.text("0")


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        server_default=sa.text("gen_random_uuid()"),
    )


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    ]


def upgrade() -> None:
    op.create_table(
        "credit_reservations",
        _uuid_pk(),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "api_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_keys.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("amount_microcredits", sa.BigInteger(), nullable=False),
        sa.Column("cost_microcredits", sa.BigInteger(), nullable=False, server_default=ZERO),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("request_id", name="uq_credit_reservations_request_id"),
        sa.CheckConstraint("status IN ('open', 'settled', 'released')", name="status_valid"),
        sa.CheckConstraint("amount_microcredits > 0", name="amount_positive"),
        sa.CheckConstraint(
            "cost_microcredits >= 0 AND cost_microcredits <= amount_microcredits",
            name="cost_valid",
        ),
    )
    op.create_index(
        "ix_credit_reservations_status_expires_at",
        "credit_reservations",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_credit_reservations_user_id_created_at",
        "credit_reservations",
        ["user_id", "created_at"],
    )

    op.add_column(
        "users",
        sa.Column(
            "spend_cap_monthly_microcredits",
            sa.BigInteger(),
            nullable=False,
            server_default=ZERO,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "spend_cap_monthly_microcredits")
    op.drop_index("ix_credit_reservations_user_id_created_at", table_name="credit_reservations")
    op.drop_index("ix_credit_reservations_status_expires_at", table_name="credit_reservations")
    op.drop_table("credit_reservations")
