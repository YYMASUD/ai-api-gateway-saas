"""Request and response contracts for auth and API-key management."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

ALLOWED_API_KEY_SCOPES = frozenset({"chat:completions", "models:read"})
# Stable ordering for the default scope set (frozenset iteration is unordered).
DEFAULT_API_KEY_SCOPES = ("chat:completions", "models:read")


class CredentialsRequest(BaseModel):
    """Email input shared by registration and login."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr

    @field_validator("email", mode="before")
    @classmethod
    def _normalise_email(cls, value: object) -> object:
        # Trim/lowercase before EmailStr validation so padded input is accepted.
        if isinstance(value, str):
            return value.strip().lower()
        return value


class RegisterRequest(CredentialsRequest):
    """Registration input enforcing the 12-128 character password policy."""

    password: str = Field(min_length=12, max_length=128, repr=False)
    display_name: str | None = Field(default=None, max_length=255)

    @field_validator("display_name")
    @classmethod
    def _normalise_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class LoginRequest(CredentialsRequest):
    """Login input.

    Password bounds are deliberately loose: any wrong or out-of-policy password
    must produce the same generic ``401 invalid_credentials`` rather than a
    validation ``422`` that hints at account state.
    """

    password: str = Field(min_length=1, max_length=256, repr=False)


class UserResponse(BaseModel):
    """Public user fields; credentials and hashes never appear."""

    id: str
    email: str
    display_name: str | None
    role: str
    status: str
    created_at: datetime


class TokenResponse(BaseModel):
    """Access-token envelope returned by registration and login."""

    access_token: str = Field(repr=False)
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


class APIKeyCreateRequest(BaseModel):
    """Named API-key input with an explicit allowlist of scopes."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    scopes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_API_KEY_SCOPES),
        min_length=1,
        max_length=2,
    )

    @field_validator("name")
    @classmethod
    def _normalise_name(cls, value: str) -> str:
        normalised = value.strip()
        if not normalised:
            raise ValueError("name must not be blank")
        return normalised

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, value: list[str]) -> list[str]:
        if set(value) - ALLOWED_API_KEY_SCOPES:
            raise ValueError("unknown API key scope")
        return list(dict.fromkeys(value))


class APIKeyResponse(BaseModel):
    """Non-secret API-key metadata returned by list and creation."""

    id: str
    name: str
    prefix: str
    scopes: list[str]
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class APIKeyCreateResponse(APIKeyResponse):
    """Creation response; ``key`` is populated only at creation time."""

    key: str = Field(repr=False)


__all__ = [
    "ALLOWED_API_KEY_SCOPES",
    "DEFAULT_API_KEY_SCOPES",
    "APIKeyCreateRequest",
    "APIKeyCreateResponse",
    "APIKeyResponse",
    "CredentialsRequest",
    "LoginRequest",
    "RegisterRequest",
    "TokenResponse",
    "UserResponse",
]
