"""Tests for the worker-local logging redaction processor."""

from app.logging import redact_sensitive_fields


def test_redact_sensitive_fields() -> None:
    event = {
        "event": "job.finished",
        "api_key": "placeholder",
        "nested": {"token": "placeholder", "safe": "ok"},
        "message": "authorization: Bearer placeholder",
        "items": [{"prompt": "placeholder"}],
    }

    redacted = redact_sensitive_fields(None, "info", event)

    assert redacted["event"] == "job.finished"
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"] == {"token": "[REDACTED]", "safe": "ok"}
    assert "placeholder" not in str(redacted["message"])
    assert redacted["items"] == [{"prompt": "[REDACTED]"}]
