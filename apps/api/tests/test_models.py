"""Model metadata tests that do not require a database connection."""

from __future__ import annotations

from sqlalchemy import BigInteger, Float, Numeric, UniqueConstraint

from app.db import models

EXPECTED_TABLES = {
    "users",
    "api_keys",
    "credit_ledger",
    "credit_balances",
    "usage_events",
    "payments",
    "webhook_events",
    "audit_logs",
    "model_pricing",
}

# Logical Phase 1 fields that must exist under exactly these names.
REQUIRED_FIELDS: dict[str, set[str]] = {
    "users": {
        "id",
        "email",
        "auth_provider_id",
        "role",
        "status",
        "spend_cap_monthly_cents",
        "created_at",
        "updated_at",
    },
    "api_keys": {
        "id",
        "user_id",
        "name",
        "prefix",
        "key_hash",
        "last_used_at",
        "revoked_at",
        "scopes",
        "created_at",
        "updated_at",
    },
    "credit_ledger": {
        "id",
        "user_id",
        "type",
        "amount_microcredits",
        "reference_id",
        "idempotency_key",
        "metadata",
        "created_at",
        "updated_at",
    },
    "credit_balances": {"id", "user_id", "balance", "reserved", "created_at", "updated_at"},
    "usage_events": {
        "id",
        "user_id",
        "api_key_id",
        "request_id",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "cost_microcredits",
        "status",
        "latency_ms",
        "upstream_provider",
        "created_at",
        "updated_at",
    },
    "payments": {
        "id",
        "user_id",
        "stripe_session_id",
        "stripe_payment_intent",
        "amount_cents",
        "credits_granted",
        "status",
        "created_at",
        "updated_at",
    },
    "webhook_events": {
        "id",
        "stripe_event_id",
        "type",
        "payload",
        "processed_at",
        "error",
        "created_at",
        "updated_at",
    },
    "audit_logs": {
        "id",
        "actor_id",
        "action",
        "target_type",
        "target_id",
        "ip",
        "metadata",
        "created_at",
        "updated_at",
    },
    "model_pricing": {
        "id",
        "model_id",
        "input_price_per_1k",
        "output_price_per_1k",
        "enabled",
        "created_at",
        "updated_at",
    },
}

LEGACY_FIELDS = {
    "key_prefix",
    "entry_type",
    "delta_microcredits",
    "balance_after_microcredits",
    "balance_microcredits",
    "reserved_microcredits",
    "stripe_checkout_session_id",
    "stripe_payment_intent_id",
    "event_id",
    "event_type",
    "ip_address",
    "resource_type",
    "resource_id",
    "model_name",
    "is_active",
    "password_hash",
    "metadata_json",
    "payload_json",
}

MONEY_FIELDS = {
    ("users", "spend_cap_monthly_cents"),
    ("credit_ledger", "amount_microcredits"),
    ("credit_balances", "balance"),
    ("credit_balances", "reserved"),
    ("credit_balances", "lifetime_purchased_microcredits"),
    ("credit_balances", "lifetime_spent_microcredits"),
    ("usage_events", "cost_microcredits"),
    ("payments", "amount_cents"),
    ("payments", "credits_granted"),
    ("model_pricing", "input_price_per_1k"),
    ("model_pricing", "output_price_per_1k"),
}


def test_all_phase_one_tables_exist() -> None:
    assert EXPECTED_TABLES == set(models.Base.metadata.tables)


def test_all_phase_one_required_fields_exist() -> None:
    for table_name, required in REQUIRED_FIELDS.items():
        assert required <= set(models.Base.metadata.tables[table_name].c.keys()), table_name


def test_no_legacy_field_names_remain() -> None:
    for table in models.Base.metadata.tables.values():
        assert not LEGACY_FIELDS.intersection(table.c.keys()), table.name


def test_every_table_has_uuid_pk_and_timestamps() -> None:
    for table in models.Base.metadata.tables.values():
        assert table.c.id.primary_key, table.name
        assert table.c.created_at.type.timezone, table.name
        assert table.c.updated_at.type.timezone, table.name
        assert not table.c.created_at.nullable
        assert not table.c.updated_at.nullable


def test_money_columns_are_bigints_not_floats() -> None:
    for table_name, column_name in MONEY_FIELDS:
        column = models.Base.metadata.tables[table_name].c[column_name]
        assert isinstance(column.type, BigInteger), column
        assert not isinstance(column.type, Float | Numeric), column
    # Guard against accidental float/numeric money columns anywhere in the schema.
    for table in models.Base.metadata.tables.values():
        for column in table.columns:
            assert not isinstance(column.type, Float | Numeric), column


def test_ledger_is_append_only_by_construction() -> None:
    table = models.Base.metadata.tables["credit_ledger"]
    assert "deleted_at" not in table.c
    assert "type" in table.c
    assert "amount_microcredits" in table.c
    assert not table.c.idempotency_key.nullable
    assert "metadata" in table.c


def _unique_constraint_columns(table_name: str) -> set[frozenset[str]]:
    table = models.Base.metadata.tables[table_name]
    return {
        frozenset(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_key_uniques_and_indexes() -> None:
    assert frozenset({"key_hash"}) in _unique_constraint_columns("api_keys")
    assert frozenset({"email"}) in _unique_constraint_columns("users")
    assert frozenset({"request_id"}) in _unique_constraint_columns("usage_events")
    assert frozenset({"model_id"}) in _unique_constraint_columns("model_pricing")
    assert frozenset({"user_id"}) in _unique_constraint_columns("credit_balances")
    assert frozenset({"idempotency_key"}) in _unique_constraint_columns("credit_ledger")
    assert frozenset({"stripe_session_id"}) in _unique_constraint_columns("payments")
    assert frozenset({"stripe_event_id"}) in _unique_constraint_columns("webhook_events")


def test_api_key_prefix_is_eight_characters() -> None:
    prefix = models.Base.metadata.tables["api_keys"].c.prefix.type
    assert prefix.length == 8  # type: ignore[attr-defined]
