"""Tests for API key hashing and pinned JWT verification."""

from __future__ import annotations

import hashlib
import time
from typing import Any

import jwt
import pytest

from app.core import security
from app.core.security import (
    JWT_ALGORITHM,
    TokenVerificationError,
    hash_api_key,
    verify_api_key,
    verify_jwt,
)

RAW_KEY = "test-only-raw-api-key"
PEPPER = "test-only-pepper"
SECRET = "test-only-jwt-secret"
ISSUER = "https://issuer.example.com"
AUDIENCE = "https://api.example.com"


def test_hash_api_key_matches_sha256_of_raw_plus_pepper() -> None:
    expected = hashlib.sha256(f"{RAW_KEY}{PEPPER}".encode()).hexdigest()
    assert hash_api_key(RAW_KEY, PEPPER) == expected
    assert len(expected) == 64


def test_hash_changes_with_pepper_and_raw_key() -> None:
    assert hash_api_key(RAW_KEY, PEPPER) != hash_api_key(RAW_KEY, PEPPER + "x")
    assert hash_api_key(RAW_KEY, PEPPER) != hash_api_key(RAW_KEY + "x", PEPPER)


@pytest.mark.parametrize("raw,pepper", [("", PEPPER), (RAW_KEY, ""), (None, PEPPER)])
def test_hash_rejects_empty_inputs(raw: Any, pepper: Any) -> None:
    with pytest.raises(ValueError):
        hash_api_key(raw, pepper)


def test_verify_api_key_accepts_matching_hash_and_rejects_wrong_values() -> None:
    digest = hash_api_key(RAW_KEY, PEPPER)
    assert verify_api_key(RAW_KEY, digest, PEPPER)
    assert verify_api_key(RAW_KEY, digest.upper(), PEPPER)
    assert not verify_api_key(RAW_KEY + "x", digest, PEPPER)
    assert not verify_api_key(RAW_KEY, digest, PEPPER + "x")
    assert not verify_api_key(RAW_KEY, "", PEPPER)


def test_verify_api_key_uses_constant_time_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, Any]] = []
    original = security.hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return original(a, b)

    monkeypatch.setattr(security.hmac, "compare_digest", spy)
    digest = hash_api_key(RAW_KEY, PEPPER)
    assert verify_api_key(RAW_KEY, digest, PEPPER)
    assert len(calls) == 1


def _token(*, algorithm: str = JWT_ALGORITHM, **overrides: Any) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": "user-1",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 600,
    }
    payload.update(overrides)
    return jwt.encode(payload, SECRET, algorithm=algorithm)


def test_verify_jwt_returns_claims_for_valid_token() -> None:
    claims = verify_jwt(_token(), SECRET, ISSUER, AUDIENCE)
    assert claims["sub"] == "user-1"
    assert claims["iss"] == ISSUER
    assert claims["aud"] == AUDIENCE


@pytest.mark.parametrize(
    "token_kwargs",
    [
        {"exp": 1},
        {"iss": "https://wrong.example.com"},
        {"aud": "https://wrong.example.com"},
        {"exp": None},
    ],
)
def test_verify_jwt_rejects_bad_claims(token_kwargs: dict[str, Any]) -> None:
    with pytest.raises(TokenVerificationError):
        verify_jwt(_token(**token_kwargs), SECRET, ISSUER, AUDIENCE)


def test_verify_jwt_pins_hs256() -> None:
    with pytest.raises(TokenVerificationError):
        verify_jwt(_token(algorithm="HS512"), SECRET, ISSUER, AUDIENCE)


def test_verify_jwt_rejects_wrong_secret_and_malformed_token() -> None:
    with pytest.raises(TokenVerificationError):
        verify_jwt(_token(), "test-only-wrong-secret", ISSUER, AUDIENCE)
    with pytest.raises(TokenVerificationError):
        verify_jwt("not-a-jwt", SECRET, ISSUER, AUDIENCE)


def test_verify_jwt_rejects_unsigned_token() -> None:
    token = jwt.encode(
        {"sub": "user-1", "iss": ISSUER, "aud": AUDIENCE, "exp": 1_700_000_600},
        key="",
        algorithm="none",
    )
    with pytest.raises(TokenVerificationError):
        verify_jwt(token, SECRET, ISSUER, AUDIENCE)


def test_verify_jwt_rejects_missing_parameters() -> None:
    with pytest.raises(TokenVerificationError):
        verify_jwt(_token(), "", ISSUER, AUDIENCE)


def test_missing_required_claim_error_message() -> None:
    payload = {"sub": "user-1", "iss": ISSUER, "aud": AUDIENCE}
    token = jwt.encode(payload, SECRET, algorithm=JWT_ALGORITHM)
    with pytest.raises(TokenVerificationError) as exc:
        verify_jwt(token, SECRET, ISSUER, AUDIENCE)
    assert "missing claim" in str(exc.value)
