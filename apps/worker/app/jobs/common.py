"""Shared types for Phase 1 worker job stubs."""

from typing import Literal, TypedDict


class JobResult(TypedDict):
    """Serializable result returned by a Phase 1 no-op job."""

    status: Literal["noop"]
    job: str
