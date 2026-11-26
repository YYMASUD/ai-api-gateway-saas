"""Unit tests for integer-only pricing arithmetic (no external services)."""

from __future__ import annotations

import pytest

from app.services.pricing import (
    ModelPricingRow,
    actual_cost,
    ceil_per_1k,
    estimate_prompt_tokens,
    reservation_estimate,
)


class _Msg:
    def __init__(self, content: object) -> None:
        self.content = content


@pytest.mark.parametrize(
    ("tokens", "price", "expected"),
    [
        (0, 0, 0),
        (1, 0, 0),
        (10000, 0, 0),  # zero-price placeholder never charges
        (1, 1000, 1),
        (999, 1, 1),
        (1000, 1, 1),
        (1001, 1, 2),
        (1500, 1, 2),
        (10, 7, 1),
        (200, 7, 2),  # 1400 / 1000 -> 2
        (10, -5, 0),  # defensive: negative price cannot produce a charge
        (-1, 10, 0),
    ],
)
def test_ceil_per_1k_uses_exact_integer_math(tokens: int, price: int, expected: int) -> None:
    result = ceil_per_1k(tokens, price)
    assert result == expected
    assert isinstance(result, int)
    assert not isinstance(result, float)


def test_ceil_per_1k_matches_formula_for_many_values() -> None:
    for tokens in (0, 1, 7, 123, 999, 1000, 4096):
        for price in (0, 1, 3, 250, 1000):
            assert ceil_per_1k(tokens, price) == (tokens * price + 999) // 1000


def test_estimate_prompt_tokens_empty_messages_is_priming_only() -> None:
    assert estimate_prompt_tokens([]) == 2


def test_estimate_prompt_tokens_is_upper_bound_heuristic() -> None:
    # ceil(4/4) + 4 framing + 2 priming = 7
    assert estimate_prompt_tokens([_Msg("aaaa")]) == 7
    # ceil(8/4) + 4 + (ceil(1/4) + 4) + 2 = 2 + 4 + 1 + 4 + 2 = 13
    assert estimate_prompt_tokens([_Msg("12345678"), _Msg("x")]) == 13


def test_estimate_prompt_tokens_handles_content_parts_and_dicts() -> None:
    parts = [{"type": "text", "text": "abcd"}, {"type": "image_url", "image_url": {"url": "x"}}]
    assert estimate_prompt_tokens([_Msg(parts)]) == 7
    assert estimate_prompt_tokens([{"content": "abcd", "role": "user"}]) == 7
    assert estimate_prompt_tokens([_Msg(None), _Msg(123)]) == 2 + 4 + 4


def test_reservation_estimate_zero_pricing_is_zero() -> None:
    free = ModelPricingRow(input_price_per_1k=0, output_price_per_1k=0)
    assert reservation_estimate(1000, 4096, free) == 0
    assert reservation_estimate(1000, None, free) == 0


def test_reservation_estimate_includes_output_budget() -> None:
    row = ModelPricingRow(input_price_per_1k=1, output_price_per_1k=1)
    assert reservation_estimate(1000, 2000, row) == 1 + 2
    assert reservation_estimate(1000, None, row) == 1


def test_actual_cost_sums_input_and_output_ceils() -> None:
    row = ModelPricingRow(input_price_per_1k=10, output_price_per_1k=20)
    # ceil(5*10/1000)=1, ceil(3*20/1000)=1
    assert actual_cost(5, 3, row) == 2
    assert actual_cost(0, 0, row) == 0
    assert actual_cost(100, 100, row) == 1 + 2
