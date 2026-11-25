"""Offline Alembic smoke tests: no database connection is made."""

from __future__ import annotations

import re
from pathlib import Path

from alembic import command
from alembic.config import Config

from tests.test_models import REQUIRED_FIELDS

API_ROOT = Path(__file__).resolve().parents[1]


def _config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "app" / "db" / "migrations"))
    config.set_main_option("prepend_sys_path", str(API_ROOT))
    # Offline mode never connects; this only selects the PostgreSQL dialect.
    config.set_main_option("sqlalchemy.url", "postgresql://user:placeholder@localhost:5432/gateway")
    return config


def _offline_sql(capsys) -> str:
    command.upgrade(_config(), "head", sql=True)
    return capsys.readouterr().out


def test_initial_migration_renders_all_tables_offline(capsys) -> None:
    sql = _offline_sql(capsys)
    for table in (
        "users",
        "api_keys",
        "credit_ledger",
        "credit_balances",
        "usage_events",
        "payments",
        "webhook_events",
        "audit_logs",
        "model_pricing",
    ):
        assert f"CREATE TABLE {table} " in sql
    assert "CREATE EXTENSION IF NOT EXISTS pgcrypto" in sql
    assert "BIGINT" in sql
    assert "forbid_credit_ledger_mutation" in sql


def test_initial_migration_uses_spec_field_names(capsys) -> None:
    sql = _offline_sql(capsys)
    for table_name, required in REQUIRED_FIELDS.items():
        for field in required:
            # id is rendered by a helper; only check explicit columns here.
            if field == "id":
                continue
            assert re.search(rf"\b{re.escape(field)}\b", sql), (table_name, field)


def test_initial_migration_has_no_legacy_field_names(capsys) -> None:
    sql = _offline_sql(capsys)
    for legacy in (
        "key_prefix",
        "entry_type",
        "delta_microcredits",
        "balance_after_microcredits",
        "balance_microcredits",
        "reserved_microcredits",
        "stripe_checkout_session_id",
        "event_id",
        "event_type",
        "ip_address",
        "resource_type",
        "resource_id",
        "model_name",
        "is_active",
        "metadata_json",
        "payload_json",
    ):
        assert not re.search(rf"\b{re.escape(legacy)}\b", sql), legacy


def test_initial_migration_money_columns_are_integer(capsys) -> None:
    sql = _offline_sql(capsys)
    for column in (
        "spend_cap_monthly_cents",
        "amount_microcredits",
        "balance",
        "reserved",
        "cost_microcredits",
        "amount_cents",
        "credits_granted",
        "input_price_per_1k",
        "output_price_per_1k",
    ):
        assert re.search(rf"\b{re.escape(column)}\s+BIGINT\b", sql), column
    assert " FLOAT" not in sql
    assert " NUMERIC" not in sql


def test_initial_migration_has_required_uniques(capsys) -> None:
    sql = _offline_sql(capsys)
    for constraint in (
        "uq_users_email",
        "uq_api_keys_key_hash",
        "uq_credit_ledger_idempotency_key",
        "uq_credit_balances_user_id",
        "uq_usage_events_request_id",
        "uq_payments_stripe_session_id",
        "uq_webhook_events_stripe_event_id",
        "uq_model_pricing_model_id",
    ):
        assert constraint in sql
