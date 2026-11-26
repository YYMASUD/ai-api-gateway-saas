"""Credit pack catalog and integer math tests."""

from __future__ import annotations

import pytest

from app.services.credit_packs import (
    CENTS_PER_CREDIT,
    MAX_CENTS,
    MICROCREDITS_PER_CENT,
    PACKS,
    get_pack,
    microcredits_for_cents,
    pack_for_cents,
)


def test_one_cent_equals_ten_thousand_microcredits() -> None:
    """The core invariant: 1 cent = 10,000 microcredits (integer math only)."""
    assert microcredits_for_cents(1) == 10_000
    assert isinstance(microcredits_for_cents(1), int)


def test_microcredits_per_cent_is_integer_constant() -> None:
    assert MICROCREDITS_PER_CENT == 10_000
    assert isinstance(MICROCREDITS_PER_CENT, int)


def test_cents_per_credit_is_integer_constant() -> None:
    assert CENTS_PER_CREDIT == 100
    assert isinstance(CENTS_PER_CREDIT, int)


def test_pack_catalog_has_three_packs() -> None:
    assert set(PACKS.keys()) == {"pack_10", "pack_50", "pack_100"}


def test_pack_10_amounts() -> None:
    pack = PACKS["pack_10"]
    assert pack.amount_cents == 1_000
    assert pack.credits == 10
    assert pack.microcredits == 10_000_000
    assert isinstance(pack.microcredits, int)


def test_pack_50_amounts() -> None:
    pack = PACKS["pack_50"]
    assert pack.amount_cents == 5_000
    assert pack.credits == 50
    assert pack.microcredits == 50_000_000


def test_pack_100_amounts() -> None:
    pack = PACKS["pack_100"]
    assert pack.amount_cents == 10_000
    assert pack.credits == 100
    assert pack.microcredits == 100_000_000


def test_get_pack_returns_known_packs() -> None:
    assert get_pack("pack_10") is not None
    assert get_pack("pack_50") is not None
    assert get_pack("pack_100") is not None


def test_get_pack_returns_none_for_unknown() -> None:
    assert get_pack("pack_999") is None
    assert get_pack("") is None


def test_pack_for_cents_matches_amount() -> None:
    assert pack_for_cents(1_000) is PACKS["pack_10"]
    assert pack_for_cents(5_000) is PACKS["pack_50"]
    assert pack_for_cents(10_000) is PACKS["pack_100"]


def test_pack_for_cents_returns_none_for_unknown_amount() -> None:
    assert pack_for_cents(999) is None
    assert pack_for_cents(1) is None


def test_microcredits_for_cents_rejects_bool() -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        microcredits_for_cents(True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must be an integer"):
        microcredits_for_cents(False)  # type: ignore[arg-type]


def test_microcredits_for_cents_rejects_negative() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        microcredits_for_cents(-1)


def test_microcredits_for_cents_rejects_zero() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        microcredits_for_cents(0)


def test_microcredits_for_cents_rejects_float() -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        microcredits_for_cents(1.5)  # type: ignore[arg-type]


def test_microcredits_for_cents_rejects_string() -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        microcredits_for_cents("100")  # type: ignore[arg-type]


def test_microcredits_for_cents_bigint_bound() -> None:
    # MAX_CENTS is the largest cent value whose microcredit amount fits in BIGINT.
    assert microcredits_for_cents(MAX_CENTS) == MAX_CENTS * MICROCREDITS_PER_CENT
    # One cent above that would overflow.
    with pytest.raises(ValueError, match="exceeds the supported BIGINT range"):
        microcredits_for_cents(MAX_CENTS + 1)


def test_pack_for_cents_validates_input() -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        pack_for_cents(True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must be positive"):
        pack_for_cents(-100)
    with pytest.raises(ValueError, match="must be positive"):
        pack_for_cents(0)


def test_no_float_in_pack_math() -> None:
    """Verify pack microcredits are computed with integer math, never float."""
    for pack in PACKS.values():
        assert isinstance(pack.amount_cents, int)
        assert isinstance(pack.credits, int)
        assert isinstance(pack.microcredits, int)
        # Recompute from scratch to prove no float was involved.
        expected = pack.amount_cents * MICROCREDITS_PER_CENT
        assert pack.microcredits == expected
        assert isinstance(expected, int)
