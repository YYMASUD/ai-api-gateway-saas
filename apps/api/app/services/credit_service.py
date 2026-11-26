"""Credit ledger accounting with reservations (Phase 3).

``credit_balances.balance`` is the TOTAL unspent wallet and always equals
``sum(credit_ledger.amount_microcredits)``. ``credit_balances.reserved`` is
the sum of open reservation amounts, so::

    available = balance - reserved

Operations:

* ``reserve``  — hold credits: check ``balance - reserved >= A`` and the
  monthly cap, then ``reserved += A``. ``balance`` does NOT change; a
  zero-amount ``reservation`` ledger row records the hold.
* ``settle``   — charge ``C`` (``0 <= C <= A``) against the hold:
  ``reserved -= A`` and ``balance -= C``; a settlement ledger row records
  ``-C`` and lifetime spend grows by ``C``.
* ``refund``   — release the whole hold: ``reserved -= A`` only; ``balance``
  does NOT change and a zero-amount ``refund`` ledger row is written.
* ``release_expired`` — same arithmetic as ``refund`` for expired holds.

Purchases/adjustments add directly to ``balance``. The ledger is append-only;
open reservations live in a separate ``credit_reservations`` table.

Every public method takes a caller-owned ``session``: the service may
``flush()`` but MUST NOT commit. Locking order is users → credit_balances →
credit_reservations, and no row lock is ever held across an external await.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    IdempotencyConflictError,
    InsufficientCreditsError,
    MonthlySpendCapExceededError,
    ReservationExpiredError,
    ReservationNotFoundError,
)
from app.db.models import CreditBalance, CreditLedger, CreditReservation, User

BIGINT_MAX = 2**63 - 1
DEFAULT_RESERVATION_TTL_SECONDS = 900

__all__ = [
    "BIGINT_MAX",
    "CreditRepository",
    "CreditService",
    "IdempotencyConflictError",
    "InsufficientCreditsError",
    "MonthlySpendCapExceededError",
    "ReservationExpiredError",
    "ReservationNotFoundError",
    "ReserveRequest",
    "SQLAlchemyCreditRepository",
]


@dataclass(frozen=True)
class ReserveRequest:
    """A reservation request payload."""

    user_id: uuid.UUID
    amount_microcredits: int
    request_id: str
    api_key_id: uuid.UUID | None = None
    expires_in_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS


def _utcnow() -> datetime:
    """Indirection so tests can control the clock."""
    return datetime.now(UTC)


def _ensure_int(value: object, *, name: str, minimum: int) -> int:
    """Reject bools, non-ints, negatives, and values outside BIGINT range."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if value > BIGINT_MAX:
        raise ValueError(f"{name} exceeds the BIGINT range")
    return value


class CreditRepository(Protocol):
    """Persistence boundary; implemented by SQLAlchemy and by test fakes."""

    async def lock_user(self, user_id: uuid.UUID) -> User | None: ...

    async def get_or_create_balance(self, user_id: uuid.UUID) -> CreditBalance: ...

    async def get_balance(self, user_id: uuid.UUID) -> CreditBalance | None: ...

    async def get_reservation(self, reservation_id: uuid.UUID) -> CreditReservation | None: ...

    async def get_reservation_for_update(
        self, reservation_id: uuid.UUID
    ) -> CreditReservation | None: ...

    async def get_reservation_by_request_id(self, request_id: str) -> CreditReservation | None: ...

    async def create_reservation(self, reservation: CreditReservation) -> None: ...

    async def create_ledger(self, ledger: CreditLedger) -> None: ...

    async def get_ledger_by_idempotency_key(self, key: str) -> CreditLedger | None: ...

    async def month_settled_spend(self, user_id: uuid.UUID) -> int: ...

    async def sum_ledger_amount(self, user_id: uuid.UUID) -> int: ...

    async def sum_open_reservations(self, user_id: uuid.UUID) -> int: ...

    async def list_expired_open(self, *, now: datetime, limit: int) -> list[CreditReservation]: ...

    async def list_ledger(
        self, *, user_id: uuid.UUID, limit: int, cursor: str | None
    ) -> list[CreditLedger]: ...

    async def flush(self) -> None: ...

    async def refresh(self, instance: object) -> None: ...


class SQLAlchemyCreditRepository:
    """Credit persistence backed by an async SQLAlchemy session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_user(self, user_id: uuid.UUID) -> User | None:
        result = await self._session.execute(
            select(User).where(User.id == user_id).with_for_update()
        )
        return result.scalar_one_or_none()

    async def get_or_create_balance(self, user_id: uuid.UUID) -> CreditBalance:
        # INSERT ... ON CONFLICT DO NOTHING avoids a race between concurrent
        # first-time callers; the subsequent SELECT FOR UPDATE locks the row.
        await self._session.execute(
            pg_insert(CreditBalance)
            .values(user_id=user_id)
            .on_conflict_do_nothing(index_elements=["user_id"])
        )
        result = await self._session.execute(
            select(CreditBalance).where(CreditBalance.user_id == user_id).with_for_update()
        )
        return result.scalar_one()

    async def get_balance(self, user_id: uuid.UUID) -> CreditBalance | None:
        result = await self._session.execute(
            select(CreditBalance).where(CreditBalance.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_reservation(self, reservation_id: uuid.UUID) -> CreditReservation | None:
        result = await self._session.execute(
            select(CreditReservation).where(CreditReservation.id == reservation_id)
        )
        return result.scalar_one_or_none()

    async def get_reservation_for_update(
        self, reservation_id: uuid.UUID
    ) -> CreditReservation | None:
        result = await self._session.execute(
            select(CreditReservation)
            .where(CreditReservation.id == reservation_id)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def get_reservation_by_request_id(self, request_id: str) -> CreditReservation | None:
        result = await self._session.execute(
            select(CreditReservation).where(CreditReservation.request_id == request_id)
        )
        return result.scalar_one_or_none()

    async def create_reservation(self, reservation: CreditReservation) -> None:
        self._session.add(reservation)

    async def create_ledger(self, ledger: CreditLedger) -> None:
        self._session.add(ledger)

    async def get_ledger_by_idempotency_key(self, key: str) -> CreditLedger | None:
        result = await self._session.execute(
            select(CreditLedger).where(CreditLedger.idempotency_key == key)
        )
        return result.scalar_one_or_none()

    async def month_settled_spend(self, user_id: uuid.UUID) -> int:
        month_start = text("date_trunc('month', now())")
        next_month = text("date_trunc('month', now()) + interval '1 month'")
        result = await self._session.execute(
            select(func.coalesce(func.sum(-CreditLedger.amount_microcredits), 0)).where(
                CreditLedger.user_id == user_id,
                CreditLedger.type == "settlement",
                CreditLedger.amount_microcredits < 0,
                CreditLedger.created_at >= month_start,
                CreditLedger.created_at < next_month,
            )
        )
        return int(result.scalar_one())

    async def sum_ledger_amount(self, user_id: uuid.UUID) -> int:
        result = await self._session.execute(
            select(func.coalesce(func.sum(CreditLedger.amount_microcredits), 0)).where(
                CreditLedger.user_id == user_id
            )
        )
        return int(result.scalar_one())

    async def sum_open_reservations(self, user_id: uuid.UUID) -> int:
        result = await self._session.execute(
            select(func.coalesce(func.sum(CreditReservation.amount_microcredits), 0)).where(
                CreditReservation.user_id == user_id,
                CreditReservation.status == "open",
            )
        )
        return int(result.scalar_one())

    async def list_expired_open(self, *, now: datetime, limit: int) -> list[CreditReservation]:
        result = await self._session.execute(
            select(CreditReservation)
            .where(
                CreditReservation.status == "open",
                CreditReservation.expires_at < now,
            )
            .order_by(CreditReservation.expires_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_ledger(
        self, *, user_id: uuid.UUID, limit: int, cursor: str | None
    ) -> list[CreditLedger]:
        statement = select(CreditLedger).where(CreditLedger.user_id == user_id)
        if cursor is not None:
            anchor = await self._get_ledger_entry(user_id, cursor)
            if anchor is not None:
                statement = statement.where(
                    (CreditLedger.created_at < anchor.created_at)
                    | (
                        (CreditLedger.created_at == anchor.created_at)
                        & (CreditLedger.id < anchor.id)
                    )
                )
        statement = statement.order_by(
            CreditLedger.created_at.desc(), CreditLedger.id.desc()
        ).limit(limit)
        result = await self._session.execute(statement)
        return list(result.scalars().all())

    async def _get_ledger_entry(self, user_id: uuid.UUID, ledger_id: str) -> CreditLedger | None:
        try:
            parsed = uuid.UUID(ledger_id)
        except (ValueError, AttributeError, TypeError):
            return None
        result = await self._session.execute(
            select(CreditLedger).where(
                CreditLedger.id == parsed,
                CreditLedger.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def flush(self) -> None:
        await self._session.flush()

    async def refresh(self, instance: object) -> None:
        await self._session.refresh(instance)


class CreditService:
    """Reservation lifecycle and ledger accounting."""

    def __init__(self, repository: CreditRepository | AsyncSession | None = None) -> None:
        self._repository: CreditRepository | None
        if isinstance(repository, AsyncSession):
            self._repository = SQLAlchemyCreditRepository(repository)
        else:
            self._repository = repository

    def _repo(self, session: AsyncSession | None) -> CreditRepository:
        if self._repository is not None:
            return self._repository
        if session is None:
            raise RuntimeError("a session or repository is required")
        return SQLAlchemyCreditRepository(session)

    async def reserve(
        self,
        session: AsyncSession | None,
        *,
        user_id: uuid.UUID,
        amount_microcredits: int,
        request_id: str,
        api_key_id: uuid.UUID | None = None,
        expires_in_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS,
    ) -> CreditReservation:
        amount = _ensure_int(amount_microcredits, name="amount_microcredits", minimum=1)
        ttl = _ensure_int(expires_in_seconds, name="expires_in_seconds", minimum=0)
        repo = self._repo(session)

        # Lock order: users -> balances -> reservations.
        user = await repo.lock_user(user_id)

        # Idempotency is rechecked only after the user lock is held.
        existing = await repo.get_reservation_by_request_id(request_id)
        if existing is not None:
            if (
                existing.user_id != user_id
                or existing.amount_microcredits != amount
                or existing.api_key_id != api_key_id
            ):
                raise IdempotencyConflictError(
                    "request_id already used with a different reservation"
                )
            return existing

        balance = await repo.get_or_create_balance(user_id)

        cap = user.spend_cap_monthly_microcredits if user is not None else 0
        if cap is None:
            cap = 0
        if cap > 0:
            current_spend = await repo.month_settled_spend(user_id)
            if current_spend + amount > cap:
                raise MonthlySpendCapExceededError("monthly spend cap would be exceeded")

        available = balance.balance - balance.reserved
        if available < amount:
            raise InsufficientCreditsError("insufficient available credits")

        now = _utcnow()
        # Only the hold changes; ``balance`` stays the total unspent wallet.
        balance.reserved += amount
        balance.updated_at = now

        reservation = CreditReservation(
            id=uuid.uuid4(),
            user_id=user_id,
            api_key_id=api_key_id,
            request_id=request_id,
            status="open",
            amount_microcredits=amount,
            cost_microcredits=0,
            expires_at=now + timedelta(seconds=ttl),
            settled_at=None,
            released_at=None,
            created_at=now,
            updated_at=now,
        )
        await repo.create_reservation(reservation)
        await repo.create_ledger(
            CreditLedger(
                id=uuid.uuid4(),
                user_id=user_id,
                type="reservation",
                amount_microcredits=0,
                reference_id=request_id,
                idempotency_key=f"reserve:{request_id}",
                metadata_json={"reservation_id": str(reservation.id)},
                created_at=now,
                updated_at=now,
            )
        )
        await repo.flush()
        await repo.refresh(reservation)
        return reservation

    async def settle(
        self,
        session: AsyncSession | None,
        *,
        reservation_id: uuid.UUID,
        cost_microcredits: int,
        request_id: str,
    ) -> CreditReservation:
        cost = _ensure_int(cost_microcredits, name="cost_microcredits", minimum=0)
        repo = self._repo(session)

        # Resolve the owner without a lock, then follow the lock order:
        # users -> credit_balances -> credit_reservations.
        probe = await repo.get_reservation(reservation_id)
        if probe is None:
            raise ReservationNotFoundError("reservation not found")
        await repo.lock_user(probe.user_id)
        balance = await repo.get_or_create_balance(probe.user_id)
        reservation = await repo.get_reservation_for_update(reservation_id)
        if reservation is None:
            raise ReservationNotFoundError("reservation not found")

        existing_ledger = await repo.get_ledger_by_idempotency_key(
            f"settle:{reservation.id}:{cost}"
        )
        if existing_ledger is not None:
            return reservation
        if reservation.status == "settled":
            raise IdempotencyConflictError("reservation already settled with a different cost")
        if reservation.status != "open":
            raise IdempotencyConflictError("reservation is already released")
        if cost > reservation.amount_microcredits:
            raise InsufficientCreditsError("settlement cost exceeds the reserved amount")

        now = _utcnow()
        if now >= reservation.expires_at:
            raise ReservationExpiredError("reservation expired before settlement")

        amount = reservation.amount_microcredits
        # Charge ``cost`` from the total wallet and drop the whole hold.
        balance.reserved -= amount
        balance.balance -= cost
        balance.lifetime_spent_microcredits += cost
        balance.updated_at = now

        reservation.status = "settled"
        reservation.cost_microcredits = cost
        reservation.settled_at = now
        reservation.updated_at = now

        await repo.create_ledger(
            CreditLedger(
                id=uuid.uuid4(),
                user_id=reservation.user_id,
                type="settlement",
                amount_microcredits=-cost,
                reference_id=request_id,
                idempotency_key=f"settle:{reservation.id}:{cost}",
                metadata_json={
                    "reservation_id": str(reservation.id),
                    "request_id": request_id,
                },
                created_at=now,
                updated_at=now,
            )
        )
        await repo.flush()
        await repo.refresh(reservation)
        return reservation

    async def refund(
        self,
        session: AsyncSession | None,
        *,
        reservation_id: uuid.UUID,
        reason: str,
        request_id: str,
    ) -> CreditReservation:
        repo = self._repo(session)

        probe = await repo.get_reservation(reservation_id)
        if probe is None:
            raise ReservationNotFoundError("reservation not found")
        await repo.lock_user(probe.user_id)
        balance = await repo.get_or_create_balance(probe.user_id)
        reservation = await repo.get_reservation_for_update(reservation_id)
        if reservation is None:
            raise ReservationNotFoundError("reservation not found")
        existing_ledger = await repo.get_ledger_by_idempotency_key(
            f"refund:{reservation.id}:{reason}"
        )
        if existing_ledger is not None:
            return reservation
        if reservation.status == "settled":
            raise IdempotencyConflictError("reservation is already settled")
        if reservation.status != "open":
            raise IdempotencyConflictError("reservation already released with a different reason")

        now = _utcnow()
        amount = reservation.amount_microcredits
        # Releasing a hold only lowers ``reserved``; ``balance`` is unchanged.
        balance.reserved -= amount
        balance.updated_at = now

        reservation.status = "released"
        reservation.released_at = now
        reservation.updated_at = now

        await repo.create_ledger(
            CreditLedger(
                id=uuid.uuid4(),
                user_id=reservation.user_id,
                type="refund",
                amount_microcredits=0,
                reference_id=request_id,
                idempotency_key=f"refund:{reservation.id}:{reason}",
                metadata_json={
                    "reservation_id": str(reservation.id),
                    "reason": reason,
                },
                created_at=now,
                updated_at=now,
            )
        )
        await repo.flush()
        await repo.refresh(reservation)
        return reservation

    async def release_expired(self, session: AsyncSession | None, *, limit: int = 100) -> int:
        repo = self._repo(session)
        now = _utcnow()
        expired = await repo.list_expired_open(now=now, limit=limit)
        released = 0
        for candidate in expired:
            await repo.lock_user(candidate.user_id)
            balance = await repo.get_or_create_balance(candidate.user_id)
            reservation = await repo.get_reservation_for_update(candidate.id)
            if reservation is None or reservation.status != "open" or reservation.expires_at > now:
                continue
            amount = reservation.amount_microcredits
            # Expiry releases the hold; ``balance`` (total unspent) is unchanged.
            balance.reserved -= amount
            balance.updated_at = now

            reservation.status = "released"
            reservation.released_at = now
            reservation.updated_at = now

            await repo.create_ledger(
                CreditLedger(
                    id=uuid.uuid4(),
                    user_id=reservation.user_id,
                    type="refund",
                    amount_microcredits=0,
                    reference_id=reservation.request_id,
                    idempotency_key=f"release_expired:{reservation.id}",
                    metadata_json={
                        "reservation_id": str(reservation.id),
                        "reason": "expired",
                    },
                    created_at=now,
                    updated_at=now,
                )
            )
            released += 1
        if released:
            await repo.flush()
        return released

    async def grant_purchase(
        self,
        session: AsyncSession | None,
        *,
        user_id: uuid.UUID,
        amount_microcredits: int,
        reference_id: str,
        idempotency_key: str,
    ) -> CreditLedger:
        amount = _ensure_int(amount_microcredits, name="amount_microcredits", minimum=1)
        key = f"purchase:{reference_id}"
        repo = self._repo(session)

        await repo.lock_user(user_id)
        balance = await repo.get_or_create_balance(user_id)

        existing = await repo.get_ledger_by_idempotency_key(key)
        if existing is not None:
            if existing.user_id != user_id or existing.amount_microcredits != amount:
                raise IdempotencyConflictError("reference_id already used for a different purchase")
            return existing

        now = _utcnow()
        balance.balance += amount
        balance.lifetime_purchased_microcredits += amount
        balance.updated_at = now

        ledger = CreditLedger(
            id=uuid.uuid4(),
            user_id=user_id,
            type="purchase",
            amount_microcredits=amount,
            reference_id=reference_id,
            idempotency_key=key,
            metadata_json={"external_idempotency_key": idempotency_key},
            created_at=now,
            updated_at=now,
        )
        await repo.create_ledger(ledger)
        await repo.flush()
        await repo.refresh(ledger)
        return ledger

    async def get_balance(
        self, session: AsyncSession | None, *, user_id: uuid.UUID
    ) -> CreditBalance:
        repo = self._repo(session)
        balance = await repo.get_balance(user_id)
        if balance is not None:
            return balance
        # Read-only fallback so a user with no ledger still renders zeros.
        now = _utcnow()
        return CreditBalance(
            id=uuid.uuid4(),
            user_id=user_id,
            balance=0,
            reserved=0,
            lifetime_purchased_microcredits=0,
            lifetime_spent_microcredits=0,
            created_at=now,
            updated_at=now,
        )

    async def list_ledger(
        self,
        session: AsyncSession | None,
        *,
        user_id: uuid.UUID,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[CreditLedger]:
        repo = self._repo(session)
        return await repo.list_ledger(user_id=user_id, limit=limit, cursor=cursor)

    async def reconcile_user(
        self, session: AsyncSession | None, *, user_id: uuid.UUID
    ) -> dict[str, int | bool]:
        """Read-only consistency check between ledger, balance, reservations."""
        repo = self._repo(session)
        balance = await repo.get_balance(user_id)
        row_balance = balance.balance if balance is not None else 0
        row_reserved = balance.reserved if balance is not None else 0
        ledger_total = await repo.sum_ledger_amount(user_id)
        open_reserved = await repo.sum_open_reservations(user_id)
        return {
            "balance_microcredits": row_balance,
            "reserved_microcredits": row_reserved,
            "available_microcredits": row_balance - row_reserved,
            "ledger_balance_microcredits": ledger_total,
            "open_reserved_microcredits": open_reserved,
            # ``balance`` is the total unspent wallet and must equal the ledger
            # sum; zero-amount reservation rows keep that invariant intact.
            "consistent": row_balance == ledger_total and row_reserved == open_reserved,
        }
