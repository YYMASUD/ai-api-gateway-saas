"""Phase 3 integration tests: reconciliation and expired-reservation release.

Authoritative accounting model (see ``docs/ARCHITECTURE.md`` and
``docs/SECURITY.md``):

* ``balance`` is TOTAL unspent credits and equals ``sum(credit_ledger)``.
* ``reserved`` is the sum of open reservation holds.
* ``available = balance - reserved``.
* ``reserve(A)``: balance unchanged, ``reserved += A``.
* ``settle(R, C)``: ``reserved -= R``, ``balance -= C``.
* ``refund``/``release``: ``reserved -= R``, balance unchanged.
* ``grant_purchase``: ``balance += amount``.

``reconcile_user()`` is a read-only check that ``credit_balances`` matches the
ledger and open reservations; it must never mutate the database.
``release_expired()`` frees stale holds without changing the total balance and
writes a zero-amount refund ledger row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import CreditBalance, CreditLedger, CreditReservation, User
from app.services.credit_service import CreditService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_user(session: AsyncSession, *, email: str, balance_microcredits: int = 0) -> User:
    """Insert a user plus (optionally) a matching purchase/balance seed."""
    user = User(id=uuid.uuid4(), email=email)
    session.add(user)
    await session.flush()
    if balance_microcredits > 0:
        session.add(
            CreditBalance(
                id=uuid.uuid4(),
                user_id=user.id,
                balance=balance_microcredits,
                reserved=0,
                lifetime_purchased_microcredits=balance_microcredits,
                lifetime_spent_microcredits=0,
            )
        )
        session.add(
            CreditLedger(
                id=uuid.uuid4(),
                user_id=user.id,
                type="purchase",
                amount_microcredits=balance_microcredits,
                reference_id=f"seed:{user.id}",
                idempotency_key=f"purchase:seed:{user.id}",
                metadata_json={"seed": True},
            )
        )
    await session.commit()
    await session.refresh(user)
    return user


async def _run_mixed_sequence(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> None:
    """grant(1000) → reserve(100) → settle(100, 50) → reserve(200) → refund.

    Under the authoritative model the final state is:
      balance=950, reserved=0, open=0, ledger_sum=950.
    """
    service = CreditService()

    async with session_factory() as s:
        await service.grant_purchase(
            s,
            user_id=user_id,
            amount_microcredits=1000,
            reference_id="seq-purchase-1",
            idempotency_key="seq-idem-1",
        )
        await s.commit()

    async with session_factory() as s:
        res1 = await service.reserve(
            s,
            user_id=user_id,
            amount_microcredits=100,
            request_id="seq-req-1",
        )
        res1_id = res1.id
        await s.commit()

    async with session_factory() as s:
        await service.settle(
            s,
            reservation_id=res1_id,
            cost_microcredits=50,
            request_id="seq-req-1",
        )
        await s.commit()

    async with session_factory() as s:
        res2 = await service.reserve(
            s,
            user_id=user_id,
            amount_microcredits=200,
            request_id="seq-req-2",
        )
        res2_id = res2.id
        await s.commit()

    async with session_factory() as s:
        await service.refund(
            s,
            reservation_id=res2_id,
            reason="test-refund",
            request_id="seq-req-2",
        )
        await s.commit()


async def _get_balance(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> CreditBalance | None:
    async with session_factory() as session:
        result = await session.execute(
            select(CreditBalance).where(CreditBalance.user_id == user_id)
        )
        return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_reconcile_user_detects_mismatch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A directly corrupted balance is reported as inconsistent and not repaired."""
    async with session_factory() as setup:
        user = await _create_user(
            setup,
            email=f"recon-dirty-{uuid.uuid4()}@example.com",
        )
        user_id = user.id

    await _run_mixed_sequence(session_factory, user_id)

    # Corrupt the materialised balance directly; the ledger still sums to 950.
    async with session_factory() as s:
        await s.execute(
            text("UPDATE credit_balances SET balance = 500 WHERE user_id = :uid"),
            {"uid": user_id},
        )
        await s.commit()

    before = await _get_balance(session_factory, user_id)
    assert before is not None
    assert before.balance == 500

    service = CreditService()
    async with session_factory() as s:
        report = await service.reconcile_user(s, user_id=user_id)
        # The read-only check must not leave the session dirty.
        assert not s.new and not s.dirty and not s.deleted
        await s.rollback()

    assert report["consistent"] is False, f"expected mismatch, got {report}"
    assert report["balance_microcredits"] == 500
    assert report["reserved_microcredits"] == 0
    assert report["ledger_balance_microcredits"] == 950
    assert report["open_reserved_microcredits"] == 0

    # reconcile_user is read-only: the corrupted value must remain untouched.
    after = await _get_balance(session_factory, user_id)
    assert after is not None
    assert after.balance == 500, "reconcile_user must not repair the database"
    assert after.reserved == before.reserved


async def test_reconcile_user_clean_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A closed-loop sequence leaves credit_balances consistent with the ledger."""
    async with session_factory() as setup:
        user = await _create_user(
            setup,
            email=f"recon-clean-{uuid.uuid4()}@example.com",
        )
        user_id = user.id

    await _run_mixed_sequence(session_factory, user_id)

    service = CreditService()
    async with session_factory() as s:
        report = await service.reconcile_user(s, user_id=user_id)
        await s.rollback()

    assert report["consistent"] is True, f"unexpected mismatch: {report}"
    # grant(1000) - settle(50) = 950; every hold was closed.
    assert report["balance_microcredits"] == 950
    assert report["reserved_microcredits"] == 0
    assert report["available_microcredits"] == 950
    assert report["ledger_balance_microcredits"] == 950
    assert report["open_reserved_microcredits"] == 0


async def test_release_expired_reservation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """release_expired() flips an expired open hold to released and refunds it."""
    async with session_factory() as setup:
        user = await _create_user(
            setup,
            email=f"expire-{uuid.uuid4()}@example.com",
            balance_microcredits=1000,
        )
        user_id = user.id

    service = CreditService()
    request_id = f"req-expired-{uuid.uuid4()}"

    # Create the hold through the service so balance/reserved are written by
    # the real code path, then move its expiry into the past.
    async with session_factory() as s:
        reservation = await service.reserve(
            s,
            user_id=user_id,
            amount_microcredits=100,
            request_id=request_id,
        )
        reservation_id = reservation.id
        await s.commit()

    past = datetime.now(UTC) - timedelta(minutes=5)
    async with session_factory() as s:
        await s.execute(
            text("UPDATE credit_reservations SET expires_at = :past WHERE id = :rid"),
            {"past": past, "rid": reservation_id},
        )
        await s.commit()

    before = await _get_balance(session_factory, user_id)
    assert before is not None
    assert before.reserved == 100

    async with session_factory() as s:
        released_count = await service.release_expired(s, limit=100)
        await s.commit()

    assert released_count >= 1, "release_expired should report at least 1 release"

    async with session_factory() as s:
        result = await s.execute(
            select(CreditReservation).where(CreditReservation.id == reservation_id)
        )
        reservation_after = result.scalar_one()
    assert reservation_after.status == "released"
    assert reservation_after.released_at is not None

    async with session_factory() as s:
        result = await s.execute(
            select(CreditLedger).where(
                CreditLedger.user_id == user_id,
                CreditLedger.type == "refund",
            )
        )
        refund_rows = list(result.scalars().all())
    matching = [
        row for row in refund_rows if row.idempotency_key == f"release_expired:{reservation_id}"
    ]
    assert len(matching) == 1, (
        "expected one release_expired refund ledger row; found "
        f"{[r.idempotency_key for r in refund_rows]}"
    )
    assert matching[0].amount_microcredits == 0

    # Releasing a hold frees the reservation but never changes the total.
    after = await _get_balance(session_factory, user_id)
    assert after is not None
    assert after.reserved == 0
    assert after.balance == 1000
    assert after.balance - after.reserved >= 0
