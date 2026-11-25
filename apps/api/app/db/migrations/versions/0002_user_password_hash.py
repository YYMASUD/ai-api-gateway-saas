"""Add users.password_hash for local email/password authentication.

Revision ID: 0002_user_password_hash
Revises: 0001_initial
Create Date: 2026-11-26

Phase 2 stores an Argon2id hash (never the plaintext password) for accounts
that register with email/password. The column is nullable: OAuth-only or
pre-existing accounts have no local password until a later reset flow.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_user_password_hash"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "password_hash")
