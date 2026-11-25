"""API-key service tests: format, hashing, cache, revocation, auditing."""

from __future__ import annotations

import hashlib
import json
import re
import uuid

import pytest

from app.config import Settings
from app.core.errors import AuthenticationError, NotFoundError
from app.core.logging import configure_logging
from app.core.security import hash_api_key
from app.services.api_key_service import (
    API_KEY_CACHE_TTL_SECONDS,
    APIKeyPrincipal,
    APIKeyService,
    cache_key_for_hash,
    display_prefix,
    generate_api_key,
)
from tests.fakes import FakeAPIKeyRepository, FakeRedis, make_user

KEY_PATTERN = re.compile(r"^sk_live_[0-9A-Za-z]{43}$")


def test_generate_api_key_format_prefix_and_hash(settings: Settings) -> None:
    generated = generate_api_key(settings.api_key_pepper)
    assert KEY_PATTERN.fullmatch(generated.raw_key)
    random_part = generated.raw_key.removeprefix("sk_live_")
    assert len(random_part) == 43
    assert generated.prefix == random_part[:8]
    assert len(generated.prefix) == 8
    expected = hashlib.sha256(f"{generated.raw_key}{settings.api_key_pepper}".encode()).hexdigest()
    assert generated.key_hash == expected
    assert generated.key_hash == hash_api_key(generated.raw_key, settings.api_key_pepper)
    assert display_prefix(generated.prefix) == "sk_live_" + generated.prefix


def test_generated_keys_are_unique(settings: Settings) -> None:
    first = generate_api_key(settings.api_key_pepper)
    second = generate_api_key(settings.api_key_pepper)
    assert first.raw_key != second.raw_key
    assert first.key_hash != second.key_hash


async def test_create_key_stores_hash_and_audits_without_secrets(settings: Settings) -> None:
    repo = FakeAPIKeyRepository()
    user = make_user()
    repo.register_user(user)
    service = APIKeyService(repo, settings, None)
    key, raw_key = await service.create_key(user.id, "Prod", ["chat:completions"], ip="203.0.113.9")
    assert KEY_PATTERN.fullmatch(raw_key)
    assert key.key_hash == hash_api_key(raw_key, settings.api_key_pepper)
    assert key.prefix == raw_key.removeprefix("sk_live_")[:8]
    assert key.revoked_at is None
    audit = repo.audits[0]
    assert audit.action == "api_key.create"
    assert audit.actor_id == user.id
    assert audit.target_type == "api_key"
    assert audit.target_id == str(key.id)
    assert audit.ip == "203.0.113.9"
    assert audit.metadata_json == {"name": "Prod", "scopes": ["chat:completions"]}
    assert raw_key not in json.dumps(audit.metadata_json)
    assert key.key_hash not in json.dumps(audit.metadata_json)


async def test_list_keys_returns_only_own_keys(settings: Settings) -> None:
    repo = FakeAPIKeyRepository()
    owner = make_user()
    other = make_user(email="other@example.com")
    repo.register_user(owner)
    repo.register_user(other)
    service = APIKeyService(repo, settings, None)
    key, _ = await service.create_key(owner.id, "Mine", ["models:read"])
    await service.create_key(other.id, "Theirs", ["models:read"])
    listed = await service.list_keys(owner.id)
    assert [k.id for k in listed] == [key.id]


async def test_revoke_requires_ownership(settings: Settings) -> None:
    repo = FakeAPIKeyRepository()
    owner = make_user()
    other = make_user(email="other@example.com")
    repo.register_user(owner)
    repo.register_user(other)
    service = APIKeyService(repo, settings, None)
    key, _ = await service.create_key(owner.id, "Mine", ["models:read"])
    with pytest.raises(NotFoundError) as exc:
        await service.revoke_key(other.id, key.id)
    assert exc.value.status_code == 404
    assert exc.value.code == "not_found"
    assert key.revoked_at is None


async def test_revoke_missing_key_404(settings: Settings) -> None:
    service = APIKeyService(FakeAPIKeyRepository(), settings, None)
    with pytest.raises(NotFoundError):
        await service.revoke_key(uuid.uuid4(), uuid.uuid4())


async def test_revoke_sets_timestamp_audits_and_purges_cache(settings: Settings) -> None:
    repo = FakeAPIKeyRepository()
    user = make_user()
    repo.register_user(user)
    redis = FakeRedis()
    service = APIKeyService(repo, settings, redis)
    key, raw = await service.create_key(user.id, "K", ["models:read"])
    await service.authenticate(raw)
    cache_key = cache_key_for_hash(key.key_hash)
    assert cache_key in redis.store
    await service.revoke_key(user.id, key.id, ip="198.51.100.7")
    assert key.revoked_at is not None
    assert cache_key not in redis.store
    assert redis.deletes == [cache_key]
    revoke_audits = [a for a in repo.audits if a.action == "api_key.revoke"]
    assert len(revoke_audits) == 1
    assert revoke_audits[0].ip == "198.51.100.7"
    assert revoke_audits[0].metadata_json == {"name": "K", "scopes": ["models:read"]}


async def test_revoke_is_idempotent(settings: Settings) -> None:
    repo = FakeAPIKeyRepository()
    user = make_user()
    repo.register_user(user)
    service = APIKeyService(repo, settings, FakeRedis())
    key, _ = await service.create_key(user.id, "K", ["models:read"])
    await service.revoke_key(user.id, key.id)
    first_ts = key.revoked_at
    await service.revoke_key(user.id, key.id)
    assert key.revoked_at == first_ts
    assert len([a for a in repo.audits if a.action == "api_key.revoke"]) == 1


async def test_authenticate_cache_miss_then_hit(settings: Settings) -> None:
    repo = FakeAPIKeyRepository()
    user = make_user()
    repo.register_user(user)
    redis = FakeRedis()
    service = APIKeyService(repo, settings, redis)
    key, raw = await service.create_key(user.id, "K", ["models:read"])
    principal = await service.authenticate(raw)
    assert repo.get_by_hash_calls == 1
    assert principal == APIKeyPrincipal(str(user.id), str(key.id), ("models:read",))
    cache_key = cache_key_for_hash(key.key_hash)
    assert cache_key in redis.store
    assert redis.ttls[cache_key] == API_KEY_CACHE_TTL_SECONDS == 60
    cached = json.loads(redis.store[cache_key])
    assert cached == {
        "user_id": str(user.id),
        "api_key_id": str(key.id),
        "scopes": ["models:read"],
    }
    again = await service.authenticate(raw)
    assert again == principal
    assert repo.get_by_hash_calls == 1


async def test_authenticate_sets_last_used_at_on_db_miss(settings: Settings) -> None:
    repo = FakeAPIKeyRepository()
    user = make_user()
    repo.register_user(user)
    service = APIKeyService(repo, settings, None)
    key, raw = await service.create_key(user.id, "K", ["models:read"])
    assert key.last_used_at is None
    await service.authenticate(raw)
    assert key.last_used_at is not None
    assert repo.flushes == 2  # create_key + authenticate (DB miss)


async def test_authenticate_skips_last_used_at_write_on_cache_hit(settings: Settings) -> None:
    repo = FakeAPIKeyRepository()
    user = make_user()
    repo.register_user(user)
    redis = FakeRedis()
    service = APIKeyService(repo, settings, redis)
    key, raw = await service.create_key(user.id, "K", ["models:read"])
    await service.authenticate(raw)
    first_used = key.last_used_at
    assert first_used is not None
    flushes_after_miss = repo.flushes
    # Second call is served from the positive cache: no DB lookup or write.
    await service.authenticate(raw)
    assert repo.get_by_hash_calls == 1
    assert repo.flushes == flushes_after_miss
    assert key.last_used_at == first_used


async def test_authenticate_rejects_revoked_key(settings: Settings) -> None:
    repo = FakeAPIKeyRepository()
    user = make_user()
    repo.register_user(user)
    service = APIKeyService(repo, settings, FakeRedis())
    key, raw = await service.create_key(user.id, "K", ["models:read"])
    await service.revoke_key(user.id, key.id)
    with pytest.raises(AuthenticationError) as exc:
        await service.authenticate(raw)
    assert exc.value.code == "invalid_api_key"


async def test_authenticate_rejects_suspended_user(settings: Settings) -> None:
    # A suspended user's key must be indistinguishable from an invalid key.
    repo = FakeAPIKeyRepository()
    user = make_user(status="suspended")
    repo.register_user(user)
    service = APIKeyService(repo, settings, None)
    _, raw = await service.create_key(user.id, "K", ["models:read"])
    with pytest.raises(AuthenticationError) as suspended:
        await service.authenticate(raw)
    with pytest.raises(AuthenticationError) as unknown:
        await service.authenticate(generate_api_key(settings.api_key_pepper).raw_key)
    assert suspended.value.status_code == unknown.value.status_code == 401
    assert suspended.value.code == unknown.value.code == "invalid_api_key"
    assert suspended.value.message == unknown.value.message


async def test_authenticate_rejects_unknown_and_malformed_keys(settings: Settings) -> None:
    repo = FakeAPIKeyRepository()
    service = APIKeyService(repo, settings, None)
    for bad in ["", "sk_live_short", "sk_test_" + "A" * 43, "sk_live_" + "!" * 43]:
        with pytest.raises(AuthenticationError):
            await service.authenticate(bad)
    assert repo.get_by_hash_calls == 0
    valid_looking = generate_api_key(settings.api_key_pepper).raw_key
    with pytest.raises(AuthenticationError):
        await service.authenticate(valid_looking)
    assert repo.get_by_hash_calls == 1


async def test_redis_failure_falls_back_to_database(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()
    repo = FakeAPIKeyRepository()
    user = make_user()
    repo.register_user(user)
    redis = FakeRedis(fail=True)
    service = APIKeyService(repo, settings, redis)
    key, raw = await service.create_key(user.id, "K", ["models:read"])
    principal = await service.authenticate(raw)
    assert principal.api_key_id == str(key.id)
    assert repo.get_by_hash_calls == 1
    await service.authenticate(raw)
    assert repo.get_by_hash_calls == 2
    captured = capsys.readouterr().out
    assert raw not in captured
    assert key.key_hash not in captured


async def test_revoke_invalidates_cached_principal(settings: Settings) -> None:
    repo = FakeAPIKeyRepository()
    user = make_user()
    repo.register_user(user)
    redis = FakeRedis()
    service = APIKeyService(repo, settings, redis)
    key, raw = await service.create_key(user.id, "K", ["models:read"])
    await service.authenticate(raw)
    await service.authenticate(raw)
    assert repo.get_by_hash_calls == 1
    await service.revoke_key(user.id, key.id)
    with pytest.raises(AuthenticationError):
        await service.authenticate(raw)
    assert repo.get_by_hash_calls == 2


async def test_revoke_still_succeeds_when_cache_delete_fails(settings: Settings) -> None:
    repo = FakeAPIKeyRepository()
    user = make_user()
    repo.register_user(user)
    service = APIKeyService(repo, settings, FakeRedis(fail=True))
    key, _ = await service.create_key(user.id, "K", ["models:read"])
    await service.revoke_key(user.id, key.id)
    assert key.revoked_at is not None


async def test_corrupt_cache_entry_falls_back_to_db(settings: Settings) -> None:
    repo = FakeAPIKeyRepository()
    user = make_user()
    repo.register_user(user)
    redis = FakeRedis()
    service = APIKeyService(repo, settings, redis)
    key, raw = await service.create_key(user.id, "K", ["models:read"])
    redis.store[cache_key_for_hash(key.key_hash)] = b"not-json"
    principal = await service.authenticate(raw)
    assert principal.api_key_id == str(key.id)
    assert repo.get_by_hash_calls == 1


def test_principal_json_round_trip() -> None:
    principal = APIKeyPrincipal(str(uuid.uuid4()), str(uuid.uuid4()), ("models:read",))
    assert APIKeyPrincipal.from_json(principal.to_json()) == principal
    assert APIKeyPrincipal.from_json("garbage") is None
    assert APIKeyPrincipal.from_json('{"user_id": "x"}') is None
