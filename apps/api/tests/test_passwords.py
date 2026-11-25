"""Argon2id password hashing, verification, and rehash detection tests."""

from __future__ import annotations

import pytest

from app.core import passwords
from app.core.passwords import (
    hash_password,
    hash_password_async,
    password_needs_rehash,
    verify_password,
    verify_password_async,
)

PASSWORD = "correct horse battery staple"


def test_hash_is_argon2id_and_never_contains_plaintext() -> None:
    digest = hash_password(PASSWORD)
    assert digest.startswith("$argon2id$")
    assert PASSWORD not in digest


def test_hashes_use_different_salts() -> None:
    first = hash_password(PASSWORD)
    second = hash_password(PASSWORD)
    assert first != second
    assert verify_password(PASSWORD, first)
    assert verify_password(PASSWORD, second)


def test_verify_accepts_correct_and_rejects_wrong_password() -> None:
    digest = hash_password(PASSWORD)
    assert verify_password(PASSWORD, digest) is True
    assert verify_password(PASSWORD + "x", digest) is False
    assert verify_password("", digest) is False


@pytest.mark.parametrize(
    "bad_hash",
    ["", "not-a-hash", "$argon2id$v=19$m=65536,t=3,p=4$broken", "$argon2i$v=19$junk"],
)
def test_verify_returns_false_for_malformed_hashes(bad_hash: str) -> None:
    assert verify_password(PASSWORD, bad_hash) is False


def test_verify_rejects_non_string_inputs() -> None:
    assert verify_password(None, "$argon2id$") is False  # type: ignore[arg-type]
    assert verify_password(PASSWORD, None) is False  # type: ignore[arg-type]


def test_hash_rejects_empty_password() -> None:
    with pytest.raises(ValueError):
        hash_password("")


def test_current_parameters_do_not_need_rehash() -> None:
    assert password_needs_rehash(hash_password(PASSWORD)) is False


def test_legacy_parameters_need_rehash() -> None:
    weaker = passwords.PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    assert password_needs_rehash(weaker) is True
    assert password_needs_rehash("not-a-hash") is False
    assert password_needs_rehash("") is False


async def test_async_helpers_round_trip() -> None:
    digest = await hash_password_async(PASSWORD)
    assert await verify_password_async(PASSWORD, digest) is True
    assert await verify_password_async("wrong-password", digest) is False
