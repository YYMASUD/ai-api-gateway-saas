"""Unit tests proving structured logs never contain sensitive payloads."""

from __future__ import annotations

import json

import pytest
import structlog

from app.core.logging import REDACTED, configure_logging, redact_sensitive_data, redact_value

SENSITIVE = "SENSITIVE-VALUE-MUST-NOT-APPEAR"

FAKE_SECRET_VALUES = [
    "sk-abcdefghijklmnopqrstuvwxyz123456",
    "sk_live_" + "fakekey1234567890",
    "whsec_fakesecret1234567890",
    "Bearer fake-bearer-token-1234567890",
    (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiJmYWtlLXVzZXIiLCJyb2xlIjoidGVzdCJ9."
        "ZmFrZS1zaWduYXR1cmUtMTIzNDU2Nzg5MA"
    ),
]


def _process(event: dict) -> dict:
    return redact_sensitive_data(None, "info", event)


def test_authorization_and_api_keys_are_redacted() -> None:
    result = _process(
        {
            "event": "request",
            "authorization": f"Bearer {SENSITIVE}",
            "api_key": SENSITIVE,
            "X-API-Key": SENSITIVE,
            "client_secret": SENSITIVE,
            "status": 200,
        }
    )
    assert result["authorization"] == REDACTED
    assert result["api_key"] == REDACTED
    assert result["X-API-Key"] == REDACTED
    assert result["client_secret"] == REDACTED
    assert result["status"] == 200
    assert SENSITIVE not in json.dumps(result)


def test_prompt_completion_and_message_content_are_redacted_recursively() -> None:
    result = redact_value(
        {
            "prompt": SENSITIVE,
            "messages": [{"role": "user", "content": SENSITIVE}],
            "completion": {"text": SENSITIVE},
            "metadata": {"nested_prompt": SENSITIVE, "model": "gpt-test"},
        }
    )
    assert result["prompt"] == REDACTED
    assert result["messages"] == REDACTED
    assert result["completion"] == REDACTED
    assert result["metadata"]["nested_prompt"] == REDACTED
    assert result["metadata"]["model"] == "gpt-test"
    assert SENSITIVE not in json.dumps(result)


def test_key_hash_is_redacted() -> None:
    key_hash = "f" * 64
    result = _process({"event": "api_key_authenticated", "key_hash": key_hash})
    assert result["key_hash"] == REDACTED
    assert key_hash not in json.dumps(result)


def test_stripe_secrets_in_free_text_are_masked() -> None:
    result = _process({"event": f"stripe callback with whsec_{SENSITIVE}"})
    assert SENSITIVE not in result["event"]
    assert REDACTED in result["event"]


def test_json_renderer_emits_redacted_json(capsys) -> None:
    configure_logging()
    logger = structlog.get_logger("test-redaction")
    logger.info(
        "upstream call",
        authorization=f"Bearer {SENSITIVE}",
        messages=[{"role": "user", "content": SENSITIVE}],
        token_value="safe-value",
    )
    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert SENSITIVE not in line
    assert payload["authorization"] == REDACTED
    assert payload["messages"] == REDACTED
    assert payload["token_value"] == "safe-value"


def test_original_mapping_is_not_mutated() -> None:
    original = {"prompt": SENSITIVE, "status": "ok"}
    redact_value(original)
    assert original == {"prompt": SENSITIVE, "status": "ok"}


@pytest.mark.parametrize("secret", FAKE_SECRET_VALUES)
def test_value_level_redaction_of_credential_shapes(secret: str) -> None:
    result = _process({"event": "upstream response", "detail": f"received {secret} from caller"})
    assert secret not in result["detail"]
    assert REDACTED in result["detail"]


@pytest.mark.parametrize("secret", FAKE_SECRET_VALUES)
def test_value_level_redaction_nested_in_plain_values(secret: str) -> None:
    result = redact_value({"note": [f"token={secret}"], "safe": "keep-me"})
    assert secret not in json.dumps(result)
    assert result["safe"] == "keep-me"
