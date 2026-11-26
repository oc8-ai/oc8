"""Binding a messenger account to a user, which is where an approval channel is
either safe or worthless.

A chat id is not a user and a phone number proves nothing. If oc8 inferred the
link from either, knowing the bot's name and spoofing a number would be enough
to approve a refund — so every property below is about NOT inferring it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.channels.binding import (
    BindingError,
    issue_code,
    recipients,
    redeem_code,
    resolve,
    revoke,
)
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

CHANNEL = "telegram"


async def _code(db: Any, tenant: uuid.UUID, user: uuid.UUID) -> str:
    row = await issue_code(db, tenant_id=tenant, user_id=user, channel=CHANNEL)
    assert row.code is not None
    return row.code


async def test_a_redeemed_code_binds_that_account_and_nobody_else(
    app_session: AppSessionFactory,
) -> None:
    tenant, user = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        code = await _code(db, tenant, user)
        bound = await redeem_code(
            db, tenant_id=tenant, channel=CHANNEL, code=code, external_id="chat-1"
        )
        assert bound.user_id == user
        assert bound.code is None, "single use: the code is spent, not merely marked"

        found = await resolve(db, tenant_id=tenant, channel=CHANNEL, external_id="chat-1")
        assert found is not None and found.user_id == user
        assert await resolve(db, tenant_id=tenant, channel=CHANNEL, external_id="chat-2") is None


async def test_the_same_code_cannot_be_redeemed_twice(app_session: AppSessionFactory) -> None:
    """A code quoted into a chat stays in that chat's history. If it kept
    working, anybody who later reads the thread inherits the right to approve."""
    tenant, user = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        code = await _code(db, tenant, user)
        await redeem_code(db, tenant_id=tenant, channel=CHANNEL, code=code, external_id="chat-1")
        with pytest.raises(BindingError):
            await redeem_code(
                db, tenant_id=tenant, channel=CHANNEL, code=code, external_id="attacker"
            )


async def test_an_expired_code_is_refused(app_session: AppSessionFactory) -> None:
    tenant, user = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        row = await issue_code(db, tenant_id=tenant, user_id=user, channel=CHANNEL)
        row.code_expires_at = dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=1)
        await db.flush()
        assert row.code is not None
        with pytest.raises(BindingError):
            await redeem_code(
                db, tenant_id=tenant, channel=CHANNEL, code=row.code, external_id="chat-1"
            )


async def test_a_wrong_code_is_refused_and_says_nothing_more(
    app_session: AppSessionFactory,
) -> None:
    """One message for every failure. Distinguishing "expired" from "already
    used" from "never existed" turns the bot into an oracle for live codes."""
    tenant, user = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        await _code(db, tenant, user)
        with pytest.raises(BindingError) as wrong:
            await redeem_code(
                db, tenant_id=tenant, channel=CHANNEL, code="nonsense", external_id="chat-1"
            )
        with pytest.raises(BindingError) as absent:
            await redeem_code(
                db, tenant_id=tenant, channel=CHANNEL, code="also-wrong", external_id="chat-2"
            )
        assert str(wrong.value) == str(absent.value)


async def test_a_code_from_one_tenant_does_not_bind_in_another(
    app_session: AppSessionFactory,
) -> None:
    """Redemption is tenant-scoped because a tenant runs its own bot. A code
    that worked across that line would make one customer's approver another
    customer's."""
    tenant_a, tenant_b, user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant_a) as db:
        code = await _code(db, tenant_a, user)
    async with app_session(tenant_b) as db:
        with pytest.raises(BindingError):
            await redeem_code(
                db, tenant_id=tenant_b, channel=CHANNEL, code=code, external_id="chat-1"
            )


async def test_rebinding_retires_the_account_it_replaces(
    app_session: AppSessionFactory,
) -> None:
    """Somebody who changes phones should not leave the old one able to approve.
    Nobody remembers to revoke it, so re-binding does."""
    tenant, user = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        first = await _code(db, tenant, user)
        await redeem_code(db, tenant_id=tenant, channel=CHANNEL, code=first, external_id="old")
        second = await _code(db, tenant, user)
        await redeem_code(db, tenant_id=tenant, channel=CHANNEL, code=second, external_id="new")

        assert await resolve(db, tenant_id=tenant, channel=CHANNEL, external_id="old") is None
        assert await resolve(db, tenant_id=tenant, channel=CHANNEL, external_id="new") is not None


async def test_a_revoked_binding_can_no_longer_decide_and_is_still_on_record(
    app_session: AppSessionFactory,
) -> None:
    """Kept as a row rather than deleted: which account could approve, and until
    when, is exactly what an audit asks afterwards."""
    tenant, user = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        code = await _code(db, tenant, user)
        bound = await redeem_code(
            db, tenant_id=tenant, channel=CHANNEL, code=code, external_id="chat-1"
        )
        await revoke(db, tenant_id=tenant, binding_id=bound.id)

        assert await resolve(db, tenant_id=tenant, channel=CHANNEL, external_id="chat-1") is None
        # `department_id` is required since the fan-out became department-scoped;
        # None asks for the tenant-wide audience, which is the widest one there
        # is -- so an empty answer here is the strongest form of this assertion.
        assert (
            await recipients(
                db, tenant_id=tenant, channel=CHANNEL, department_id=None, action_type="tool_send"
            )
            == []
        )
        still_there = await db.get(m.ApprovalChannelBinding, bound.id)
        assert still_there is not None and still_there.revoked_at is not None


async def test_a_binding_that_names_nobody_is_skipped_out_loud(
    app_session: AppSessionFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Dropping every pre-slice binding is deliberate. It was also silent.

    Migration 0046 adds `member_id` and grandfathers nothing -- inventing an
    `org_member` per binding would collide with `uq_org_member_subject_uuid` the
    first time that human authenticated, 500-ing every request from exactly the
    population piloting the feature. So the inner join in `recipients` drops those
    rows, and on the live system that is the demo's Telegram binding: it stops
    receiving announcements AND withdrawals, `raise_approval` still succeeds, and
    the only trace was a comment in a migration file.

    One indexed count per announce buys a line naming the row and the fix.
    """
    import logging

    tenant, user = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(
            m.ApprovalChannelBinding(
                tenant_id=tenant,
                channel=CHANNEL,
                user_id=user,
                external_id="chat-from-before",
                member_id=None,
            )
        )
        await db.flush()

        with caplog.at_level(logging.WARNING, logger="oc8.channels.binding"):
            told = await recipients(
                db, tenant_id=tenant, channel=CHANNEL, department_id=None, action_type="tool_send"
            )

    assert told == [], "a binding nobody stands behind was told about an approval"
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("name no org_member" in w for w in warnings), warnings
    assert any("POST /channels/" in w for w in warnings), (
        "the line has to say how to put it right, or it is noise somebody mutes"
    )


async def test_a_healthy_fan_out_says_nothing(
    app_session: AppSessionFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """The other half: a warning that fires on every announce in a healthy tenant
    is a warning nobody reads by the second week."""
    import logging

    tenant, user = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        from oc8.authz.scope import subject_uuid_for

        member = m.OrgMember(
            tenant_id=tenant,
            subject="boss",
            subject_uuid=subject_uuid_for("boss"),
            display_name="Boss",
            all_departments=True,
        )
        db.add(member)
        await db.flush()
        db.add(
            m.ApprovalChannelBinding(
                tenant_id=tenant,
                channel=CHANNEL,
                user_id=user,
                external_id="chat-boss",
                member_id=member.id,
            )
        )
        await db.flush()

        with caplog.at_level(logging.WARNING, logger="oc8.channels.binding"):
            told = await recipients(
                db, tenant_id=tenant, channel=CHANNEL, department_id=None, action_type="tool_send"
            )

    assert [b.external_id for b in told] == ["chat-boss"]
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
