"""Scope reservation request ids to their owning user.

Revision ID: 0004_request_id_per_user
Revises: 0003_credit_reservations
Create Date: 2026-11-26

``credit_reservations.request_id`` was unique across ALL users, so two users
choosing the same id collided (an unhandled IntegrityError or a cross-user
409). Replace the global unique constraint with a composite
``(user_id, request_id)`` unique constraint so every user gets an independent
request-id namespace.

The downgrade restores the global constraint and will fail if rows written
under the composite constraint share a ``request_id`` across users; resolve
such duplicates before downgrading.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_request_id_per_user"
down_revision: str | None = "0003_credit_reservations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_credit_reservations_request_id", "credit_reservations", type_="unique")
    op.create_unique_constraint(
        "uq_credit_reservations_user_id_request_id",
        "credit_reservations",
        ["user_id", "request_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_credit_reservations_user_id_request_id", "credit_reservations", type_="unique"
    )
    op.create_unique_constraint(
        "uq_credit_reservations_request_id", "credit_reservations", ["request_id"]
    )
