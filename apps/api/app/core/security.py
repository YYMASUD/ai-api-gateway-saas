"""Security primitives for API keys and dashboard JWTs.

API keys are stored as SHA-256 hex digests of ``raw_key + pepper``. The pepper
is a server-side secret loaded from the environment, so a database leak alone
does not reveal usable keys. Verification uses a constant-time comparison.

JWTs are pinned to HS256 and every verification requires ``exp``, ``iss`` and
``aud`` claims. Access tokens additionally carry ``iat``, ``jti`` and
``type="access"``; the auth dependency enforces the token type.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from typing import Any

import jwt

JWT_ALGORITHM = "HS256"
JWT_REQUIRED_CLAIMS = ["exp", "iss", "aud"]
ACCESS_TOKEN_TYPE = "access"


class TokenVerificationError(Exception):
    """Raised when a dashboard JWT cannot be trusted."""


def hash_api_key(raw_key: str, pepper: str) -> str:
    """Return the lowercase SHA-256 hex digest for an API key.

    Hashing is deterministic so lookups can use an indexed ``key_hash`` column.
    """
    if not isinstance(raw_key, str) or not raw_key:
        raise ValueError("raw_key must be a non-empty string")
    if not isinstance(pepper, str) or not pepper:
        raise ValueError("pepper must be a non-empty string")
    digest = hashlib.sha256(f"{raw_key}{pepper}".encode()).hexdigest()
    return digest


def verify_api_key(raw_key: str, expected_hash: str, pepper: str) -> bool:
    """Constant-time comparison of a presented key against a stored hash."""
    if not isinstance(raw_key, str) or not isinstance(expected_hash, str):
        return False
    if not raw_key or not expected_hash:
        return False
    try:
        candidate = hash_api_key(raw_key, pepper)
    except ValueError:
        return False
    return hmac.compare_digest(candidate, expected_hash.strip().lower())


def verify_jwt(
    token: str,
    secret: str,
    issuer: str,
    audience: str,
) -> dict[str, Any]:
    """Verify a dashboard JWT and return its claims.

    The algorithm list is pinned to HS256 to reject ``alg=none`` and algorithm
    confusion attempts. ``exp``, ``iss`` and ``aud`` are mandatory.
    """
    if not token or not secret or not issuer or not audience:
        raise TokenVerificationError("missing JWT verification parameters")
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],
            audience=audience,
            issuer=issuer,
            options={"require": list(JWT_REQUIRED_CLAIMS), "verify_signature": True},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenVerificationError("token expired") from exc
    except jwt.InvalidIssuerError as exc:
        raise TokenVerificationError("invalid issuer") from exc
    except jwt.InvalidAudienceError as exc:
        raise TokenVerificationError("invalid audience") from exc
    except jwt.InvalidAlgorithmError as exc:
        raise TokenVerificationError("invalid algorithm") from exc
    except jwt.MissingRequiredClaimError as exc:
        raise TokenVerificationError(f"missing claim: {exc.claim}") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenVerificationError("invalid token") from exc
    return claims


def create_access_token(
    subject: str,
    secret: str,
    issuer: str,
    audience: str,
    expires_minutes: int,
) -> str:
    """Issue a short-lived HS256 access token for a dashboard user.

    Claims: ``sub``, ``iss``, ``aud``, ``iat``, ``exp``, ``jti`` and
    ``type="access"``. The algorithm is pinned to HS256; callers must never
    log the returned token.
    """
    if not subject or not secret or not issuer or not audience:
        raise ValueError("missing access token parameters")
    if expires_minutes <= 0:
        raise ValueError("expires_minutes must be positive")
    now = int(time.time())
    claims = {
        "sub": subject,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + expires_minutes * 60,
        "jti": str(uuid.uuid4()),
        "type": ACCESS_TOKEN_TYPE,
    }
    token: str = jwt.encode(claims, secret, algorithm=JWT_ALGORITHM)
    return token
