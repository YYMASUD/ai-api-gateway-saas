"""Integer-only token pricing arithmetic.

Prices are **microcredits per 1,000 tokens** (``BIGINT``). Every computation
is performed in Python ``int``; no floats are used anywhere, so results are
exact and independent of platform IEEE-754 behaviour.

Token counts are rounded UP (``ceil``) so a reservation always covers the
eventual settlement. ``estimate_prompt_tokens`` is a documented upper-bound
heuristic (no tokenizer dependency); the gateway settles on real upstream
usage, so any over-reservation is released at settlement time.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ModelPricingRow",
    "actual_cost",
    "ceil_per_1k",
    "estimate_prompt_tokens",
    "reservation_estimate",
]


@dataclass(frozen=True)
class ModelPricingRow:
    """Immutable pricing snapshot used by the arithmetic helpers."""

    input_price_per_1k: int
    output_price_per_1k: int


def ceil_per_1k(tokens: int, price_per_1k: int) -> int:
    """Return ``ceil(tokens * price_per_1k / 1000)`` using integer math.

    ``(tokens * price + 999) // 1000`` is the integer form of the ceiling.
    A zero (or non-positive) price or token count yields zero. Multiplying
    before dividing keeps a tiny per-1k price exact until the token count
    justifies a whole microcredit.
    """
    if tokens <= 0 or price_per_1k <= 0:
        return 0
    return (tokens * price_per_1k + 999) // 1000


def _message_text_length(message: Any) -> int:
    """Conservative character count for a message's ``content``.

    ``content`` may be a plain string, a list of OpenAI content parts, or
    ``None``. Only textual parts are counted; anything else contributes zero.
    """
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = getattr(message, "content", None)
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    total += len(text)
            elif isinstance(part, str):
                total += len(part)
        return total
    return 0


def estimate_prompt_tokens(messages: Iterable[Any]) -> int:
    """Upper-bound prompt-token estimate (no tokenizer dependency).

    Heuristic: ``sum(ceil(len(text) / 4) + 4 per message) + 2``. The +4 is
    per-message framing and the +2 is reply priming. This deliberately
    OVER-estimates so the reservation covers the real count regardless of
    tokenizer; settlement uses actual upstream usage.
    """
    total = 2
    for message in messages:
        total += (_message_text_length(message) + 3) // 4 + 4
    return total


def reservation_estimate(
    prompt_tokens: int, max_tokens: int | None, pricing: ModelPricingRow
) -> int:
    """Reservation amount assuming up to ``max_tokens`` output tokens.

    When ``max_tokens`` is ``None`` only the prompt cost is reserved; the
    route passes an explicit output budget (``settings.max_output_tokens``)
    so the hold is always an upper bound. The route clamps the result to
    ``>= 1`` because ``CreditService.reserve`` rejects zero-amount holds.
    """
    prompt_cost = ceil_per_1k(prompt_tokens, pricing.input_price_per_1k)
    if max_tokens is None:
        return prompt_cost
    return prompt_cost + ceil_per_1k(max_tokens, pricing.output_price_per_1k)


def actual_cost(prompt_tokens: int, completion_tokens: int, pricing: ModelPricingRow) -> int:
    """Settlement cost from real upstream-reported token counts."""
    return ceil_per_1k(prompt_tokens, pricing.input_price_per_1k) + ceil_per_1k(
        completion_tokens, pricing.output_price_per_1k
    )
