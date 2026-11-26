"""Redacting JSON logging configuration for the worker only."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, MutableMapping
from typing import Any, cast

import structlog

_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "body",
    "credential",
    "key",
    "message",
    "messages",
    "password",
    "passwd",
    "payload",
    "private_key",
    "prompt",
    "secret",
    "token",
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)\S+"),
    re.compile(r"(?i)(redis(?:s)?://)[^@\s]+@"),
)


def _is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _scrub_string(value: str) -> str:
    for pattern in _SECRET_VALUE_PATTERNS:
        value = pattern.sub(lambda match: f"{match.group(1)}{_REDACTED}", value)
    return value


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: (_REDACTED if _is_sensitive_key(key) else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    if isinstance(value, str):
        return _scrub_string(value)
    return value


def redact_sensitive_fields(
    logger: Any,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> Mapping[str, Any]:
    """Structlog processor that redacts secrets, prompts, and credentialed URLs."""
    del logger, method_name
    return cast(Mapping[str, Any], _redact(event_dict))


def configure_logging(level: str = "INFO") -> None:
    """Configure structured JSON logs with redaction enabled."""
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level, logging.INFO),
        force=True,
    )
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_sensitive_fields,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
