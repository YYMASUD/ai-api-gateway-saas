"""Request/response contracts for Stripe Checkout (Phase 5)."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, field_validator

from app.services.credit_packs import Pack

__all__ = ["CheckoutRequest", "CheckoutResponse"]

# Non-local redirect URLs must use TLS; plain HTTP is allowed only for local
# development so the blank-by-default local configuration keeps working.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _valid_redirect_url(value: str | None) -> str | None:
    if value is None:
        return None
    parts = urlsplit(value)
    if parts.scheme == "https" and parts.netloc and not parts.username and not parts.password:
        return value
    if (
        parts.scheme == "http"
        and parts.hostname in _LOCAL_HOSTS
        and not parts.username
        and not parts.password
    ):
        return value
    raise ValueError("must be an HTTPS URL or an http://localhost URL")


class CheckoutRequest(BaseModel):
    """Client request to start a credit-pack Checkout session.

    ``pack_id`` must be a catalog id; unknown ids are rejected by validation
    (422) before any database or Stripe work happens. Redirect URLs are
    optional — omitted values fall back to the configured local defaults.
    """

    pack_id: str
    success_url: str | None = None
    cancel_url: str | None = None

    @field_validator("pack_id")
    @classmethod
    def _known_pack(cls, value: str) -> str:
        from app.services.credit_packs import get_pack

        if get_pack(value) is None:
            raise ValueError(f"unknown pack_id: {value}")
        return value

    @field_validator("success_url", "cancel_url")
    @classmethod
    def _redirect_url(cls, value: str | None) -> str | None:
        return _valid_redirect_url(value)


class CheckoutResponse(BaseModel):
    """The Checkout URL and the integer amounts represented by the purchase."""

    checkout_url: str
    session_id: str
    amount_cents: int
    credits: int
    microcredits: int

    @classmethod
    def from_pack(cls, *, pack: Pack, session_id: str, checkout_url: str) -> CheckoutResponse:
        return cls(
            checkout_url=checkout_url,
            session_id=session_id,
            amount_cents=pack.amount_cents,
            credits=pack.credits,
            microcredits=pack.microcredits,
        )
