"""Credit service unit tests with in-memory repositories (no database)."""

from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.core.errors import (
    IdempotencyConflictError,
    InsufficientCreditsError,
    MonthlySpendCapExceededError,
    ReservationExpiredError,
    ReservationNotFoundError,
)
from app.db.models import CreditBalance, CreditLedger, CreditReservation, User
from app.services.credit_service import (
    BIGINT_MAX,
    CreditService,
    ReserveRequest,
    SQLAlchemyCreditRepository,
)
from tests.fakes import make_user


class FakeCreditRepository:
    """In-memory credit persistence for testing."""

    def __init__(self) -> None:
        self.users: dict[uuid.UUID, User] = {}
        self.balances: dict[uuid.UUID, CreditBalance] = {}
        self.reservations: dict[uuid.UUID, CreditReservation] = {}
        self.ledgers: list[CreditLedger] = []
        self.events: list[str] = []
        self.flushes = 0

    def register_user(self, user: User) -> None:
        self.users[user.id] = user

    def seed_balance(self, user_id: uuid.UUID, balance: int, reserved: int = 0) -> CreditBalance:
        now = datetime.now(UTC)
        row = CreditBalance(
            id=uuid.uuid4(),
            user_id=user_id,
            balance=balance,
            reserved=reserved,
            lifetime_purchased_microcredits=0,
            lifetime_spent_microcredits=0,
            created_at=now,
            updated_at=now,
        )
        self.balances[user_id] = row
        return row

    async def lock_user(self, user_id: uuid.UUID) -> User | None:
        self.events.append(f"lock_user:{user_id}")
        return self.users.get(user_id)

    async def get_or_create_balance(self, user_id: uuid.UUID) -> CreditBalance:
        self.events.append("balance")
        if user_id not in self.balances:
            self.seed_balance(user_id, 0)
        return self.balances[user_id]

    async def get_balance(self, user_id: uuid.UUID) -> CreditBalance | None:
        return self.balances.get(user_id)

    async def get_reservation(self, reservation_id: uuid.UUID) -> CreditReservation | None:
        self.events.append("reservation_probe")
        return self.reservations.get(reservation_id)

    async def get_reservation_for_update(
        self, reservation_id: uuid.UUID
    ) -> CreditReservation | None:
        self.events.append("reservation_lock")
        return self.reservations.get(reservation_id)

    async def get_reservation_by_request_id(self, request_id: str) -> CreditReservation | None:
        for reservation in self.reservations.values():
            if reservation.request_id == request_id:
                return reservation
        return None

    async def create_reservation(self, reservation: CreditReservation) -> None:
        self.events.append("create_reservation")
        self.reservations[reservation.id] = reservation

    async def create_ledger(self, ledger: CreditLedger) -> None:
        self.ledgers.append(ledger)

    async def get_ledger_by_idempotency_key(self, key: str) -> CreditLedger | None:
        for ledger in self.ledgers:
            if ledger.idempotency_key == key:
                return ledger
        return None

    async def get_ledger_by_id(self, ledger_id: str) -> CreditLedger | None:
        try:
            parsed = uuid.UUID(ledger_id)
        except (ValueError, AttributeError, TypeError):
            return None
        for ledger in self.ledgers:
            if ledger.id == parsed:
                return ledger
        return None

    async def month_settled_spend(self, user_id: uuid.UUID) -> int:
        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (month_start + timedelta(days=32)).replace(day=1)
        total = 0
        for ledger in self.ledgers:
            if (
                ledger.user_id == user_id
                and ledger.type == "settlement"
                and ledger.amount_microcredits < 0
                and month_start <= ledger.created_at < next_month
            ):
                total += -ledger.amount_microcredits
        return total

    async def sum_ledger_amount(self, user_id: uuid.UUID) -> int:
        return sum(
            ledger.amount_microcredits for ledger in self.ledgers if ledger.user_id == user_id
        )

    async def sum_open_reservations(self, user_id: uuid.UUID) -> int:
        return sum(
            reservation.amount_microcredits
            for reservation in self.reservations.values()
            if reservation.user_id == user_id and reservation.status == "open"
        )

    async def list_expired_open(self, *, now: datetime, limit: int) -> list[CreditReservation]:
        expired = [
            reservation
            for reservation in self.reservations.values()
            if reservation.status == "open" and reservation.expires_at < now
        ]
        expired.sort(key=lambda reservation: reservation.expires_at)
        return expired[:limit]

    async def list_ledger(
        self, *, user_id: uuid.UUID, limit: int, cursor: str | None
    ) -> list[CreditLedger]:
        rows = [entry for entry in self.ledgers if entry.user_id == user_id]
        rows.sort(key=lambda entry: (entry.created_at, entry.id), reverse=True)
        if cursor is not None:
            anchor = await self.get_ledger_by_id(cursor)
            if anchor is not None and anchor.user_id == user_id:
                rows = [
                    entry
                    for entry in rows
                    if (entry.created_at, entry.id) < (anchor.created_at, anchor.id)
                ]
        return rows[:limit]

    async def flush(self) -> None:
        self.flushes += 1

    async def refresh(self, instance: object) -> None:
        pass


def _settlement(user_id: uuid.UUID, amount: int, *, when: datetime) -> CreditLedger:
    return CreditLedger(
        id=uuid.uuid4(),
        user_id=user_id,
        type="settlement",
        amount_microcredits=-amount,
        reference_id="old-req",
        idempotency_key=f"settle:{uuid.uuid4()}:{amount}",
        metadata_json={},
        created_at=when,
        updated_at=when,
    )


def _reserve_request(**overrides: Any) -> ReserveRequest:
    defaults: dict[str, Any] = {
        "user_id": uuid.uuid4(),
        "amount_microcredits": 10,
        "request_id": "req",
    }
    defaults.update(overrides)
    return ReserveRequest(**defaults)


# ---------------------------------------------------------------------------
# reserve
# ---------------------------------------------------------------------------


async def test_reserve_moves_available_into_reserved_only() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    balance = repo.seed_balance(user.id, 1000)
    service = CreditService(repo)
    before = datetime.now(UTC)

    reservation = await service.reserve(
        None, user_id=user.id, amount_microcredits=300, request_id="req-1"
    )

    assert reservation.status == "open"
    assert reservation.amount_microcredits == 300
    assert reservation.cost_microcredits == 0
    # balance is total unspent: reserving only lowers available.
    assert balance.balance == 1000
    assert balance.reserved == 300
    assert reservation.expires_at >= before + timedelta(seconds=899)
    assert len(repo.ledgers) == 1
    entry = repo.ledgers[0]
    assert entry.type == "reservation"
    assert entry.amount_microcredits == 0
    assert entry.idempotency_key == "reserve:req-1"
    assert entry.metadata_json["reservation_id"] == str(reservation.id)


async def test_reserve_allows_exactly_available() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    balance = repo.seed_balance(user.id, 1000, reserved=700)
    service = CreditService(repo)

    reservation = await service.reserve(
        None, user_id=user.id, amount_microcredits=300, request_id="req-exact"
    )

    assert reservation.status == "open"
    assert balance.balance == 1000
    assert balance.reserved == 1000


async def test_reserve_insufficient_credits_raises_402() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, 100)
    service = CreditService(repo)

    with pytest.raises(InsufficientCreditsError) as exc:
        await service.reserve(None, user_id=user.id, amount_microcredits=200, request_id="req-2")
    assert exc.value.status_code == 402
    assert exc.value.code == "insufficient_credits"


async def test_reserve_monthly_spend_cap_exceeded() -> None:
    user = make_user()
    user.spend_cap_monthly_microcredits = 500
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, 10000)
    repo.ledgers.append(_settlement(user.id, 400, when=datetime.now(UTC)))
    service = CreditService(repo)

    with pytest.raises(MonthlySpendCapExceededError) as exc:
        await service.reserve(None, user_id=user.id, amount_microcredits=200, request_id="req-3")
    assert exc.value.status_code == 402
    assert exc.value.code == "monthly_spend_cap_exceeded"


async def test_reserve_monthly_cap_allows_exact_boundary() -> None:
    user = make_user()
    user.spend_cap_monthly_microcredits = 500
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, 10000)
    repo.ledgers.append(_settlement(user.id, 400, when=datetime.now(UTC)))
    service = CreditService(repo)

    reservation = await service.reserve(
        None, user_id=user.id, amount_microcredits=100, request_id="req-cap-exact"
    )
    assert reservation.status == "open"


async def test_reserve_monthly_cap_disabled_when_zero() -> None:
    user = make_user()
    user.spend_cap_monthly_microcredits = 0
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, 10_000)
    repo.ledgers.append(_settlement(user.id, 9_000, when=datetime.now(UTC)))
    service = CreditService(repo)

    reservation = await service.reserve(
        None, user_id=user.id, amount_microcredits=1_000, request_id="req-cap-off"
    )
    assert reservation.status == "open"


async def test_reserve_monthly_cap_ignores_previous_month_spend() -> None:
    user = make_user()
    user.spend_cap_monthly_microcredits = 500
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, 10_000)
    this_month = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_month = this_month - timedelta(days=1)
    repo.ledgers.append(_settlement(user.id, 10_000, when=previous_month))
    service = CreditService(repo)

    reservation = await service.reserve(
        None, user_id=user.id, amount_microcredits=500, request_id="req-cap-prev"
    )
    assert reservation.status == "open"


async def test_reserve_idempotent_replay_returns_same_reservation() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    balance = repo.seed_balance(user.id, 1000)
    service = CreditService(repo)

    first = await service.reserve(
        None, user_id=user.id, amount_microcredits=300, request_id="req-4"
    )
    second = await service.reserve(
        None, user_id=user.id, amount_microcredits=300, request_id="req-4"
    )

    assert first.id == second.id
    assert balance.balance == 1000
    assert balance.reserved == 300  # held only once
    assert len(repo.ledgers) == 1


async def test_reserve_idempotency_conflict_different_amount() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, 1000)
    service = CreditService(repo)

    await service.reserve(None, user_id=user.id, amount_microcredits=300, request_id="req-5")
    with pytest.raises(IdempotencyConflictError) as exc:
        await service.reserve(None, user_id=user.id, amount_microcredits=400, request_id="req-5")
    assert exc.value.status_code == 409
    assert exc.value.code == "idempotency_conflict"


async def test_reserve_idempotency_conflict_different_api_key() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, 1000)
    service = CreditService(repo)

    await service.reserve(
        None,
        user_id=user.id,
        amount_microcredits=300,
        request_id="req-key",
        api_key_id=uuid.uuid4(),
    )
    with pytest.raises(IdempotencyConflictError):
        await service.reserve(
            None,
            user_id=user.id,
            amount_microcredits=300,
            request_id="req-key",
            api_key_id=uuid.uuid4(),
        )


async def test_reserve_records_api_key() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, 1000)
    service = CreditService(repo)
    key_id = uuid.uuid4()

    reservation = await service.reserve(
        None,
        user_id=user.id,
        amount_microcredits=100,
        request_id="req-apikey",
        api_key_id=key_id,
    )
    assert reservation.api_key_id == key_id


async def test_reserve_locks_user_then_balance_then_reservation() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, 1000)
    service = CreditService(repo)

    await service.reserve(None, user_id=user.id, amount_microcredits=100, request_id="req-lock")

    assert repo.events[0] == f"lock_user:{user.id}"
    assert repo.events.index("balance") < repo.events.index("create_reservation")


@pytest.mark.parametrize("amount", [True, False, -1, 0, 1.5, "10", None])
async def test_reserve_rejects_invalid_amounts(amount: object) -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, 1000)
    service = CreditService(repo)

    with pytest.raises(ValueError):
        await service.reserve(
            None,
            user_id=user.id,
            amount_microcredits=amount,  # type: ignore[arg-type]
            request_id="req-bad",
        )


async def test_reserve_rejects_amount_above_bigint_max() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, BIGINT_MAX)
    service = CreditService(repo)

    with pytest.raises(ValueError, match="BIGINT"):
        await service.reserve(
            None,
            user_id=user.id,
            amount_microcredits=BIGINT_MAX + 1,
            request_id="req-overflow",
        )


# ---------------------------------------------------------------------------
# settle
# ---------------------------------------------------------------------------


async def _reserve(
    repo: FakeCreditRepository, user: User, amount: int, request_id: str
) -> CreditReservation:
    return await CreditService(repo).reserve(
        None, user_id=user.id, amount_microcredits=amount, request_id=request_id
    )


async def test_settle_and_refund_follow_lock_order() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, 1000)
    service = CreditService(repo)
    reservation = await _reserve(repo, user, 100, "req-order-settle")
    repo.events.clear()
    await service.settle(
        None, reservation_id=reservation.id, cost_microcredits=50, request_id="s-order"
    )
    assert repo.events.index(f"lock_user:{user.id}") < repo.events.index("balance")
    assert repo.events.index("balance") < repo.events.index("reservation_lock")

    other = await _reserve(repo, user, 100, "req-order-refund")
    repo.events.clear()
    await service.refund(None, reservation_id=other.id, reason="cancel", request_id="r-order")
    assert repo.events.index(f"lock_user:{user.id}") < repo.events.index("balance")
    assert repo.events.index("balance") < repo.events.index("reservation_lock")


async def test_settle_full_cost_debits_balance() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    balance = repo.seed_balance(user.id, 1000)
    service = CreditService(repo)

    reservation = await _reserve(repo, user, 300, "req-6")
    settled = await service.settle(
        None,
        reservation_id=reservation.id,
        cost_microcredits=300,
        request_id="settle-6",
    )

    assert settled.status == "settled"
    assert settled.cost_microcredits == 300
    assert settled.settled_at is not None
    assert balance.balance == 700
    assert balance.reserved == 0
    assert balance.lifetime_spent_microcredits == 300
    assert len(repo.ledgers) == 2
    assert repo.ledgers[1].type == "settlement"
    assert repo.ledgers[1].amount_microcredits == -300


async def test_settle_partial_cost_releases_remainder() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    balance = repo.seed_balance(user.id, 1000)
    service = CreditService(repo)

    reservation = await _reserve(repo, user, 300, "req-7")
    settled = await service.settle(
        None,
        reservation_id=reservation.id,
        cost_microcredits=100,
        request_id="settle-7",
    )

    assert settled.cost_microcredits == 100
    assert balance.balance == 900
    assert balance.reserved == 0
    assert balance.lifetime_spent_microcredits == 100


async def test_settle_zero_cost_leaves_balance_intact() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    balance = repo.seed_balance(user.id, 1000)
    service = CreditService(repo)

    reservation = await _reserve(repo, user, 300, "req-zero")
    settled = await service.settle(
        None,
        reservation_id=reservation.id,
        cost_microcredits=0,
        request_id="settle-zero",
    )

    assert settled.status == "settled"
    assert balance.balance == 1000
    assert balance.reserved == 0
    assert balance.lifetime_spent_microcredits == 0


async def test_settle_idempotent_replay_charges_once() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    balance = repo.seed_balance(user.id, 1000)
    service = CreditService(repo)
    reservation = await _reserve(repo, user, 300, "req-replay")

    first = await service.settle(
        None,
        reservation_id=reservation.id,
        cost_microcredits=100,
        request_id="settle-a",
    )
    second = await service.settle(
        None,
        reservation_id=reservation.id,
        cost_microcredits=100,
        request_id="settle-b",
    )

    assert first.id == second.id
    assert balance.balance == 900  # charged only once
    assert len(repo.ledgers) == 2


async def test_settle_conflicting_cost_after_settlement_is_409() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, 1000)
    service = CreditService(repo)
    reservation = await _reserve(repo, user, 300, "req-conflict")

    await service.settle(
        None, reservation_id=reservation.id, cost_microcredits=100, request_id="s1"
    )
    with pytest.raises(IdempotencyConflictError) as exc:
        await service.settle(
            None, reservation_id=reservation.id, cost_microcredits=200, request_id="s2"
        )
    assert exc.value.status_code == 409


async def test_settle_cost_above_reserved_is_rejected() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, 1000)
    service = CreditService(repo)
    reservation = await _reserve(repo, user, 300, "req-too-much")

    with pytest.raises(InsufficientCreditsError):
        await service.settle(
            None,
            reservation_id=reservation.id,
            cost_microcredits=301,
            request_id="settle-too-much",
        )


async def test_settle_expired_reservation_fails() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, 1000)
    service = CreditService(repo)
    reservation = await service.reserve(
        None,
        user_id=user.id,
        amount_microcredits=300,
        request_id="req-8",
        expires_in_seconds=0,
    )
    reservation.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(ReservationExpiredError) as exc:
        await service.settle(
            None,
            reservation_id=reservation.id,
            cost_microcredits=100,
            request_id="settle-8",
        )
    assert exc.value.status_code == 410
    assert exc.value.code == "reservation_expired"


async def test_settle_released_reservation_is_conflict() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, 1000)
    service = CreditService(repo)
    reservation = await _reserve(repo, user, 300, "req-released")
    await service.refund(
        None, reservation_id=reservation.id, reason="cancel", request_id="refund-r"
    )

    with pytest.raises(IdempotencyConflictError):
        await service.settle(
            None, reservation_id=reservation.id, cost_microcredits=100, request_id="s-r"
        )


async def test_settle_not_found() -> None:
    repo = FakeCreditRepository()
    service = CreditService(repo)

    with pytest.raises(ReservationNotFoundError):
        await service.settle(
            None,
            reservation_id=uuid.uuid4(),
            cost_microcredits=100,
            request_id="settle-x",
        )


@pytest.mark.parametrize("cost", [True, -1, 1.5, "1", None])
async def test_settle_rejects_invalid_cost(cost: object) -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, 1000)
    service = CreditService(repo)
    reservation = await _reserve(repo, user, 100, "req-bad-cost")

    with pytest.raises(ValueError):
        await service.settle(
            None,
            reservation_id=reservation.id,
            cost_microcredits=cost,  # type: ignore[arg-type]
            request_id="settle-bad",
        )


# ---------------------------------------------------------------------------
# refund / expiry
# ---------------------------------------------------------------------------


async def test_refund_releases_reservation_without_changing_balance() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    balance = repo.seed_balance(user.id, 1000)
    service = CreditService(repo)
    reservation = await _reserve(repo, user, 300, "req-9")

    refunded = await service.refund(
        None,
        reservation_id=reservation.id,
        reason="user_cancelled",
        request_id="refund-9",
    )

    assert refunded.status == "released"
    assert refunded.released_at is not None
    assert balance.balance == 1000
    assert balance.reserved == 0
    assert len(repo.ledgers) == 2
    assert repo.ledgers[1].type == "refund"
    assert repo.ledgers[1].amount_microcredits == 0


async def test_refund_idempotent_replay_is_noop() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    balance = repo.seed_balance(user.id, 1000)
    service = CreditService(repo)
    reservation = await _reserve(repo, user, 300, "req-refund-replay")

    first = await service.refund(
        None, reservation_id=reservation.id, reason="cancel", request_id="r1"
    )
    second = await service.refund(
        None, reservation_id=reservation.id, reason="cancel", request_id="r2"
    )

    assert first.id == second.id
    assert balance.reserved == 0
    assert len(repo.ledgers) == 2


async def test_refund_with_different_reason_after_release_is_conflict() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, 1000)
    service = CreditService(repo)
    reservation = await _reserve(repo, user, 300, "req-refund-conflict")

    await service.refund(None, reservation_id=reservation.id, reason="cancel", request_id="r1")
    with pytest.raises(IdempotencyConflictError):
        await service.refund(None, reservation_id=reservation.id, reason="other", request_id="r2")


async def test_refund_settled_reservation_is_conflict() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, 1000)
    service = CreditService(repo)
    reservation = await _reserve(repo, user, 300, "req-refund-settled")
    await service.settle(
        None, reservation_id=reservation.id, cost_microcredits=100, request_id="s1"
    )

    with pytest.raises(IdempotencyConflictError):
        await service.refund(None, reservation_id=reservation.id, reason="cancel", request_id="r1")


async def test_refund_not_found() -> None:
    repo = FakeCreditRepository()
    service = CreditService(repo)
    with pytest.raises(ReservationNotFoundError):
        await service.refund(None, reservation_id=uuid.uuid4(), reason="cancel", request_id="r")


async def test_refund_can_release_expired_open_reservation() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    balance = repo.seed_balance(user.id, 1000)
    service = CreditService(repo)
    reservation = await _reserve(repo, user, 300, "req-expired-refund")
    reservation.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    refunded = await service.refund(
        None, reservation_id=reservation.id, reason="expired", request_id="r-exp"
    )

    assert refunded.status == "released"
    assert balance.balance == 1000
    assert balance.reserved == 0


async def test_release_expired_reservations() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    balance = repo.seed_balance(user.id, 1000)
    service = CreditService(repo)

    await _reserve(repo, user, 200, "req-10")
    expired = await service.reserve(
        None,
        user_id=user.id,
        amount_microcredits=300,
        request_id="req-11",
        expires_in_seconds=0,
    )
    expired.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    released = await service.release_expired(None, limit=100)

    assert released == 1
    assert balance.balance == 1000
    assert balance.reserved == 200
    assert expired.status == "released"
    assert repo.ledgers[-1].type == "refund"
    assert repo.ledgers[-1].amount_microcredits == 0


async def test_release_expired_respects_limit() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    balance = repo.seed_balance(user.id, 1000)
    service = CreditService(repo)
    for index in range(3):
        reservation = await _reserve(repo, user, 100, f"req-limit-{index}")
        reservation.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    released = await service.release_expired(None, limit=2)

    assert released == 2
    assert balance.reserved == 100


async def test_release_expired_with_no_candidates_returns_zero() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    balance = repo.seed_balance(user.id, 1000)
    service = CreditService(repo)
    await _reserve(repo, user, 100, "req-open")

    assert await service.release_expired(None) == 0
    assert balance.reserved == 100
    assert repo.flushes == 1  # only the earlier reserve


# ---------------------------------------------------------------------------
# purchases / balance / reconciliation
# ---------------------------------------------------------------------------


async def test_grant_purchase_increments_balance_and_lifetime() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    service = CreditService(repo)

    ledger = await service.grant_purchase(
        None,
        user_id=user.id,
        amount_microcredits=5000,
        reference_id="stripe-session-123",
        idempotency_key="ext-key-123",
    )

    assert ledger.type == "purchase"
    assert ledger.amount_microcredits == 5000
    assert ledger.idempotency_key == "purchase:stripe-session-123"
    balance = await repo.get_balance(user.id)
    assert balance is not None
    assert balance.balance == 5000
    assert balance.lifetime_purchased_microcredits == 5000


async def test_grant_purchase_idempotent_replay() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    service = CreditService(repo)

    first = await service.grant_purchase(
        None,
        user_id=user.id,
        amount_microcredits=5000,
        reference_id="stripe-session-456",
        idempotency_key="ext-key-456",
    )
    second = await service.grant_purchase(
        None,
        user_id=user.id,
        amount_microcredits=5000,
        reference_id="stripe-session-456",
        idempotency_key="ext-key-456",
    )

    assert first.id == second.id
    balance = await repo.get_balance(user.id)
    assert balance is not None
    assert balance.balance == 5000


async def test_grant_purchase_conflicting_amount_is_conflict() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    service = CreditService(repo)
    await service.grant_purchase(
        None,
        user_id=user.id,
        amount_microcredits=5000,
        reference_id="ref-conflict",
        idempotency_key="idem-1",
    )

    with pytest.raises(IdempotencyConflictError):
        await service.grant_purchase(
            None,
            user_id=user.id,
            amount_microcredits=6000,
            reference_id="ref-conflict",
            idempotency_key="idem-1",
        )


@pytest.mark.parametrize("amount", [True, -1, 0, 1.5, None, BIGINT_MAX + 1])
async def test_grant_purchase_rejects_invalid_amount(amount: object) -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    service = CreditService(repo)

    with pytest.raises(ValueError):
        await service.grant_purchase(
            None,
            user_id=user.id,
            amount_microcredits=amount,  # type: ignore[arg-type]
            reference_id="ref-bad",
            idempotency_key="idem-bad",
        )


async def test_reconcile_user_consistent_with_ledger() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    service = CreditService(repo)
    await service.grant_purchase(
        None,
        user_id=user.id,
        amount_microcredits=1000,
        reference_id="ref-1",
        idempotency_key="idem-1",
    )
    await service.reserve(None, user_id=user.id, amount_microcredits=300, request_id="req-12")

    result = await service.reconcile_user(None, user_id=user.id)

    assert result["balance_microcredits"] == 1000
    assert result["ledger_balance_microcredits"] == 1000
    assert result["reserved_microcredits"] == 300
    assert result["open_reserved_microcredits"] == 300
    assert result["available_microcredits"] == 700
    assert result["consistent"] is True


async def test_reconcile_user_detects_balance_drift() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    balance = repo.seed_balance(user.id, 1000)
    service = CreditService(repo)
    balance.balance = 999  # simulate drift from the ledger

    result = await service.reconcile_user(None, user_id=user.id)

    assert result["consistent"] is False


async def test_get_balance_returns_existing_row() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    row = repo.seed_balance(user.id, 1234, reserved=56)

    balance = await CreditService(repo).get_balance(None, user_id=user.id)

    assert balance is row
    assert balance.balance == 1234
    assert balance.reserved == 56


async def test_get_balance_returns_zeros_for_new_user() -> None:
    user = make_user()
    repo = FakeCreditRepository()
    service = CreditService(repo)

    balance = await service.get_balance(None, user_id=user.id)

    assert balance.balance == 0
    assert balance.reserved == 0
    assert balance.lifetime_purchased_microcredits == 0
    assert balance.lifetime_spent_microcredits == 0
    # The fallback must not create or flush a row.
    assert user.id not in repo.balances
    assert repo.flushes == 0


async def test_list_ledger_delegates_and_filters_owner() -> None:
    owner = make_user()
    other = make_user(email="other@example.com")
    repo = FakeCreditRepository()
    repo.register_user(owner)
    repo.register_user(other)
    now = datetime.now(UTC)
    repo.ledgers.append(_settlement(owner.id, 10, when=now))
    repo.ledgers.append(_settlement(other.id, 20, when=now))

    rows = await CreditService(repo).list_ledger(None, user_id=owner.id, limit=10)

    assert len(rows) == 1
    assert rows[0].user_id == owner.id


async def test_service_never_commits() -> None:
    class CommitForbidden(FakeCreditRepository):
        async def commit(self) -> None:  # pragma: no cover - must not be called
            raise AssertionError("service must not commit")

        async def rollback(self) -> None:  # pragma: no cover - must not be called
            raise AssertionError("service must not rollback")

    user = make_user()
    repo = CommitForbidden()
    repo.register_user(user)
    repo.seed_balance(user.id, 1000)

    await CreditService(repo).reserve(
        None, user_id=user.id, amount_microcredits=100, request_id="req-commit"
    )
    assert repo.flushes == 1


async def test_reserve_request_defaults_and_immutability() -> None:
    request = _reserve_request()

    assert request.api_key_id is None
    assert request.expires_in_seconds == 900
    with pytest.raises(FrozenInstanceError):
        request.request_id = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SQLAlchemy repository (SQL generation only; no database connection)
# ---------------------------------------------------------------------------


class _StubScalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _StubResult:
    def __init__(self, *, scalar: Any = None, rows: list[Any] | None = None) -> None:
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def scalar_one(self) -> Any:
        return self._scalar

    def scalars(self) -> _StubScalars:
        return _StubScalars(self._rows)


class RecordingSession:
    """Records executed statements and returns queued stub results."""

    def __init__(self, results: list[_StubResult] | None = None) -> None:
        self.statements: list[Any] = []
        self.results = list(results or [])
        self.added: list[object] = []
        self.flushed = False
        self.refreshed: list[object] = []

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def execute(self, statement: Any) -> _StubResult:
        self.statements.append(statement)
        if self.results:
            return self.results.pop(0)
        return _StubResult()

    async def flush(self) -> None:
        self.flushed = True

    async def refresh(self, instance: object) -> None:
        self.refreshed.append(instance)


def _sql(statement: Any) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


async def test_repository_get_or_create_balance_upserts_then_locks() -> None:
    sentinel = object()
    session = RecordingSession([_StubResult(), _StubResult(scalar=sentinel)])
    repo = SQLAlchemyCreditRepository(session)  # type: ignore[arg-type]

    balance = await repo.get_or_create_balance(uuid.uuid4())

    assert balance is sentinel
    assert len(session.statements) == 2
    assert "ON CONFLICT" in _sql(session.statements[0])
    assert "FOR UPDATE" in _sql(session.statements[1])


async def test_repository_lock_user_uses_for_update() -> None:
    user = make_user()
    session = RecordingSession([_StubResult(scalar=user)])
    repo = SQLAlchemyCreditRepository(session)  # type: ignore[arg-type]

    locked = await repo.lock_user(user.id)

    assert locked is user
    assert "FOR UPDATE" in _sql(session.statements[0])


async def test_repository_month_settled_spend_uses_date_trunc() -> None:
    session = RecordingSession([_StubResult(scalar=123)])
    repo = SQLAlchemyCreditRepository(session)  # type: ignore[arg-type]

    spend = await repo.month_settled_spend(uuid.uuid4())

    assert spend == 123
    sql = _sql(session.statements[0])
    assert "date_trunc('month', now())" in sql
    assert "interval '1 month'" in sql
    assert "settlement" in sql


async def test_repository_list_ledger_keyset_cursor() -> None:
    user_id = uuid.uuid4()
    anchor = CreditLedger(
        id=uuid.uuid4(),
        user_id=user_id,
        type="purchase",
        amount_microcredits=10,
        reference_id=None,
        idempotency_key="k",
        metadata_json={},
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session = RecordingSession([_StubResult(scalar=anchor), _StubResult(rows=[anchor])])
    repo = SQLAlchemyCreditRepository(session)  # type: ignore[arg-type]

    rows = await repo.list_ledger(user_id=user_id, limit=10, cursor=str(anchor.id))

    assert rows == [anchor]
    assert len(session.statements) == 2
    sql = _sql(session.statements[1])
    assert "created_at <" in sql
    assert "ORDER BY credit_ledger.created_at DESC" in sql
    assert "LIMIT 10" in sql


async def test_repository_list_ledger_skips_invalid_cursor() -> None:
    session = RecordingSession([_StubResult(rows=[])])
    repo = SQLAlchemyCreditRepository(session)  # type: ignore[arg-type]

    rows = await repo.list_ledger(user_id=uuid.uuid4(), limit=5, cursor="not-a-uuid")

    assert rows == []
    assert len(session.statements) == 1
    assert "created_at <" not in _sql(session.statements[0])


async def test_repository_aggregates_and_lists() -> None:
    session = RecordingSession(
        [
            _StubResult(scalar=7),
            _StubResult(scalar=8),
            _StubResult(rows=[]),
            _StubResult(rows=[]),
        ]
    )
    repo = SQLAlchemyCreditRepository(session)  # type: ignore[arg-type]
    user_id = uuid.uuid4()

    assert await repo.sum_ledger_amount(user_id) == 7
    assert await repo.sum_open_reservations(user_id) == 8
    assert await repo.list_expired_open(now=datetime.now(UTC), limit=3) == []
    assert await repo.list_ledger(user_id=user_id, limit=3, cursor=None) == []


async def test_repository_getters_delegate() -> None:
    balance = object()
    user_id = uuid.uuid4()
    reservation = object()
    key = object()
    session = RecordingSession(
        [
            _StubResult(scalar=balance),
            _StubResult(scalar=None),
            _StubResult(scalar=reservation),
            _StubResult(scalar=reservation),
            _StubResult(scalar=reservation),
            _StubResult(scalar=key),
        ]
    )
    repo = SQLAlchemyCreditRepository(session)  # type: ignore[arg-type]

    assert await repo.get_balance(user_id) is balance
    assert await repo.get_balance(user_id) is None
    assert await repo.get_reservation(uuid.uuid4()) is reservation
    assert await repo.get_reservation_for_update(uuid.uuid4()) is reservation
    assert await repo.get_reservation_by_request_id("req") is reservation
    assert await repo.get_ledger_by_idempotency_key("key") is key


async def test_repository_mutations_and_flush_delegate() -> None:
    session = RecordingSession()
    repo = SQLAlchemyCreditRepository(session)  # type: ignore[arg-type]
    reservation = CreditReservation(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        api_key_id=None,
        request_id="req",
        status="open",
        amount_microcredits=1,
        cost_microcredits=0,
        expires_at=datetime.now(UTC),
        settled_at=None,
        released_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    ledger = CreditLedger(
        id=uuid.uuid4(),
        user_id=reservation.user_id,
        type="reservation",
        amount_microcredits=0,
        reference_id="req",
        idempotency_key="reserve:req",
        metadata_json={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    await repo.create_reservation(reservation)
    await repo.create_ledger(ledger)
    await repo.refresh(reservation)
    await repo.flush()

    assert reservation in session.added
    assert ledger in session.added
    assert reservation in session.refreshed
    assert session.flushed is True
