"""Initial schema for the AI API Gateway.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-25

Field names mirror the Phase 1 logical data model. All money columns are
BIGINT integers: microcredits (1,000,000 = 1 credit) for credit/usage values
and fiat cents for ``users.spend_cap_monthly_cents`` and
``payments.amount_cents``. ``model_pricing.*_price_per_1k`` are microcredits
per 1,000 tokens.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
EMPTY_JSONB = sa.text("'{}'::jsonb")
EMPTY_JSONB_ARRAY = sa.text("'[]'::jsonb")
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
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "users",
        _uuid_pk(),
        sa.Column("email", sa.String(length=320), nullable=False),
        # Additive: dashboard display label.
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("auth_provider_id", sa.String(length=255), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="user"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("spend_cap_monthly_cents", sa.BigInteger(), nullable=False, server_default=ZERO),
        # Additive: Stripe customer reconciliation and dashboard telemetry.
        sa.Column("stripe_customer_id", sa.String(length=255), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("stripe_customer_id", name="uq_users_stripe_customer_id"),
        sa.CheckConstraint("role IN ('user', 'admin')", name="role_valid"),
        sa.CheckConstraint("status IN ('active', 'suspended')", name="status_valid"),
    )
    op.create_index("ix_users_created_at", "users", ["created_at"])

    op.create_table(
        "api_keys",
        _uuid_pk(),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("prefix", sa.String(length=8), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", postgresql.JSONB(), nullable=False, server_default=EMPTY_JSONB_ARRAY),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    )
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    op.create_index("ix_api_keys_prefix", "api_keys", ["prefix"])
    op.create_index("ix_api_keys_created_at", "api_keys", ["created_at"])

    op.create_table(
        "credit_ledger",
        _uuid_pk(),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=48), nullable=False),
        sa.Column("amount_microcredits", sa.BigInteger(), nullable=False),
        sa.Column("reference_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=EMPTY_JSONB),
        *_timestamps(),
        sa.UniqueConstraint("idempotency_key", name="uq_credit_ledger_idempotency_key"),
        sa.CheckConstraint(
            "type IN ('purchase', 'reservation', 'settlement', 'refund', 'adjustment')",
            name="type_valid",
        ),
    )
    op.create_index(
        "ix_credit_ledger_user_id_created_at", "credit_ledger", ["user_id", "created_at"]
    )
    op.create_index("ix_credit_ledger_reference_id", "credit_ledger", ["reference_id"])

    # Enforce append-only semantics in the database, not just in application code.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION forbid_credit_ledger_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'credit_ledger is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_credit_ledger_append_only
        BEFORE UPDATE OR DELETE ON credit_ledger
        FOR EACH ROW EXECUTE FUNCTION forbid_credit_ledger_mutation()
        """
    )

    op.create_table(
        "credit_balances",
        _uuid_pk(),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("balance", sa.BigInteger(), nullable=False, server_default=ZERO),
        sa.Column("reserved", sa.BigInteger(), nullable=False, server_default=ZERO),
        # Additive: lifetime counters derived from the append-only ledger.
        sa.Column(
            "lifetime_purchased_microcredits", sa.BigInteger(), nullable=False, server_default=ZERO
        ),
        sa.Column(
            "lifetime_spent_microcredits", sa.BigInteger(), nullable=False, server_default=ZERO
        ),
        *_timestamps(),
        sa.UniqueConstraint("user_id", name="uq_credit_balances_user_id"),
        sa.CheckConstraint("balance >= 0", name="nonnegative"),
        sa.CheckConstraint("reserved >= 0", name="reserved_nonnegative"),
    )

    op.create_table(
        "usage_events",
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
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default=ZERO),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default=ZERO),
        sa.Column("cost_microcredits", sa.BigInteger(), nullable=False, server_default=ZERO),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="success"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("upstream_provider", sa.String(length=64), nullable=True),
        # Additive: token total and failure diagnostics.
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default=ZERO),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=EMPTY_JSONB),
        *_timestamps(),
        sa.UniqueConstraint("request_id", name="uq_usage_events_request_id"),
    )
    op.create_index("ix_usage_events_user_id_created_at", "usage_events", ["user_id", "created_at"])
    op.create_index("ix_usage_events_model", "usage_events", ["model"])
    op.create_index("ix_usage_events_api_key_id", "usage_events", ["api_key_id"])

    op.create_table(
        "payments",
        _uuid_pk(),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("stripe_session_id", sa.String(length=128), nullable=True),
        sa.Column("stripe_payment_intent", sa.String(length=128), nullable=True),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("credits_granted", sa.BigInteger(), nullable=False, server_default=ZERO),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        # Additive: fiat currency for amount_cents.
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="usd"),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=EMPTY_JSONB),
        *_timestamps(),
        sa.UniqueConstraint("stripe_session_id", name="uq_payments_stripe_session_id"),
        sa.UniqueConstraint("stripe_payment_intent", name="uq_payments_stripe_payment_intent"),
    )
    op.create_index("ix_payments_user_id_created_at", "payments", ["user_id", "created_at"])
    op.create_index("ix_payments_status", "payments", ["status"])

    op.create_table(
        "webhook_events",
        _uuid_pk(),
        sa.Column("stripe_event_id", sa.String(length=128), nullable=False),
        sa.Column("type", sa.String(length=128), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=EMPTY_JSONB),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        # Additive: retry counter; processed_at is the terminal success marker.
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=ZERO),
        *_timestamps(),
        sa.UniqueConstraint("stripe_event_id", name="uq_webhook_events_stripe_event_id"),
    )
    op.create_index("ix_webhook_events_processed_at", "webhook_events", ["processed_at"])
    op.create_index("ix_webhook_events_created_at", "webhook_events", ["created_at"])

    op.create_table(
        "audit_logs",
        _uuid_pk(),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=True),
        sa.Column("target_id", sa.String(length=128), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        # Additive: actor classification and request diagnostics, no secrets.
        sa.Column("actor_type", sa.String(length=32), nullable=False, server_default="user"),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=EMPTY_JSONB),
        *_timestamps(),
    )
    op.create_index("ix_audit_logs_actor_id_created_at", "audit_logs", ["actor_id", "created_at"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])

    op.create_table(
        "model_pricing",
        _uuid_pk(),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("input_price_per_1k", sa.BigInteger(), nullable=False, server_default=ZERO),
        sa.Column("output_price_per_1k", sa.BigInteger(), nullable=False, server_default=ZERO),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        # Additive: upstream routing label.
        sa.Column("provider", sa.String(length=64), nullable=False, server_default="litellm"),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=EMPTY_JSONB),
        *_timestamps(),
        sa.UniqueConstraint("model_id", name="uq_model_pricing_model_id"),
        sa.CheckConstraint("input_price_per_1k >= 0", name="input_nonnegative"),
        sa.CheckConstraint("output_price_per_1k >= 0", name="output_nonnegative"),
    )
    op.create_index("ix_model_pricing_provider", "model_pricing", ["provider"])


def downgrade() -> None:
    op.drop_index("ix_model_pricing_provider", table_name="model_pricing")
    op.drop_table("model_pricing")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_id_created_at", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_webhook_events_created_at", table_name="webhook_events")
    op.drop_index("ix_webhook_events_processed_at", table_name="webhook_events")
    op.drop_table("webhook_events")
    op.drop_index("ix_payments_status", table_name="payments")
    op.drop_index("ix_payments_user_id_created_at", table_name="payments")
    op.drop_table("payments")
    op.drop_index("ix_usage_events_api_key_id", table_name="usage_events")
    op.drop_index("ix_usage_events_model", table_name="usage_events")
    op.drop_index("ix_usage_events_user_id_created_at", table_name="usage_events")
    op.drop_table("usage_events")
    op.drop_table("credit_balances")
    op.execute("DROP TRIGGER IF EXISTS trg_credit_ledger_append_only ON credit_ledger")
    op.execute("DROP FUNCTION IF EXISTS forbid_credit_ledger_mutation()")
    op.drop_index("ix_credit_ledger_reference_id", table_name="credit_ledger")
    op.drop_index("ix_credit_ledger_user_id_created_at", table_name="credit_ledger")
    op.drop_table("credit_ledger")
    op.drop_index("ix_api_keys_created_at", table_name="api_keys")
    op.drop_index("ix_api_keys_prefix", table_name="api_keys")
    op.drop_index("ix_api_keys_user_id", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("ix_users_created_at", table_name="users")
    op.drop_table("users")
