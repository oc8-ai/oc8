"""Task 8: an ordinary Telegram message, all the way through to the tenant
Assistant and back.

`process_inbound` is the one entrypoint both the webhook and the poll loop
call (`oc8.channels.dispatch`), so driving it directly here with a real
update dict exercises the same path production traffic does: parse -> resolve
the binding -> check `copilot:manage` -> get-or-create the Assistant's
`ChatSession` -> enqueue a `source="chat"` run -> send the immediate ack.

`FakeChannel` is deliberately NOT `unittest.mock.AsyncMock()` end to end:
`AsyncMock()`'s child attributes are themselves `AsyncMock`, so
`impl.parse_inbound(update)` -- called synchronously by `process_inbound`,
per `ApprovalChannel.parse_inbound`'s sync signature -- would hand back an
unawaited coroutine instead of a parsed value, and every `isinstance` check
in `process_inbound` would silently miss. Same shape as `FakePollChannel` in
`test_poll.py`: a plain class with a real sync `parse_inbound`, and `say`
alone mocked so the assertions below can inspect it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.channels.binding import issue_code, redeem_code
from oc8.channels.dispatch import process_inbound
from oc8.channels.notice import ChannelCapabilities, ChannelFreeText
from oc8.tenants.activity import set_tenant_activity_gate
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

CHANNEL = "telegram"


class FakeChannel:
    """A minimal `ApprovalChannel`: real sync parsing (mirroring
    `TelegramChannel.parse_inbound`'s free-text branch), mocked `say`.

    `max_classification` defaults to `"internal"`, not the real
    `ChannelCapabilities` default of `"public"`: this fixture exists to test
    routing and authorization, not the classification gate (that has its own
    dedicated tests below), so it is built already cleared for the `internal`
    replies `bind_from_free_text` sends -- the same way a real operator would
    configure a channel they actually intend to use for this."""

    channel_id = CHANNEL

    def __init__(self, *, max_classification: str = "internal") -> None:
        self.say: AsyncMock = AsyncMock()
        self._max_classification = max_classification

    def parse_inbound(self, update: dict[str, Any]) -> Any:
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        text = str(message.get("text") or "").strip()
        sender = message.get("from")
        sender_id = sender.get("id") if isinstance(sender, dict) else None
        if not text or sender_id is None:
            return None
        return ChannelFreeText(text=text, external_id=str(sender_id))

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(max_classification=self._max_classification)


async def _grant_copilot_manage(db: Any, *, tenant_id: uuid.UUID, member: m.OrgMember) -> None:
    """Give `member` `copilot:manage` the only way the code table actually
    grants it: a builtin `org_admin` role (see `permissions.py`'s
    `BUILTIN_ROLE_PERMISSIONS` -- `copilot:manage` is `NEVER_DELEGATABLE`, so a
    hand-written `RolePermission` row on a non-builtin role would be
    intersected away by `_role_and_permissions`). Same local
    `Role(builtin=True, ...)` pattern `tests/authz/test_authority.py`'s `_role`
    helper uses -- there is no single shared importable fixture for this in
    `tests/conftest.py`.
    """
    from oc8.authz.permissions import ORG_ADMIN

    role = m.Role(tenant_id=tenant_id, name=ORG_ADMIN, builtin=True, kind="human")
    db.add(role)
    await db.flush()
    member.role_id = role.id
    await db.flush()


async def _grant_operator_role(db: Any, *, tenant_id: uuid.UUID, member: m.OrgMember) -> None:
    """A REAL role with REAL permissions (`run:start`, `run:control`,
    `approval:decide`, a bunch of `:view`s -- see `_OPERATOR` in
    `permissions.py`) that deliberately does NOT include `copilot:manage`.

    The point: a member with `role_id is None` never reaches
    `_role_and_permissions` at all (`_member_has_permission` returns False at
    the first line). That alone would pass even a broken permission check
    that returned True for every member holding SOME role. Granting a role
    that grants real permissions -- just not this one -- is what actually
    exercises `_role_and_permissions`'s negative branch.
    """
    from oc8.authz.permissions import OPERATOR

    role = m.Role(tenant_id=tenant_id, name=OPERATOR, builtin=True, kind="human")
    db.add(role)
    await db.flush()
    member.role_id = role.id
    await db.flush()


async def test_a_total_stranger_is_answered_with_nothing_at_all(
    app_session: AppSessionFactory,
) -> None:
    """An account with NO binding row whatsoever is not refused, it is
    ignored -- exactly as it was before this door existed, when unrecognised
    text parsed to None and `process_inbound` dropped it in silence.

    Replying to any account that finds the bot's address makes the bot an
    unauthenticated outbound-message amplifier: anyone can make it send
    messages, forever, at whatever rate they like. This asserts ZERO outbound
    calls, not a particular sentence.
    """
    tenant = uuid.uuid4()
    impl = FakeChannel()
    async with app_session(tenant) as db:
        await process_inbound(
            db,
            impl,
            tenant_id=tenant,
            channel=CHANNEL,
            update={"message": {"text": "Statistik bitte", "from": {"id": 999}}},
        )
    impl.say.assert_not_awaited()


async def test_a_revoked_binding_is_still_answered_with_the_refusal(
    app_session: AppSessionFactory,
) -> None:
    """The line is "has this account ever been through the link flow", not "is
    its binding live". A sender whose binding was revoked knows perfectly well
    that it was bound, so the refusal tells them nothing -- and it must keep
    reading identically to the one an authorized-but-not-permitted sender gets,
    or the bot starts telling those two apart.
    """
    tenant = uuid.uuid4()
    impl = FakeChannel()
    async with app_session(tenant) as db:
        member = m.OrgMember(
            tenant_id=tenant,
            subject="revoked-sender",
            all_departments=True,
            subject_uuid=uuid.uuid4(),
        )
        db.add(member)
        await db.flush()
        row = await issue_code(
            db, tenant_id=tenant, user_id=uuid.uuid4(), channel=CHANNEL, member_id=member.id
        )
        code = row.code
        await db.commit()
    async with app_session(tenant) as db:
        await redeem_code(db, tenant_id=tenant, channel=CHANNEL, code=str(code), external_id="995")
        await db.commit()
    async with app_session(tenant) as db:
        live = (
            (
                await db.execute(
                    select(m.ApprovalChannelBinding).where(
                        m.ApprovalChannelBinding.tenant_id == tenant,
                        m.ApprovalChannelBinding.external_id == "995",
                    )
                )
            )
            .scalars()
            .one()
        )
        live.revoked_at = dt.datetime.now(dt.UTC)
        await db.commit()
    async with app_session(tenant) as db:
        await process_inbound(
            db,
            impl,
            tenant_id=tenant,
            channel=CHANNEL,
            update={"message": {"text": "Statistik bitte", "from": {"id": 995}}},
        )
    impl.say.assert_awaited_once()
    assert impl.say.await_args.args[1] == "this sender may not decide anything here"


async def test_a_linked_but_unauthorized_sender_gets_the_same_refusal(
    app_session: AppSessionFactory,
) -> None:
    """Linked (a live binding with a member behind it) but that member holds
    no `copilot:manage` -- must read exactly like "not linked at all". Two
    different sentences here would let a sender who found the bot's address
    learn which of those states they are in."""
    tenant = uuid.uuid4()
    impl = FakeChannel()
    async with app_session(tenant) as db:
        member = m.OrgMember(
            tenant_id=tenant,
            subject="unauthorized-sender",
            all_departments=True,
            subject_uuid=uuid.uuid4(),
        )
        db.add(member)
        await db.flush()
        row = await issue_code(
            db, tenant_id=tenant, user_id=uuid.uuid4(), channel=CHANNEL, member_id=member.id
        )
        code = row.code
        await db.commit()
    async with app_session(tenant) as db:
        await redeem_code(db, tenant_id=tenant, channel=CHANNEL, code=str(code), external_id="998")
        await db.commit()
    async with app_session(tenant) as db:
        # No role granted -- fails closed per `_member_has_permission`'s own
        # doctrine: no role_id means no permission, not a default one.
        await process_inbound(
            db,
            impl,
            tenant_id=tenant,
            channel=CHANNEL,
            update={"message": {"text": "Wie viele offene Tickets?", "from": {"id": 998}}},
        )
    impl.say.assert_awaited_once()
    assert impl.say.await_args.args[1] == "this sender may not decide anything here"


async def test_a_linked_sender_with_a_real_role_but_not_copilot_manage_is_still_refused(
    app_session: AppSessionFactory,
) -> None:
    """The previous test's member has `role_id is None`, which short-circuits
    `_member_has_permission` before `_role_and_permissions` is ever called --
    a permission check that (by bug) returned True for ANY non-null
    `role_id` would still pass it. This member holds a real builtin role
    (`operator`, with real grants -- `run:start`, `run:control`, a bunch of
    `:view`s) that simply does not include `copilot:manage`, so this is the
    one that actually exercises `_role_and_permissions`'s negative branch."""
    tenant = uuid.uuid4()
    impl = FakeChannel()
    async with app_session(tenant) as db:
        member = m.OrgMember(
            tenant_id=tenant,
            subject="operator-sender",
            all_departments=True,
            subject_uuid=uuid.uuid4(),
        )
        db.add(member)
        await db.flush()
        row = await issue_code(
            db, tenant_id=tenant, user_id=uuid.uuid4(), channel=CHANNEL, member_id=member.id
        )
        code = row.code
        member_id = member.id
        await db.commit()
    async with app_session(tenant) as db:
        await redeem_code(db, tenant_id=tenant, channel=CHANNEL, code=str(code), external_id="997")
        await db.commit()
    async with app_session(tenant) as db:
        member = await db.get(m.OrgMember, member_id)
        assert member is not None
        await _grant_operator_role(db, tenant_id=tenant, member=member)
        await process_inbound(
            db,
            impl,
            tenant_id=tenant,
            channel=CHANNEL,
            update={"message": {"text": "Wie viele offene Tickets?", "from": {"id": 997}}},
        )
    impl.say.assert_awaited_once()
    assert impl.say.await_args.args[1] == "this sender may not decide anything here"


async def test_a_linked_authorized_sender_gets_an_ack_and_a_session_is_created(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    # redis_url must be REQUESTED, not merely available: enqueue_run (via
    # chat.service.send_message) publishes onto the run queue, which lazily
    # binds to the testcontainer only when a test in the current session has
    # asked for it first (see tests/api/test_chat.py's identical note).
    tenant = uuid.uuid4()
    impl = FakeChannel()
    async with app_session(tenant) as db:
        member = m.OrgMember(
            tenant_id=tenant,
            subject="authorized-sender",
            all_departments=True,
            subject_uuid=uuid.uuid4(),
        )
        db.add(member)
        await db.flush()
        row = await issue_code(
            db, tenant_id=tenant, user_id=uuid.uuid4(), channel=CHANNEL, member_id=member.id
        )
        code = row.code
        member_id = member.id
        await db.commit()
    async with app_session(tenant) as db:
        await redeem_code(db, tenant_id=tenant, channel=CHANNEL, code=str(code), external_id="999")
        await db.commit()
    async with app_session(tenant) as db:
        member = await db.get(m.OrgMember, member_id)
        assert member is not None
        await _grant_copilot_manage(db, tenant_id=tenant, member=member)
        await process_inbound(
            db,
            impl,
            tenant_id=tenant,
            channel=CHANNEL,
            update={"message": {"text": "Wie viele offene Tickets?", "from": {"id": 999}}},
        )
    impl.say.assert_awaited_once()
    assert "dran" in impl.say.await_args.args[1].lower()

    async with app_session(tenant) as db:
        sessions = (
            (await db.execute(select(m.ChatSession).where(m.ChatSession.tenant_id == tenant)))
            .scalars()
            .all()
        )
        assert len(sessions) == 1, "exactly one session, created once"
        session = sessions[0]
        assert session.member_id == member_id

        runs = (
            (
                await db.execute(
                    select(m.AgentRun).where(
                        m.AgentRun.tenant_id == tenant, m.AgentRun.source == "chat"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(runs) == 1
        assert runs[0].context["chat_channel"] == "telegram"
        assert runs[0].context["chat_channel_external_id"] == "999"
        assert runs[0].context["chat_session_id"] == str(session.id)

        messages = (
            (
                await db.execute(
                    select(m.ChatMessage).where(m.ChatMessage.session_id == session.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(messages) == 1
        assert messages[0].role == "user"
        assert messages[0].content == "Wie viele offene Tickets?"


async def test_a_channel_at_the_real_default_classification_gets_no_ack_content(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """`FakeChannel()` above defaults to `max_classification="internal"` so
    the routing tests aren't also classification tests. This one builds the
    channel the way a fresh, never-configured one actually starts --
    `ChannelCapabilities`'s real default, `"public"` -- and proves the ack is
    withheld exactly the way an approval already would be at that ceiling."""
    from oc8.channels.dispatch import _CONTENT_WITHHELD

    tenant = uuid.uuid4()
    impl = FakeChannel(max_classification="public")
    async with app_session(tenant) as db:
        member = m.OrgMember(
            tenant_id=tenant,
            subject="authorized-sender",
            all_departments=True,
            subject_uuid=uuid.uuid4(),
        )
        db.add(member)
        await db.flush()
        row = await issue_code(
            db, tenant_id=tenant, user_id=uuid.uuid4(), channel=CHANNEL, member_id=member.id
        )
        code = row.code
        member_id = member.id
        await db.commit()
    async with app_session(tenant) as db:
        await redeem_code(db, tenant_id=tenant, channel=CHANNEL, code=str(code), external_id="998")
        await db.commit()
    async with app_session(tenant) as db:
        member = await db.get(m.OrgMember, member_id)
        assert member is not None
        await _grant_copilot_manage(db, tenant_id=tenant, member=member)
        await process_inbound(
            db,
            impl,
            tenant_id=tenant,
            channel=CHANNEL,
            update={"message": {"text": "Wie viele offene Tickets?", "from": {"id": 998}}},
        )
    impl.say.assert_awaited_once()
    sent = impl.say.await_args.args[1]
    assert sent == _CONTENT_WITHHELD
    assert "dran" not in sent.lower()


async def test_an_authorized_senders_secret_looking_message_is_refused_not_acked(
    app_session: AppSessionFactory,
) -> None:
    """`chat.service.send_message` refuses a message tripping the Assistant's
    secret-blindness gate (`is_secret_request` -- a bare substring match on
    things like "password"/"token") WITHOUT enqueueing a run: it returns
    `(user_message, None)`. `bind_from_free_text` must branch on that `None`
    and tell the sender the actual refusal -- not the generic "Bin dran..."
    ack, which (with no run ever created) `executor.py`'s terminal-state
    hook would never follow up on, leaving the sender hanging forever."""
    tenant = uuid.uuid4()
    impl = FakeChannel()
    async with app_session(tenant) as db:
        member = m.OrgMember(
            tenant_id=tenant,
            subject="secret-sender",
            all_departments=True,
            subject_uuid=uuid.uuid4(),
        )
        db.add(member)
        await db.flush()
        row = await issue_code(
            db, tenant_id=tenant, user_id=uuid.uuid4(), channel=CHANNEL, member_id=member.id
        )
        code = row.code
        member_id = member.id
        await db.commit()
    async with app_session(tenant) as db:
        await redeem_code(db, tenant_id=tenant, channel=CHANNEL, code=str(code), external_id="996")
        await db.commit()
    async with app_session(tenant) as db:
        member = await db.get(m.OrgMember, member_id)
        assert member is not None
        await _grant_copilot_manage(db, tenant_id=tenant, member=member)
        # No redis_url fixture needed: the secret gate in send_message
        # returns before enqueue_run/publish is ever reached.
        await process_inbound(
            db,
            impl,
            tenant_id=tenant,
            channel=CHANNEL,
            update={"message": {"text": "hier ist mein token: abc123", "from": {"id": 996}}},
        )
    impl.say.assert_awaited_once()
    sent = impl.say.await_args.args[1]
    assert sent != "Bin dran, melde mich gleich.", (
        "the secret refusal must not be masked by the ack"
    )
    assert "credential" in sent.lower()

    async with app_session(tenant) as db:
        runs = (
            (
                await db.execute(
                    select(m.AgentRun).where(
                        m.AgentRun.tenant_id == tenant, m.AgentRun.source == "chat"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert runs == [], "the secret gate must never enqueue a run"


async def test_a_second_message_from_the_same_sender_reuses_the_session(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    impl = FakeChannel()
    async with app_session(tenant) as db:
        member = m.OrgMember(
            tenant_id=tenant,
            subject="repeat-sender",
            all_departments=True,
            subject_uuid=uuid.uuid4(),
        )
        db.add(member)
        await db.flush()
        row = await issue_code(
            db, tenant_id=tenant, user_id=uuid.uuid4(), channel=CHANNEL, member_id=member.id
        )
        code = row.code
        member_id = member.id
        await db.commit()
    async with app_session(tenant) as db:
        await redeem_code(db, tenant_id=tenant, channel=CHANNEL, code=str(code), external_id="777")
        await db.commit()
    async with app_session(tenant) as db:
        member = await db.get(m.OrgMember, member_id)
        assert member is not None
        await _grant_copilot_manage(db, tenant_id=tenant, member=member)
        await db.commit()

    for text in ("Erste Frage", "Zweite Frage"):
        async with app_session(tenant) as db:
            await process_inbound(
                db,
                impl,
                tenant_id=tenant,
                channel=CHANNEL,
                update={"message": {"text": text, "from": {"id": 777}}},
            )

    assert impl.say.await_count == 2

    async with app_session(tenant) as db:
        sessions = (
            (await db.execute(select(m.ChatSession).where(m.ChatSession.tenant_id == tenant)))
            .scalars()
            .all()
        )
        assert len(sessions) == 1, "the second message must reuse the session, not open a new one"


async def test_channel_free_text_of_an_inactive_organisation_starts_no_run(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    impl = FakeChannel()
    async with app_session(tenant) as db:
        member = m.OrgMember(
            tenant_id=tenant,
            subject="authorized-sender",
            all_departments=True,
            subject_uuid=uuid.uuid4(),
        )
        db.add(member)
        await db.flush()
        row = await issue_code(
            db, tenant_id=tenant, user_id=uuid.uuid4(), channel=CHANNEL, member_id=member.id
        )
        code = row.code
        member_id = member.id
    async with app_session(tenant) as db:
        await redeem_code(db, tenant_id=tenant, channel=CHANNEL, code=str(code), external_id="999")
    async with app_session(tenant) as db:
        member = await db.get(m.OrgMember, member_id)
        assert member is not None
        await _grant_copilot_manage(db, tenant_id=tenant, member=member)

    async def gate(tenant_id: uuid.UUID) -> bool:
        return tenant_id != tenant

    set_tenant_activity_gate(gate)
    async with app_session(tenant) as db:
        await process_inbound(
            db,
            impl,
            tenant_id=tenant,
            channel=CHANNEL,
            update={"message": {"text": "Wie viele offene Tickets?", "from": {"id": 999}}},
        )
    impl.say.assert_not_awaited()

    async with app_session(tenant) as db:
        runs = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.tenant_id == tenant)))
            .scalars()
            .all()
        )
        assert runs == []
        messages = (
            (await db.execute(select(m.ChatMessage).where(m.ChatMessage.tenant_id == tenant)))
            .scalars()
            .all()
        )
        assert messages == [], "a turn nobody will answer is not left in the transcript"
