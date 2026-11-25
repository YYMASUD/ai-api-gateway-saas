"""Structured JSON logging with defense-in-depth redaction.

The gateway handles prompts, completions, API keys, JWTs, and Stripe secrets.
Logging is intentionally strict: sensitive keys are dropped and secret-looking
values are masked recursively before JSON rendering.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any

import structlog

REDACTED = "[REDACTED]"

_EXACT_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "api_key",
        "apikey",
        "api-key",
        "x-api-key",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "jwt",
        "jwt_secret",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "pwd",
        "prompt",
        "completion",
        "messages",
        "message",
        "content",
        "request_body",
        "response_body",
        "litellm_master_key",
        "master_key",
        "api_key_pepper",
        "pepper",
        "key_hash",
        "database_url",
        "redis_url",
        "dsn",
    }
)

_SENSITIVE_KEY_FRAGMENTS = (
    "prompt",
    "completion",
    "message",
    "secret",
    "password",
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "pepper",
)

# These patterns are applied to every string value, including free-form event
# messages and exception text.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:bearer|basic|apikey)\s+[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{8,}"),
    re.compile(r"\bwhsec_[A-Za-z0-9]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
)


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.strip().lower().replace(" ", "_")
    if normalized in _EXACT_SENSITIVE_KEYS:
        return True
    return any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS)


def redact_string(value: str) -> str:
    """Mask well-known credential formats inside a string value."""
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


def redact_value(value: Any, *, key: str | None = None, _depth: int = 0) -> Any:
    """Recursively redact sensitive values from log payloads."""
    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if _depth > 12:
        return REDACTED

    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, Mapping):
        return {str(k): redact_value(v, key=str(k), _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [redact_value(item, _depth=_depth + 1) for item in value]
    return value


def redact_sensitive_data(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> dict[str, Any]:
    """structlog processor that removes credentials and message content."""
    return {str(key): redact_value(value, key=str(key)) for key, value in event_dict.items()}


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog to emit redacted JSON lines on stdout."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_sensitive_data,
            structlog.processors.JSONRenderer(sort_keys=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> Any:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)
