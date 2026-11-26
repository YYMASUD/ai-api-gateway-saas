"""Offline Alembic smoke tests for the 0005 model-pricing placeholder seed."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

API_ROOT = Path(__file__).resolve().parents[1]


def _config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "app" / "db" / "migrations"))
    config.set_main_option("prepend_sys_path", str(API_ROOT))
    # Offline mode never connects; this only selects the PostgreSQL dialect.
    config.set_main_option("sqlalchemy.url", "postgresql://user:placeholder@localhost:5432/gateway")
    return config


def _offline_upgrade(capsys: pytest.CaptureFixture[str]) -> str:
    command.upgrade(_config(), "0004_request_id_per_user:head", sql=True)
    return capsys.readouterr().out


def _offline_downgrade(capsys: pytest.CaptureFixture[str]) -> str:
    command.downgrade(_config(), "head:0004_request_id_per_user", sql=True)
    return capsys.readouterr().out


def test_0005_revision_metadata() -> None:
    scripts = ScriptDirectory.from_config(_config())
    head = scripts.get_revision("head")
    assert head is not None
    assert head.revision == "0005_seed_model_pricing"
    assert len(head.revision) <= 32
    assert head.down_revision == "0004_request_id_per_user"


def test_0005_seeds_idempotent_local_dev_placeholder(capsys: pytest.CaptureFixture[str]) -> None:
    sql = _offline_upgrade(capsys)
    assert "-- Running upgrade 0004_request_id_per_user -> 0005_seed_model_pricing" in sql
    assert "INSERT INTO model_pricing" in sql
    assert "'gateway-default'" in sql
    assert "ON CONFLICT (model_id) DO NOTHING" in sql
    assert "'litellm'" in sql
    # Zero prices and an explicit local-dev marker pending product sign-off.
    assert "local_dev_placeholder" in sql
    assert "product sign-off" in sql
    # The seed must not create or drop schema objects.
    assert "CREATE TABLE" not in sql
    assert "DROP TABLE" not in sql


def test_0005_downgrade_deletes_only_the_placeholder(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sql = _offline_downgrade(capsys)
    assert "-- Running downgrade 0005_seed_model_pricing -> 0004_request_id_per_user" in sql
    assert "DELETE FROM model_pricing WHERE model_id = 'gateway-default'" in sql
    assert "DROP TABLE" not in sql
    assert "DROP COLUMN" not in sql
    assert "DELETE FROM model_pricing WHERE model_id != " not in sql
