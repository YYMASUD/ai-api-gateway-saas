"""Email delivery job stub. Provider integration arrives in a later phase."""

from typing import Any

from worker_app.jobs.common import JobResult


async def send_email(
    ctx: dict[str, Any],
    *,
    template: str,
    recipient_id: str,
    idempotency_key: str,
) -> JobResult:
    """Send a templated email to an existing user.

    Phase 1 only acknowledges the job. Later phases will resolve the recipient
    address server-side from ``recipient_id`` and use ``idempotency_key`` to
    prevent duplicate sends. Email addresses and rendered content are never
    logged by this worker.
    """
    del ctx, template, recipient_id, idempotency_key
    # TODO(phase-2): render the template and deliver through the email provider.
    return {"status": "noop", "job": "send_email"}
