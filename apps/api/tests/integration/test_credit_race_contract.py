"""Phase 3 integration tests for credit-service race conditions.

These tests exercise the fixed service contract against PostgreSQL:

* settlement is idempotent for a replayed ``request_id``;
* settlement and refund are mutually exclusive (409 on the loser);
* an expired hold cannot be settled (410) and is released exactly once;
* purchases are keyed by the caller's idempotency key;
* the monthly cap uses UTC month boundaries, not the session time zone;
* reservation request ids are unique per user (migration 0004).

Accounting model: ``balance`` is total unspent (= ledger sum), ``reserved``
is the sum of open holds, ``available = balance - reserved``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import (
    IdempotencyConflictError,
    InsufficientCreditsError,
    MonthlySpendCapExceededError,
    ReservationExpiredError,
)
from app.db.models import CreditBalance, CreditLedger, CreditReservation, User
from app.services.credit_service import CreditService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_user(session: AsyncSession, *, email: str, balance_microcredits: int = 0) -> User:
    """Insert a user and, when requested, a matching seeded wallet."""
    user = User(id=uuid.uuid4(), email=email)
    session.add(user)
    await session.flush()
    if balance_microcredits:
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
    return user


async def _run(
    session_factory: async_sessionmaker[AsyncSession],
    operation: Callable[[AsyncSession], Awaitable[Any]],
) -> tuple[str, Any]:
    """Run one operation in its own session; commit on success, rollback on error."""
    async with session_factory() as session:
        try:
            result = await operation(session)
            await session.commit()
            return ("ok", result)
        except IdempotencyConflictError as exc:
            await session.rollback()
            return ("conflict", exc)
        except ReservationExpiredError as exc:
            await session.rollback()
            return ("expired", exc)
        except MonthlySpendCapExceededError as exc:
            await session.rollback()
            return ("cap", exc)
        except InsufficientCreditsError as exc:
            await session.rollback()
            return ("insufficient", exc)
        except Exception as exc:  # classify unexpected failures in the test output
            await session.rollback()
            return ("error", exc)


async def _balance(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> CreditBalance | None:
    async with session_factory() as session:
        result = await session.execute(
            select(CreditBalance).where(CreditBalance.user_id == user_id)
        )
        return result.scalar_one_or_none()


async def _reservation(
    session_factory: async_sessionmaker[AsyncSession], reservation_id: uuid.UUID
) -> CreditReservation:
    async with session_factory() as session:
        result = await session.execute(
            select(CreditReservation).where(CreditReservation.id == reservation_id)
        )
        return result.scalar_one()


async def _ledger_rows(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    ledger_type: str,
) -> list[CreditLedger]:
    async with session_factory() as session:
        result = await session.execute(
            select(CreditLedger).where(
                CreditLedger.user_id == user_id,
                CreditLedger.type == ledger_type,
            )
        )
        return list(result.scalars().all())


async def _reconcile(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> dict[str, int | bool]:
    async with session_factory() as session:
        report = await CreditService().reconcile_user(session, user_id=user_id)
        await session.rollback()
    return report


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_concurrent_settle_charges_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Twenty simultaneous settles of one hold produce one charge and no errors."""
    async with session_factory() as session:
        user = await _create_user(
            session,
            email=f"settle-race-{uuid.uuid4()}@example.com",
            balance_microcredits=1000,
        )
    user_id = user.id
    cost = 40
    request_id = f"settle-race-req-{uuid.uuid4()}"

    service = CreditService()
    async with session_factory() as session:
        reservation = await service.reserve(
            session,
            user_id=user_id,
            amount_microcredits=100,
            request_id=request_id,
        )
        reservation_id = reservation.id
        await session.commit()

    n = 20
    barrier = asyncio.Barrier(n)

    async def worker() -> tuple[str, Any]:
        await barrier.wait()
        return await _run(
            session_factory,
            lambda s: service.settle(
                s,
                reservation_id=reservation_id,
                cost_microcredits=cost,
                request_id=request_id,
            ),
        )

    outcomes = await asyncio.gather(*(worker() for _ in range(n)))
    failures = [outcome for outcome in outcomes if outcome[0] != "ok"]
    assert not failures, f"all replays must succeed idempotently; got {failures}"
    assert all(result.id == reservation_id for _, result in outcomes)

    settlements = await _ledger_rows(session_factory, user_id, "settlement")
    assert len(settlements) == 1
    assert settlements[0].amount_microcredits == -cost
    assert settlements[0].reference_id == request_id

    reservations = await _ledger_rows(session_factory, user_id, "reservation")
    assert len(reservations) == 1
    assert reservations[0].amount_microcredits == 0

    balance = await _balance(session_factory, user_id)
    assert balance is not None
    assert balance.balance == 1000 - cost
    assert balance.reserved == 0
    assert balance.lifetime_spent_microcredits == cost
    assert (await _reservation(session_factory, reservation_id)).status == "settled"

    report = await _reconcile(session_factory, user_id)
    assert report == {
        "balance_microcredits": 1000 - cost,
        "reserved_microcredits": 0,
        "available_microcredits": 1000 - cost,
        "ledger_balance_microcredits": 1000 - cost,
        "open_reserved_microcredits": 0,
        "consistent": True,
    }


async def test_settle_vs_refund_race(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Settle and refund of one hold: exactly one wins, the other gets 409."""
    async with session_factory() as session:
        user = await _create_user(
            session,
            email=f"settle-refund-{uuid.uuid4()}@example.com",
            balance_microcredits=1000,
        )
    user_id = user.id
    cost = 25
    request_id = f"settle-refund-req-{uuid.uuid4()}"

    service = CreditService()
    async with session_factory() as session:
        reservation = await service.reserve(
            session,
            user_id=user_id,
            amount_microcredits=100,
            request_id=request_id,
        )
        reservation_id = reservation.id
        await session.commit()

    barrier = asyncio.Barrier(2)

    async def settle_worker() -> tuple[str, Any]:
        await barrier.wait()
        return await _run(
            session_factory,
            lambda s: service.settle(
                s,
                reservation_id=reservation_id,
                cost_microcredits=cost,
                request_id=request_id,
            ),
        )

    async def refund_worker() -> tuple[str, Any]:
        await barrier.wait()
        return await _run(
            session_factory,
            lambda s: service.refund(
                s,
                reservation_id=reservation_id,
                reason="race-refund",
                request_id=request_id,
            ),
        )

    settle_result, refund_result = await asyncio.gather(settle_worker(), refund_worker())
    assert {settle_result[0], refund_result[0]} == {
        "ok",
        "conflict",
    }, f"exactly one operation must win; got {settle_result} and {refund_result}"
    loser = settle_result[1] if settle_result[0] == "conflict" else refund_result[1]
    assert isinstance(loser, IdempotencyConflictError)
    assert loser.status_code == 409

    reservation_after = await _reservation(session_factory, reservation_id)
    settlements = await _ledger_rows(session_factory, user_id, "settlement")
    refunds = await _ledger_rows(session_factory, user_id, "refund")
    balance = await _balance(session_factory, user_id)
    assert balance is not None

    if settle_result[0] == "ok":
        assert reservation_after.status == "settled"
        assert reservation_after.cost_microcredits == cost
        assert len(settlements) == 1
        assert settlements[0].amount_microcredits == -cost
        assert not refunds
        assert balance.balance == 1000 - cost
        assert balance.lifetime_spent_microcredits == cost
    else:
        assert reservation_after.status == "released"
        assert reservation_after.released_at is not None
        assert not settlements
        assert len(refunds) == 1
        assert refunds[0].amount_microcredits == 0
        assert balance.balance == 1000
        assert balance.lifetime_spent_microcredits == 0
    assert balance.reserved == 0

    report = await _reconcile(session_factory, user_id)
    assert report == {
        "balance_microcredits": balance.balance,
        "reserved_microcredits": 0,
        "available_microcredits": balance.balance,
        "ledger_balance_microcredits": balance.balance,
        "open_reserved_microcredits": 0,
        "consistent": True,
    }


async def test_expiry_vs_settle_race(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An expired hold raced by the sweeper and settle: 410 and one release.

    Both contenders are in flight simultaneously, queued on the user row
    lock held by the test with settle first. PostgreSQL grants row locks in
    request order, so settle observes the still-open expired hold (410) and
    the sweeper then releases it exactly once: a deterministic interleaving
    of a genuine concurrent race, independent of scheduler timing.
    """
    async with session_factory() as session:
        user = await _create_user(
            session,
            email=f"expiry-race-{uuid.uuid4()}@example.com",
            balance_microcredits=1000,
        )
    user_id = user.id

    service = CreditService()
    request_id = f"expiry-race-req-{uuid.uuid4()}"
    async with session_factory() as session:
        reservation = await service.reserve(
            session,
            user_id=user_id,
            amount_microcredits=60,
            request_id=request_id,
        )
        reservation_id = reservation.id
        await session.commit()

    past = datetime.now(UTC) - timedelta(minutes=5)
    async with session_factory() as session:
        await session.execute(
            text("UPDATE credit_reservations SET expires_at = :past WHERE id = :rid"),
            {"past": past, "rid": reservation_id},
        )
        await session.commit()

    lock_waiters = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE wait_event_type = 'Lock' AND query ILIKE '%FROM users%FOR UPDATE%'"
    )

    async def wait_for_waiters(expected: int) -> None:
        for _ in range(500):
            async with session_factory() as session:
                count = await session.scalar(lock_waiters)
            if count is not None and count >= expected:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"expected {expected} lock waiters on users; never observed")

    barrier = asyncio.Barrier(2)

    async def settle_worker() -> tuple[str, Any]:
        await barrier.wait()
        return await _run(
            session_factory,
            lambda s: service.settle(
                s,
                reservation_id=reservation_id,
                cost_microcredits=20,
                request_id=request_id,
            ),
        )

    async def sweep_worker() -> tuple[str, Any]:
        return await _run(session_factory, lambda s: service.release_expired(s, limit=10))

    async with session_factory() as holder:
        await holder.execute(
            text("SELECT id FROM users WHERE id = :uid FOR UPDATE"), {"uid": user_id}
        )
        settle_task = asyncio.create_task(settle_worker())
        await barrier.wait()
        await wait_for_waiters(1)
        sweep_task = asyncio.create_task(sweep_worker())
        await wait_for_waiters(2)
        # Both contenders are blocked on the user lock; releasing it lets
        # settle run first, then the sweeper, while both remain concurrent.
        await holder.commit()
        settle_result, sweep_result = await asyncio.gather(settle_task, sweep_task)

    assert (
        settle_result[0] == "expired"
    ), f"settlement of an expired hold must raise ReservationExpiredError; got {settle_result}"
    assert isinstance(settle_result[1], ReservationExpiredError)
    assert settle_result[1].status_code == 410
    assert sweep_result == (
        "ok",
        1,
    ), f"the expired hold must be released exactly once; got {sweep_result}"

    reservation_after = await _reservation(session_factory, reservation_id)
    assert reservation_after.status == "released"
    assert reservation_after.released_at is not None

    reservation_rows = await _ledger_rows(session_factory, user_id, "reservation")
    refund_rows = await _ledger_rows(session_factory, user_id, "refund")
    settlement_rows = await _ledger_rows(session_factory, user_id, "settlement")
    assert len(reservation_rows) == 1
    assert len(refund_rows) == 1
    assert refund_rows[0].amount_microcredits == 0
    assert not settlement_rows, "an expired hold must never produce a settlement ledger row"

    balance = await _balance(session_factory, user_id)
    assert balance is not None
    assert balance.balance == 1000
    assert balance.reserved == 0
    assert balance.lifetime_spent_microcredits == 0

    report = await _reconcile(session_factory, user_id)
    assert report == {
        "balance_microcredits": 1000,
        "reserved_microcredits": 0,
        "available_microcredits": 1000,
        "ledger_balance_microcredits": 1000,
        "open_reserved_microcredits": 0,
        "consistent": True,
    }


async def test_concurrent_purchase_replay(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ten replays of one caller idempotency key credit the wallet exactly once."""
    async with session_factory() as session:
        user = await _create_user(session, email=f"purchase-race-{uuid.uuid4()}@example.com")
    user_id = user.id
    amount = 500
    reference_id = f"purchase-ref-{uuid.uuid4()}"
    idempotency_key = f"purchase-idem-{uuid.uuid4()}"

    service = CreditService()

    async def purchase(
        session: AsyncSession, *, reference: str, amount_microcredits: int
    ) -> CreditLedger:
        return await service.grant_purchase(
            session,
            user_id=user_id,
            amount_microcredits=amount_microcredits,
            reference_id=reference,
            idempotency_key=idempotency_key,
        )

    n = 10
    barrier = asyncio.Barrier(n)

    async def worker() -> tuple[str, Any]:
        await barrier.wait()
        return await _run(
            session_factory,
            lambda s: purchase(s, reference=reference_id, amount_microcredits=amount),
        )

    outcomes = await asyncio.gather(*(worker() for _ in range(n)))
    assert all(outcome[0] == "ok" for outcome in outcomes), f"unexpected failures: {outcomes}"
    ledger_ids = {result.id for _, result in outcomes}
    assert len(ledger_ids) == 1, "all replays must return the same ledger row"

    purchases = await _ledger_rows(session_factory, user_id, "purchase")
    assert len(purchases) == 1
    assert purchases[0].amount_microcredits == amount
    assert purchases[0].reference_id == reference_id

    balance = await _balance(session_factory, user_id)
    assert balance is not None
    assert balance.balance == amount
    assert balance.lifetime_purchased_microcredits == amount
    assert balance.reserved == 0

    # Same caller key, different fingerprint: must conflict, never double-credit.
    conflict = await _run(
        session_factory,
        lambda s: purchase(
            s,
            reference=f"purchase-ref-other-{uuid.uuid4()}",
            amount_microcredits=amount + 1,
        ),
    )
    assert (
        conflict[0] == "conflict"
    ), f"replaying the idempotency key with a different amount must conflict; got {conflict}"
    assert isinstance(conflict[1], IdempotencyConflictError)
    assert conflict[1].status_code == 409

    purchases_after = await _ledger_rows(session_factory, user_id, "purchase")
    assert len(purchases_after) == 1
    balance_after = await _balance(session_factory, user_id)
    assert balance_after is not None
    assert balance_after.balance == amount
    assert (await _reconcile(session_factory, user_id))["consistent"] is True


async def test_monthly_cap_uses_utc_not_session_timezone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Spend seeded in the previous UTC month must not count against the cap.

    The seed instant sits 4h before the UTC month start: in the previous UTC
    month, yet inside Asia/Tokyo's current local month whenever that local
    month aligns with the UTC month (its boundary is 9h earlier). A
    session-local ``date_trunc('month', now())`` then miscounts it; the fixed
    service computes UTC bounds in Python and must not. Both assertions hold
    at every point in the month.
    """
    now_utc = datetime.now(UTC)
    utc_month_start = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_month_at = utc_month_start - timedelta(hours=4)
    cap = 1000
    prev_month_spend = 400
    current_month_spend = 800
    fits = 150  # UTC: 800 + 150 <= 1000; Tokyo-local miscount: 1200 + 150 > 1000
    over = 250  # UTC: 800 + 250 > 1000, so the cap is still enforced

    async with session_factory() as session:
        user = await _create_user(
            session,
            email=f"utc-cap-{uuid.uuid4()}@example.com",
            balance_microcredits=2000,
        )
        user_id = user.id
        await session.execute(
            text("UPDATE users SET spend_cap_monthly_microcredits = :cap WHERE id = :uid"),
            {"cap": cap, "uid": user_id},
        )
        # Explicit created_at values: no dependence on database defaults.
        session.add_all(
            [
                CreditLedger(
                    id=uuid.uuid4(),
                    user_id=user_id,
                    type="settlement",
                    amount_microcredits=-prev_month_spend,
                    reference_id="utc-cap-prev-month",
                    idempotency_key=f"utc-cap-prev-{uuid.uuid4()}",
                    created_at=prev_month_at,
                    updated_at=prev_month_at,
                ),
                CreditLedger(
                    id=uuid.uuid4(),
                    user_id=user_id,
                    type="settlement",
                    amount_microcredits=-current_month_spend,
                    reference_id="utc-cap-current-month",
                    idempotency_key=f"utc-cap-cur-{uuid.uuid4()}",
                    created_at=now_utc,
                    updated_at=now_utc,
                ),
            ]
        )
        total_spend = prev_month_spend + current_month_spend
        await session.execute(
            text(
                "UPDATE credit_balances SET balance = :balance, "
                "lifetime_spent_microcredits = :spent WHERE user_id = :uid"
            ),
            {"balance": 2000 - total_spend, "spent": total_spend, "uid": user_id},
        )
        await session.commit()

    service = CreditService()
    async with session_factory() as session:
        await session.execute(text("SET TIME ZONE 'Asia/Tokyo'"))
        assert await session.scalar(text("SHOW TIME ZONE")) == "Asia/Tokyo"
        try:
            reservation = await service.reserve(
                session,
                user_id=user_id,
                amount_microcredits=fits,
                request_id=f"utc-cap-fit-{uuid.uuid4()}",
            )
            await session.commit()
        except MonthlySpendCapExceededError:
            await session.rollback()
            raise AssertionError(
                "previous-UTC-month spend must not count against this month's cap "
                "(session time zone is Asia/Tokyo)"
            ) from None
    reservation_id = reservation.id

    # The cap must still be enforced against the UTC-month spend.
    async with session_factory() as session:
        await session.execute(text("SET TIME ZONE 'Asia/Tokyo'"))
        with pytest.raises(MonthlySpendCapExceededError):
            await service.reserve(
                session,
                user_id=user_id,
                amount_microcredits=over,
                request_id=f"utc-cap-over-{uuid.uuid4()}",
            )
        await session.rollback()

    balance = await _balance(session_factory, user_id)
    assert balance is not None
    assert balance.balance == 2000 - total_spend
    assert balance.reserved == fits
    assert (await _reservation(session_factory, reservation_id)).status == "open"
    assert (await _reconcile(session_factory, user_id))["consistent"] is True


async def test_same_request_id_different_users(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One request_id can be used concurrently by two independent users."""
    async with session_factory() as session:
        user_a = await _create_user(
            session,
            email=f"shared-req-a-{uuid.uuid4()}@example.com",
            balance_microcredits=300,
        )
        user_b = await _create_user(
            session,
            email=f"shared-req-b-{uuid.uuid4()}@example.com",
            balance_microcredits=300,
        )
    request_id = f"shared-req-{uuid.uuid4()}"
    amounts = {user_a.id: 30, user_b.id: 70}
    barrier = asyncio.Barrier(2)

    async def worker(user: User) -> tuple[str, Any]:
        await barrier.wait()
        return await _run(
            session_factory,
            lambda s: CreditService().reserve(
                s,
                user_id=user.id,
                amount_microcredits=amounts[user.id],
                request_id=request_id,
            ),
        )

    outcomes = await asyncio.gather(worker(user_a), worker(user_b))
    assert all(
        outcome[0] == "ok" for outcome in outcomes
    ), f"both users must reserve independently with the same request_id; got {outcomes}"
    reservations = [result for _, result in outcomes]
    assert reservations[0].id != reservations[1].id

    for user in (user_a, user_b):
        balance = await _balance(session_factory, user.id)
        assert balance is not None
        assert balance.balance == 300
        assert balance.reserved == amounts[user.id]
        rows = await _ledger_rows(session_factory, user.id, "reservation")
        assert len(rows) == 1
        assert rows[0].amount_microcredits == 0
        assert (await _reconcile(session_factory, user.id))["consistent"] is True
