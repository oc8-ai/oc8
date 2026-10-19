"""Poll-based ingress (oc8.channels.poll): a channel with no public webhook
URL to receive on still gets its updates processed, via the same
`process_inbound` the webhook route calls -- see the module docstring on
`oc8.channels.poll` for why every DB access there opens its own
`tenant_session` rather than sharing one across a tick."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.channels import ChannelCapabilities, ChannelLink
from oc8.channels.base import ApprovalChannel
from oc8.channels.binding import issue_code
from oc8.channels.poll import _poll_channel, _poll_tenant, poll_tick
from oc8.tenants.activity import set_tenant_activity_gate
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

CHANNEL = "fake-poll"


@dataclass
class FakePollChannel:
    """A minimal ApprovalChannel that also implements the optional `poll`
    method -- `getattr(impl, "poll", None)` in `oc8.channels.poll` is how a
    channel opts in, so this fake only needs to look like one duck-typed."""

    channel_id: str = CHANNEL
    to_return: list[tuple[list[dict[str, Any]], int]] = field(default_factory=list)
    calls: list[int] = field(default_factory=list)

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(max_classification="public")

    async def verify_inbound(self, *, headers: dict[str, str], body: bytes) -> bool:
        return True

    def parse_inbound(self, update: dict[str, Any]) -> Any:
        code = update.get("code")
        external_id = update.get("external_id")
        if code is None or external_id is None:
            return None
        return ChannelLink(code=str(code), external_id=str(external_id))

    async def deliver(self, *a: Any, **k: Any) -> str | None:
        return None

    async def withdraw(self, *a: Any, **k: Any) -> None:
        return None

    async def poll(self, *, offset: int) -> tuple[list[dict[str, Any]], int]:
        self.calls.append(offset)
        if not self.to_return:
            return [], offset
        return self.to_return.pop(0)


async def test_a_first_poll_with_no_cursor_starts_at_offset_zero(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    channel = FakePollChannel(to_return=[([], 0)])
    processed = await _poll_channel(tenant, CHANNEL, cast(ApprovalChannel, channel), channel.poll)
    assert processed == 0
    assert channel.calls == [0], "no cursor row exists yet -- must start from 0"


async def test_a_returned_update_is_processed_and_the_cursor_advances(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        row = await issue_code(db, tenant_id=tenant, user_id=uuid.uuid4(), channel=CHANNEL)
        code = row.code
        await db.commit()
    assert code is not None

    channel = FakePollChannel(
        to_return=[([{"code": code, "external_id": "chat-1"}], 5)]
    )
    processed = await _poll_channel(tenant, CHANNEL, cast(ApprovalChannel, channel), channel.poll)
    assert processed == 1

    async with app_session(tenant) as db:
        binding = (
            await db.execute(
                select(m.ApprovalChannelBinding).where(
                    m.ApprovalChannelBinding.tenant_id == tenant,
                    m.ApprovalChannelBinding.channel == CHANNEL,
                )
            )
        ).scalar_one()
        assert binding.external_id == "chat-1", "the update reached process_inbound"

        cursor = (
            await db.execute(
                select(m.ChannelPollCursor).where(
                    m.ChannelPollCursor.tenant_id == tenant,
                    m.ChannelPollCursor.channel == CHANNEL,
                )
            )
        ).scalar_one()
        assert cursor.last_update_id == 5


async def test_a_second_poll_resumes_from_the_persisted_cursor(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(m.ChannelPollCursor(tenant_id=tenant, channel=CHANNEL, last_update_id=42))
        await db.commit()

    channel = FakePollChannel(to_return=[([], 42)])
    await _poll_channel(tenant, CHANNEL, cast(ApprovalChannel, channel), channel.poll)
    assert channel.calls == [42], "must resume from the persisted cursor, not restart at 0"


async def test_no_new_updates_leaves_the_cursor_untouched(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(m.ChannelPollCursor(tenant_id=tenant, channel=CHANNEL, last_update_id=7))
        await db.commit()

    channel = FakePollChannel(to_return=[([], 7)])  # next_offset == offset: nothing new
    with patch(
        "oc8.channels.poll._advance_cursor", new_callable=AsyncMock
    ) as advance:
        await _poll_channel(tenant, CHANNEL, cast(ApprovalChannel, channel), channel.poll)
    advance.assert_not_called()


async def test_a_channel_with_no_poll_method_is_skipped(app_session: AppSessionFactory) -> None:
    """`getattr(impl, "poll", None)` is the opt-in -- a webhook-only channel
    (no `poll` attribute at all) must not be touched."""
    tenant = uuid.uuid4()

    class WebhookOnly:
        channel_id = "webhook-only"

        def capabilities(self) -> ChannelCapabilities:
            return ChannelCapabilities(max_classification="public")

        async def verify_inbound(self, *, headers: dict[str, str], body: bytes) -> bool:
            return True

        def parse_inbound(self, update: dict[str, Any]) -> Any:
            return None

        async def deliver(self, *a: Any, **k: Any) -> str | None:
            return None

        async def withdraw(self, *a: Any, **k: Any) -> None:
            return None

    with patch(
        "oc8.channels.poll.channels_for_tenant",
        new=AsyncMock(return_value={"webhook-only": WebhookOnly()}),
    ):
        processed = await _poll_tenant(tenant)
    assert processed == 0


async def test_one_channel_erroring_does_not_stop_a_sibling_channel(
    app_session: AppSessionFactory,
) -> None:
    """Mirrors `run_scheduler_tick`'s own per-tenant/per-trigger isolation:
    one broken bot must not silence another tenant's or another channel's
    approvals."""
    tenant = uuid.uuid4()

    class BrokenPoll:
        channel_id = "broken"

        def capabilities(self) -> ChannelCapabilities:
            return ChannelCapabilities(max_classification="public")

        async def verify_inbound(self, *, headers: dict[str, str], body: bytes) -> bool:
            return True

        def parse_inbound(self, update: dict[str, Any]) -> Any:
            return None

        async def deliver(self, *a: Any, **k: Any) -> str | None:
            return None

        async def withdraw(self, *a: Any, **k: Any) -> None:
            return None

        async def poll(self, *, offset: int) -> tuple[list[dict[str, Any]], int]:
            raise RuntimeError("telegram is down")

    healthy = FakePollChannel(to_return=[([], 0)])

    with patch(
        "oc8.channels.poll.channels_for_tenant",
        new=AsyncMock(return_value={"broken": BrokenPoll(), CHANNEL: healthy}),
    ):
        processed = await _poll_tenant(tenant)
    assert processed == 0
    assert healthy.calls == [0], "the healthy channel must still be polled"


async def test_poll_tick_never_raises_even_if_a_tenant_fails(
    app_session: AppSessionFactory,
) -> None:
    with patch(
        "oc8.channels.poll.list_runnable_tenant_ids",
        new=AsyncMock(return_value=[uuid.uuid4()]),
    ):
        with patch(
            "oc8.channels.poll._poll_tenant", new=AsyncMock(side_effect=RuntimeError("db down"))
        ):
            processed = await poll_tick()  # must not raise
    assert processed == 0


async def test_poll_tick_skips_an_inactive_organisation(
    app_session: AppSessionFactory,
) -> None:
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    for tenant in (tenant_a, tenant_b):
        async with app_session(tenant) as db:
            db.add(
                m.Organization(
                    id=tenant,
                    slug=f"test-{tenant.hex[:12]}",
                    name="Test Org",
                    tier="standard",
                    region="eu",
                )
            )
    channel_a = FakePollChannel(to_return=[([], 0)])
    channel_b = FakePollChannel(to_return=[([], 0)])
    channels: dict[uuid.UUID, dict[str, Any]] = {
        tenant_a: {CHANNEL: channel_a},
        tenant_b: {CHANNEL: channel_b},
    }

    async def fake_channels_for_tenant(db: Any, *, tenant_id: uuid.UUID) -> dict[str, Any]:
        return channels.get(tenant_id, {})

    async def gate(tenant_id: uuid.UUID) -> bool:
        return tenant_id != tenant_a

    set_tenant_activity_gate(gate)
    with patch("oc8.channels.poll.channels_for_tenant", new=fake_channels_for_tenant):
        await poll_tick()

    assert channel_a.calls == [], "an organisation that may not run is not polled"
    assert channel_b.calls == [0]
