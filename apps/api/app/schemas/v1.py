"""Request/response schemas for the OpenAI-compatible ``/v1`` surface.

Only the request shape is validated here; the route performs the
settings-dependent bounds (``max_tokens`` against ``max_output_tokens``) and
the model allow-list lookup. Message ``content`` is intentionally typed as
``Any`` because OpenAI accepts both a plain string and a list of typed parts;
the gateway never inspects, logs, or persists it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ChatCompletionRequest", "ChatMessage"]


class ChatMessage(BaseModel):
    """A single chat message.

    ``content`` is ``Any`` so both ``"text"`` and the OpenAI content-part array
    forms pass through unchanged. ``name`` is an optional participant label.
    Extra keys are permitted so future OpenAI message fields survive the hop.
    """

    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = None
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible chat completion request.

    Extra fields are allowed (``model_config``) so optional OpenAI SDK
    parameters the gateway does not model (``frequency_penalty``, ``seed``,
    ``response_format``, …) are forwarded upstream untouched. Only a small,
    well-understood subset is validated here.
    """

    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1, max_length=128)
    messages: list[ChatMessage] = Field(min_length=1)
    max_tokens: int | None = Field(default=None, ge=1)
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    stop: str | list[str] | None = None
    user: str | None = None
