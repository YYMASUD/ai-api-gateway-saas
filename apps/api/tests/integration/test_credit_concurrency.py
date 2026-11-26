"""Phase 3 integration tests: concurrent credit reservations.

These tests validate the credit service's row-locking and idempotency
guarantees against a real PostgreSQL database.

Authoritative accounting model (see ``docs/ARCHITECTURE.md`` and
``docs/SECURITY.md``):

* ``balance`` is TOTAL unspent credits and equals ``sum(credit_ledger)``.
* ``reserved`` is the sum of open reservation holds.
* ``available = balance - reserved``.

Operation effects:

* ``reserve(A)``      — balance unchanged, ``reserved += A``, writes a
  zero-amount ``reservation`` ledger row.
* ``settle(R, C)``    — ``reserved -= R``, ``balance -= C``, settlement ledger
  row ``-C``.
* ``refund``/``release`` — ``reserved -= R``, balance unchanged, zero-amount
  ``refund`` ledger row.
* ``grant_purchase``  — ``balance += amount``.

The acceptance criterion is that 100 parallel ``reserve()`` calls against a
small balance never produce a negative balance or a wrong reservation count.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import InsufficientCreditsError
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


async def _try_reserve(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    request_id: str,
    amount: int,
) -> tuple[str, Any]:
    """Run one ``reserve()`` in its own session and classify the outcome."""
    async with session_factory() as session:
        service = CreditService()
        try:
            reservation = await service.reserve(
                session,
                user_id=user_id,
                amount_microcredits=amount,
                request_id=request_id,
            )
            await session.commit()
            return ("ok", reservation)
        except InsufficientCreditsError:
            await session.rollback()
            return ("insufficient", None)
        except Exception as exc:
            await session.rollback()
            return ("error", exc)


async def _get_balance(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> CreditBalance | None:
    async with session_factory() as session:
        result = await session.execute(
            select(CreditBalance).where(CreditBalance.user_id == user_id)
        )
        return result.scalar_one_or_none()


async def _count_reservations(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> int:
    async with session_factory() as session:
        result = await session.execute(
            select(CreditReservation).where(CreditReservation.user_id == user_id)
        )
        return len(list(result.scalars().all()))


async def _reservation_ledger_rows(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> list[CreditLedger]:
    async with session_factory() as session:
        result = await session.execute(
            select(CreditLedger).where(
                CreditLedger.user_id == user_id,
                CreditLedger.type == "reservation",
            )
        )
        return list(result.scalars().all())


async def _ledger_total(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> int:
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT COALESCE(SUM(amount_microcredits), 0) "
                "FROM credit_ledger WHERE user_id = :uid"
            ),
            {"uid": user_id},
        )
        return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_100_parallel_reserves_never_overdraw(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """balance=1000, 100 reserves of 10 → all succeed; balance=1000, reserved=1000."""
    async with session_factory() as setup_session:
        user = await _create_user(
            setup_session,
            email=f"full-{uuid.uuid4()}@example.com",
            balance_microcredits=1000,
        )
        user_id = user.id

    n = 100
    amount = 10
    barrier = asyncio.Barrier(n)

    async def worker(i: int) -> tuple[str, Any]:
        await barrier.wait()
        return await _try_reserve(
            session_factory,
            user_id=user_id,
            request_id=f"req-full-{i}",
            amount=amount,
        )

    results = await asyncio.gather(*(worker(i) for i in range(n)))
    successes = [r for r in results if r[0] == "ok"]
    insufficient = [r for r in results if r[0] == "insufficient"]
    errors = [r for r in results if r[0] == "error"]

    assert not errors, f"unexpected errors: {errors}"
    assert len(successes) == n, (
        f"expected all {n} reservations to fit; got {len(successes)} successes "
        f"and {len(insufficient)} insufficient-credit failures"
    )

    balance = await _get_balance(session_factory, user_id)
    assert balance is not None
    # Total model: balance stays at sum(ledger); holds only raise reserved.
    assert balance.balance == 1000, f"balance should be total 1000, got {balance.balance}"
    assert balance.reserved == n * amount
    assert balance.balance - balance.reserved == 0  # available
    assert balance.balance >= 0
    assert balance.reserved >= 0

    # Exactly one zero-amount reservation ledger row per success.
    rows = await _reservation_ledger_rows(session_factory, user_id)
    assert len(rows) == n
    assert all(row.amount_microcredits == 0 for row in rows)
    assert len({row.reference_id for row in rows}) == n
    assert await _count_reservations(session_factory, user_id) == n
    # Ledger total is unchanged by holds.
    assert await _ledger_total(session_factory, user_id) == 1000


async def test_100_parallel_reserves_with_partial_balance(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """balance=50, 100 reserves of 10 → exactly 5 succeed; balance=50, reserved=50."""
    async with session_factory() as setup_session:
        user = await _create_user(
            setup_session,
            email=f"partial-{uuid.uuid4()}@example.com",
            balance_microcredits=50,
        )
        user_id = user.id

    n = 100
    amount = 10
    expected_successes = 50 // amount  # 5
    barrier = asyncio.Barrier(n)

    async def worker(i: int) -> tuple[str, Any]:
        await barrier.wait()
        return await _try_reserve(
            session_factory,
            user_id=user_id,
            request_id=f"req-partial-{i}",
            amount=amount,
        )

    results = await asyncio.gather(*(worker(i) for i in range(n)))
    successes = [r for r in results if r[0] == "ok"]
    insufficient = [r for r in results if r[0] == "insufficient"]
    errors = [r for r in results if r[0] == "error"]

    assert not errors, f"unexpected errors: {errors}"
    assert (
        len(successes) == expected_successes
    ), f"expected exactly {expected_successes} successes; got {len(successes)}"
    assert len(insufficient) == n - expected_successes

    balance = await _get_balance(session_factory, user_id)
    assert balance is not None
    assert balance.balance == 50
    assert balance.reserved == expected_successes * amount
    assert balance.balance - balance.reserved == 0  # available
    assert balance.balance >= 0
    assert balance.reserved >= 0

    rows = await _reservation_ledger_rows(session_factory, user_id)
    assert len(rows) == expected_successes
    assert all(row.amount_microcredits == 0 for row in rows)
    assert await _count_reservations(session_factory, user_id) == expected_successes
    assert await _ledger_total(session_factory, user_id) == 50


async def test_idempotent_reserve_replay(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two concurrent reserve() calls with one request_id → one logical operation."""
    async with session_factory() as setup_session:
        user = await _create_user(
            setup_session,
            email=f"idem-{uuid.uuid4()}@example.com",
            balance_microcredits=1000,
        )
        user_id = user.id

    request_id = f"req-idem-{uuid.uuid4()}"
    barrier = asyncio.Barrier(2)

    async def worker() -> tuple[str, Any]:
        await barrier.wait()
        return await _try_reserve(
            session_factory,
            user_id=user_id,
            request_id=request_id,
            amount=25,
        )

    results = await asyncio.gather(worker(), worker())
    successes = [r for r in results if r[0] == "ok"]
    errors = [r for r in results if r[0] == "error"]

    assert (
        len(successes) == 2
    ), f"both replays should succeed with the same logical operation; got {results}"
    assert not errors, f"unexpected errors: {errors}"

    # Identical results: same reservation id and amount.
    first = successes[0][1]
    second = successes[1][1]
    assert first.id == second.id
    assert first.amount_microcredits == second.amount_microcredits == 25

    # Exactly one reservation row and one zero-amount reservation ledger row.
    assert await _count_reservations(session_factory, user_id) == 1
    rows = await _reservation_ledger_rows(session_factory, user_id)
    assert len(rows) == 1
    assert rows[0].amount_microcredits == 0

    balance = await _get_balance(session_factory, user_id)
    assert balance is not None
    assert balance.balance == 1000
    assert balance.reserved == 25
