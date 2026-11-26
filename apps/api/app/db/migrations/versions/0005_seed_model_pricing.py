"""Seed a single local-development model_pricing placeholder row.

Revision ID: 0005_seed_model_pricing
Revises: 0004_request_id_per_user
Create Date: 2026-11-26

Phase 4 exposes an OpenAI-compatible ``/v1`` surface that must resolve the
requested ``model`` against an ENABLED ``model_pricing`` row. Real model
aliases and per-token prices are commercial decisions that require explicit
product sign-off, so this migration seeds exactly one clearly-labelled
development placeholder:

* ``model_id = 'gateway-default'``
* ``input_price_per_1k = 0`` and ``output_price_per_1k = 0``
* ``provider = 'litellm'``
* metadata marks it as a local-dev placeholder pending product approval

Nothing here selects a real upstream provider, invents provider credentials, or
hard-codes a real model name. Replace/disable this row (and add approved
aliases/prices) before any production use. The insert is idempotent
(``ON CONFLICT DO NOTHING`` on the ``model_id`` unique constraint) so it is
safe to re-run, and the downgrade removes only this one placeholder row.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_seed_model_pricing"
down_revision: str | None = "0004_request_id_per_user"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PLACEHOLDER_MODEL_ID = "gateway-default"
_PLACEHOLDER_METADATA = (
    '{"local_dev_placeholder": true, '
    '"note": "local-development placeholder pending product sign-off; '
    'replace with approved model aliases and prices"}'
)


def upgrade() -> None:
    # Parameter-free literal insert rendered identically online and offline.
    op.execute(
        sa.text(
            """
            INSERT INTO model_pricing
                (id, model_id, input_price_per_1k, output_price_per_1k,
                 enabled, provider, metadata, created_at, updated_at)
            VALUES
                (gen_random_uuid(), :model_id, 0, 0, true, 'litellm',
                 CAST(:metadata AS jsonb), now(), now())
            ON CONFLICT (model_id) DO NOTHING
            """
        ).bindparams(
            sa.bindparam("model_id", value=_PLACEHOLDER_MODEL_ID),
            sa.bindparam("metadata", value=_PLACEHOLDER_METADATA),
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM model_pricing WHERE model_id = :model_id").bindparams(
            sa.bindparam("model_id", value=_PLACEHOLDER_MODEL_ID)
        )
    )
