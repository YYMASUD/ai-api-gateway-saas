"""Credit pack catalog and integer-only cent → microcredit conversion.

Money is never represented as a float. One US dollar buys one credit; a credit
is 1,000,000 microcredits and 100 cents, hence exactly 10,000 microcredits per
cent::

    CENTS_PER_CREDIT      = 100
    MICROCREDITS_PER_CREDIT = 1_000_000
    MICROCREDITS_PER_CENT = 10_000

Every pack is defined by its fiat ``amount_cents`` and derived ``credits`` /
``microcredits`` using pure integer arithmetic. The Stripe price id for a pack
lives in settings (``stripe_price_10`` …); ``price_setting`` names that field so
the catalog itself carries no credentials.
"""

from __future__ import annotations

from dataclasses import dataclass

BIGINT_MAX = 2**63 - 1
CENTS_PER_CREDIT = 100
MICROCREDITS_PER_CREDIT = 1_000_000
MICROCREDITS_PER_CENT = MICROCREDITS_PER_CREDIT // CENTS_PER_CREDIT
# Largest cent amount whose microcredit value still fits in a signed BIGINT.
MAX_CENTS = BIGINT_MAX // MICROCREDITS_PER_CENT

__all__ = [
    "BIGINT_MAX",
    "CENTS_PER_CREDIT",
    "MAX_CENTS",
    "MICROCREDITS_PER_CENT",
    "MICROCREDITS_PER_CREDIT",
    "PACKS",
    "Pack",
    "get_pack",
    "microcredits_for_cents",
    "pack_for_cents",
]


def _strict_positive_int(value: object, *, name: str) -> int:
    """Return ``value`` if it is a positive int within BIGINT bounds.

    ``bool`` is rejected explicitly because ``True``/``False`` are ``int``
    subclasses; every other non-int, zero, negative, or out-of-range value is
    also rejected so no float or truncated amount can slip through.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    if value > MAX_CENTS:
        raise ValueError(f"{name} exceeds the supported BIGINT range")
    return value


def microcredits_for_cents(cents: int) -> int:
    """Convert fiat cents to microcredits with integer math only.

    ``1 cent`` is exactly ``10_000`` microcredits. The multiplication is checked
    against the signed BIGINT ceiling before it is performed so a stored value
    can never overflow its column.
    """
    amount = _strict_positive_int(cents, name="cents")
    return amount * MICROCREDITS_PER_CENT


@dataclass(frozen=True, slots=True)
class Pack:
    """A purchasable credit pack.

    ``amount_cents`` is the fiat charge, ``credits`` the whole credits bought,
    and ``microcredits`` the derived ledger amount. All three are integers.
    """

    id: str
    amount_cents: int
    credits: int
    price_setting: str

    @property
    def microcredits(self) -> int:
        """Ledger amount for this pack, derived with integer math only."""
        return microcredits_for_cents(self.amount_cents)


def _pack(pack_id: str, amount_cents: int, price_setting: str) -> Pack:
    if amount_cents % CENTS_PER_CREDIT != 0:
        raise ValueError(f"pack {pack_id} amount must be a whole number of credits")
    return Pack(
        id=pack_id,
        amount_cents=amount_cents,
        credits=amount_cents // CENTS_PER_CREDIT,
        price_setting=price_setting,
    )


PACKS: dict[str, Pack] = {
    pack.id: pack
    for pack in (
        _pack("pack_10", 1_000, "stripe_price_10"),
        _pack("pack_50", 5_000, "stripe_price_50"),
        _pack("pack_100", 10_000, "stripe_price_100"),
    )
}

_BY_CENTS: dict[int, Pack] = {pack.amount_cents: pack for pack in PACKS.values()}


def get_pack(pack_id: str) -> Pack | None:
    """Return the pack with ``pack_id`` or ``None`` when it is not in the catalog."""
    return PACKS.get(pack_id)


def pack_for_cents(cents: int) -> Pack | None:
    """Return the pack whose fiat price is exactly ``cents``, else ``None``.

    ``cents`` is validated as a strict positive integer so a boolean, float,
    negative, or zero amount returns/raises rather than silently matching.
    """
    amount = _strict_positive_int(cents, name="cents")
    return _BY_CENTS.get(amount)
