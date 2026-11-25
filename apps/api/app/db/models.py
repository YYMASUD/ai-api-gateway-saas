"""SQLAlchemy 2 async ORM models for Phase 1 data tables.

Money is always represented as signed ``BIGINT`` integers; floating point
types are intentionally absent. Two integer units are used and must not be
mixed:

* **Microcredits** — 1,000,000 microcredits = 1 credit. Used by
  ``credit_ledger.amount_microcredits``, ``credit_balances.balance`` /
  ``reserved``, ``usage_events.cost_microcredits``,
  ``payments.credits_granted`` and ``model_pricing.*_price_per_1k`` (the latter
  is microcredits per 1,000 tokens).
* **Fiat cents** — used only where an amount is a real-money charge:
  ``users.spend_cap_monthly_cents`` and ``payments.amount_cents``.

Where a spec field is named ``metadata`` the Python attribute is
``metadata_json`` because ``metadata`` is reserved by SQLAlchemy's declarative
API; the database column is still exactly ``metadata``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        UniqueConstraint("stripe_customer_id", name="uq_users_stripe_customer_id"),
        CheckConstraint("role IN ('user', 'admin')", name="role_valid"),
        CheckConstraint("status IN ('active', 'suspended')", name="status_valid"),
        Index("ix_users_created_at", "created_at"),
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    # Additive: dashboard display label; not part of the required Phase 1 set.
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    auth_provider_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(
        String(32), nullable=False, default="user", server_default="user"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active"
    )
    # Fiat cents (BIGINT). 0 disables the monthly cap.
    spend_cap_monthly_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    # Additive: needed to reconcile Stripe customers with local users.
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Additive: dashboard session telemetry.
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class APIKey(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
        Index("ix_api_keys_user_id", "user_id"),
        Index("ix_api_keys_prefix", "prefix"),
        Index("ix_api_keys_created_at", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # First 8 characters of the plaintext key, shown in the UI for identification.
    prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    # SHA-256(plaintext_key + API_KEY_PEPPER), hex encoded.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CreditLedger(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Append-only credit movements; never UPDATE or DELETE rows.

    The database enforces append-only behaviour with a trigger created in the
    initial migration.
    """

    __tablename__ = "credit_ledger"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_credit_ledger_idempotency_key"),
        CheckConstraint(
            "type IN ('purchase', 'reservation', 'settlement', 'refund', 'adjustment')",
            name="type_valid",
        ),
        Index("ix_credit_ledger_user_id_created_at", "user_id", "created_at"),
        Index("ix_credit_ledger_reference_id", "reference_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    # Signed microcredits (BIGINT): positive credits the account, negative debits it.
    amount_microcredits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reference_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class CreditBalance(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Materialised balances; fully rebuildable by replaying ``credit_ledger``."""

    __tablename__ = "credit_balances"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_credit_balances_user_id"),
        CheckConstraint("balance >= 0", name="nonnegative"),
        CheckConstraint("reserved >= 0", name="reserved_nonnegative"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Microcredits (BIGINT).
    balance: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    reserved: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    # Additive: lifetime counters are handy for dashboards without an extra
    # aggregation query and are still derived from the append-only ledger.
    lifetime_purchased_microcredits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    lifetime_spent_microcredits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )


class UsageEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_usage_events_request_id"),
        Index("ix_usage_events_user_id_created_at", "user_id", "created_at"),
        Index("ix_usage_events_model", "model"),
        Index("ix_usage_events_api_key_id", "api_key_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # Microcredits (BIGINT).
    cost_microcredits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="success", server_default="success"
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upstream_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Additive: convenience total and failure diagnostics.
    total_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class Payment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("stripe_session_id", name="uq_payments_stripe_session_id"),
        UniqueConstraint("stripe_payment_intent", name="uq_payments_stripe_payment_intent"),
        Index("ix_payments_user_id_created_at", "user_id", "created_at"),
        Index("ix_payments_status", "status"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    stripe_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stripe_payment_intent: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Fiat cents (BIGINT) actually charged by Stripe.
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Microcredits (BIGINT) credited to the user's ledger for this payment.
    credits_granted: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    # Additive: currency code for amount_cents and ledger metadata.
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="usd", server_default="usd"
    )
    metadata_json: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class WebhookEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("stripe_event_id", name="uq_webhook_events_stripe_event_id"),
        Index("ix_webhook_events_processed_at", "processed_at"),
        Index("ix_webhook_events_created_at", "created_at"),
    )

    stripe_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Additive: retry counter for the webhook handler; processed_at is the
    # terminal success marker.
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class AuditLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Security audit trail. Never store prompts, API keys, or secrets here."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_actor_id_created_at", "actor_id", "created_at"),
        Index("ix_audit_logs_action", "action"),
    )

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Additive: distinguishes user vs. system actors in the audit trail.
    actor_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="user", server_default="user"
    )
    # Additive: request diagnostics, never credentials.
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class ModelPricing(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "model_pricing"
    __table_args__ = (
        UniqueConstraint("model_id", name="uq_model_pricing_model_id"),
        CheckConstraint("input_price_per_1k >= 0", name="input_nonnegative"),
        CheckConstraint("output_price_per_1k >= 0", name="output_nonnegative"),
        Index("ix_model_pricing_provider", "provider"),
    )

    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # Microcredits per 1,000 input tokens (BIGINT).
    input_price_per_1k: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    # Microcredits per 1,000 output tokens (BIGINT).
    output_price_per_1k: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    # Additive: which upstream serves the model; useful for routing.
    provider: Mapped[str] = mapped_column(
        String(64), nullable=False, default="litellm", server_default="litellm"
    )
    metadata_json: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
