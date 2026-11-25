"""Password hashing with Argon2id.

Argon2id provides constant-time verification and resistance to side-channel,
GPU, and ASIC attacks. The default parameters balance security and performance
for typical web authentication workloads.
"""

from __future__ import annotations

import asyncio

from argon2 import PasswordHasher
from argon2.exceptions import HashingError, InvalidHashError, VerificationError
from argon2.low_level import Type

# Argon2id with OWASP-recommended defaults for password storage.
_password_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # 64 MB
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


def hash_password(password: str) -> str:
    """Hash a password using Argon2id.

    The returned string includes the algorithm parameters and salt, so it can
    be stored directly in the database.
    """
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    try:
        return _password_hasher.hash(password)
    except HashingError as exc:
        raise ValueError("password hashing failed") from exc


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against an Argon2id hash.

    Uses constant-time comparison internally. Returns False for invalid hashes
    or mismatched passwords without raising exceptions.
    """
    if not isinstance(password, str) or not isinstance(password_hash, str):
        return False
    if not password or not password_hash:
        return False
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """Check whether a hash should be recomputed with current parameters.

    Returns True when the hash uses outdated parameters (e.g., lower memory
    cost). Callers should rehash on successful login when this returns True.
    """
    if not isinstance(password_hash, str) or not password_hash:
        return False
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except ValueError:  # includes InvalidHashError
        return False


async def hash_password_async(password: str) -> str:
    """Async wrapper for hash_password to avoid blocking the event loop."""
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, password_hash: str) -> bool:
    """Async wrapper for verify_password to avoid blocking the event loop."""
    return await asyncio.to_thread(verify_password, password, password_hash)


__all__ = [
    "hash_password",
    "hash_password_async",
    "password_needs_rehash",
    "verify_password",
    "verify_password_async",
]
