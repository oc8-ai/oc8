from __future__ import annotations

import base64
import hashlib
import uuid

import pytest

from oc8.oauth.errors import OAuthStateInvalid
from oc8.oauth.state import STATE_TTL_SECONDS, consume_state, put_state

pytestmark = pytest.mark.asyncio


async def test_put_then_consume_roundtrips_the_payload(redis_url: str) -> None:
    # redis_url must be requested (not just implicitly available) -- it is what
    # points get_settings().redis_url at the testcontainer before state.py's
    # _client() reads it; see `_reset_realtime_bus`'s docstring in conftest.py
    # for the same footgun. Without it these tests would silently fall back to
    # the shared docker-compose Redis on 6381, which other sessions in this
    # worktree may also be exercising.
    tenant = uuid.uuid4()
    member = uuid.uuid4()
    state, challenge = await put_state(
        tenant_id=tenant,
        user_id="u1",
        member_id=member,
        owner="me",
        provider="google",
        scopes=("email",),
    )
    assert state and challenge
    got = await consume_state(state)
    assert got.tenant_id == tenant
    assert got.user_id == "u1"
    assert (got.member_id, got.owner) == (member, "me")
    assert got.provider == "google"
    assert got.scopes == ("email",)


async def test_challenge_is_the_s256_hash_of_the_verifier(redis_url: str) -> None:
    tenant = uuid.uuid4()
    state, challenge = await put_state(
        tenant_id=tenant, user_id=None, member_id=uuid.uuid4(), owner="me", provider="google", scopes=()
    )
    got = await consume_state(state)
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(got.verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert challenge == expected


async def test_state_is_single_use(redis_url: str) -> None:
    # The callback carries no bearer token, so the state entry IS the
    # authorization. Replaying it must fail.
    state, _ = await put_state(
        tenant_id=uuid.uuid4(), user_id=None, member_id=uuid.uuid4(), owner="me", provider="google", scopes=()
    )
    await consume_state(state)
    with pytest.raises(OAuthStateInvalid):
        await consume_state(state)


async def test_unknown_state_is_rejected(redis_url: str) -> None:
    with pytest.raises(OAuthStateInvalid):
        await consume_state("not-a-real-state")


async def test_states_are_unguessable_and_distinct(redis_url: str) -> None:
    tenant = uuid.uuid4()
    seen = set()
    for _ in range(5):
        state, _ = await put_state(
            tenant_id=tenant, user_id=None, member_id=uuid.uuid4(), owner="me", provider="google", scopes=()
        )
        assert len(state) >= 32
        seen.add(state)
    assert len(seen) == 5


def test_ttl_is_ten_minutes() -> None:
    assert STATE_TTL_SECONDS == 600
