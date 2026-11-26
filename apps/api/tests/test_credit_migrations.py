"""Offline Alembic smoke tests for Phase 3 credit reservations."""

from __future__ import annotations

import re
from pathlib import Path

from alembic import command
from alembic.config import Config

API_ROOT = Path(__file__).resolve().parents[1]


def _config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "app" / "db" / "migrations"))
    config.set_main_option("prepend_sys_path", str(API_ROOT))
    config.set_main_option("sqlalchemy.url", "postgresql://user:placeholder@localhost:5432/gateway")
    return config


def _offline_upgrade(capsys) -> str:
    command.upgrade(_config(), "head", sql=True)
    return capsys.readouterr().out


def _offline_phase3_downgrade(capsys) -> str:
    # Only render 0003 -> 0002; offline mode never connects to PostgreSQL.
    command.downgrade(_config(), "head:0002_user_password_hash", sql=True)
    return capsys.readouterr().out


def _reservation_block(sql: str) -> str:
    start = sql.index("CREATE TABLE credit_reservations")
    end = sql.index(";", start) + 1
    return sql[start:end]


def test_phase3_upgrade_creates_reservations_table(capsys) -> None:
    sql = _offline_upgrade(capsys)
    block = _reservation_block(sql)
    assert block.startswith("CREATE TABLE credit_reservations")
    for column in (
        "id",
        "user_id",
        "api_key_id",
        "request_id",
        "status",
        "amount_microcredits",
        "cost_microcredits",
        "expires_at",
        "settled_at",
        "released_at",
        "created_at",
        "updated_at",
    ):
        assert re.search(rf"\b{re.escape(column)}\b", block), column


def test_phase3_reservation_column_types_and_defaults(capsys) -> None:
    block = _reservation_block(_offline_upgrade(capsys))
    assert re.search(r"\bid UUID", block)
    assert re.search(r"\brequest_id VARCHAR\(128\) NOT NULL", block)
    assert re.search(r"\bstatus VARCHAR\(16\) DEFAULT '?open'? NOT NULL", block)
    assert re.search(r"\bamount_microcredits BIGINT NOT NULL", block)
    assert re.search(r"\bcost_microcredits BIGINT DEFAULT '?0'? NOT NULL", block)
    assert re.search(r"\bexpires_at TIMESTAMP WITH TIME ZONE NOT NULL", block)
    assert re.search(r"\bsettled_at TIMESTAMP WITH TIME ZONE\b", block)
    assert re.search(r"\breleased_at TIMESTAMP WITH TIME ZONE\b", block)
    assert re.search(r"\bcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now\(\) NOT NULL", block)


def test_phase3_reservation_constraints(capsys) -> None:
    block = _reservation_block(_offline_upgrade(capsys))
    assert "uq_credit_reservations_request_id" in block
    assert "UNIQUE (request_id)" in block
    assert "status_valid" in block
    assert "status IN ('open', 'settled', 'released')" in block
    assert "amount_positive" in block
    assert "amount_microcredits > 0" in block
    assert "cost_valid" in block
    assert "cost_microcredits >= 0 AND cost_microcredits <= amount_microcredits" in block
    assert "PRIMARY KEY (id)" in block
    assert "gen_random_uuid()" in block


def test_phase3_reservation_foreign_keys(capsys) -> None:
    block = _reservation_block(_offline_upgrade(capsys))
    assert re.search(
        r"FOREIGN KEY\(user_id\).*?REFERENCES users \(id\).*?ON DELETE RESTRICT",
        block,
        re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"FOREIGN KEY\(api_key_id\).*?REFERENCES api_keys \(id\).*?ON DELETE SET NULL",
        block,
        re.IGNORECASE | re.DOTALL,
    )


def test_phase3_creates_indexes(capsys) -> None:
    sql = _offline_upgrade(capsys)
    assert re.search(
        r"CREATE INDEX ix_credit_reservations_status_expires_at ON credit_reservations",
        sql,
    )
    assert re.search(
        r"CREATE INDEX ix_credit_reservations_user_id_created_at ON credit_reservations",
        sql,
    )


def test_phase3_adds_user_microcredit_cap(capsys) -> None:
    sql = _offline_upgrade(capsys)
    assert re.search(
        r"ALTER TABLE users ADD.*spend_cap_monthly_microcredits\s+BIGINT",
        sql,
        re.IGNORECASE,
    )


def test_phase3_upgrade_drops_nothing(capsys) -> None:
    sql = _offline_upgrade(capsys)
    assert "DROP TABLE" not in sql
    assert "DROP COLUMN" not in sql


def test_phase3_downgrade_drops_only_new_objects(capsys) -> None:
    sql = _offline_phase3_downgrade(capsys)
    assert "DROP INDEX ix_credit_reservations_status_expires_at" in sql
    assert "DROP INDEX ix_credit_reservations_user_id_created_at" in sql
    assert "DROP TABLE credit_reservations" in sql
    assert "ALTER TABLE users DROP COLUMN spend_cap_monthly_microcredits" in sql
    # Existing schema must be untouched by the Phase 3 downgrade.
    assert "DROP TABLE users" not in sql
    assert "DROP TABLE api_keys" not in sql
    assert "DROP TABLE credit_ledger" not in sql
    assert "DROP TABLE credit_balances" not in sql
    assert "DROP COLUMN spend_cap_monthly_cents" not in sql
    assert "DROP COLUMN password_hash" not in sql
