from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.copilot.followups import (
    cancel_followup,
    catch_up,
    fire_followup,
    max_active_followups,
    schedule_followup,
)
from oc8.copilot.profile import pause, resume
from oc8.copilot.responsibilities import close_responsibility, open_responsibility
from oc8.copilot.schedule import MAX_ACTIVE_FOLLOWUPS, FollowupRejected, FollowupSpec
from tests.conftest import AppSessionFactory
from tests.copilot.helpers import copilot_seat, once_in_an_hour

SPEC = once_in_an_hour()


async def _world(app_session: AppSessionFactory, *, with_role: bool = True):  # type: ignore[no-untyped-def]
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        seat = await copilot_seat(db, tenant, "lisa@example.com", with_role=with_role)
        r = await open_responsibility(
            db,
            tenant_id=tenant,
            member_id=seat.member_id,
            chat_session_id=seat.session_id,
            title="Offsite",
            goal="Keep the offsite on track",
            origin_channel=None,
            run_id=None,
            actor_agent_id=assistant.id,
            member_subject=seat.subject,
        )
        t = await schedule_followup(
            db,
            tenant_id=tenant,
            member_id=seat.member_id,
            responsibility_id=r.id,
            assistant_id=assistant.id,
            spec=SPEC,
            prompt="Check the venue replies",
            member_subject=seat.subject,
        )
        return tenant, seat.member_id, seat.session_id, r.id, t.id


async def test_fire_posts_followup_turn_and_enqueues_chat_run(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    # redis_url: firing publishes the run to the queue.
    tenant, _member_id, session_id, r_id, t_id = await _world(app_session)
    async with app_session(tenant) as db:
        trigger = await db.get(m.Trigger, t_id)
        assert trigger is not None
        assert await fire_followup(db, trigger, tenant_id=tenant) == "fired"
    async with app_session(tenant) as db:
        msgs = (
            (await db.execute(select(m.ChatMessage).where(m.ChatMessage.session_id == session_id)))
            .scalars()
            .all()
        )
        assert [x.role for x in msgs] == ["followup"]
        run = (await db.execute(select(m.AgentRun).where(m.AgentRun.source == "chat"))).scalar_one()
        assert run.context["door"] == "followup"
        assert run.context["followup"]["responsibility_id"] == str(r_id)
        assert run.context["originating_operator"] == "lisa@example.com"
        assert "operator_role" not in run.context
        t = await db.get(m.Trigger, t_id)
        assert t is not None and t.enabled is False  # once-kind is spent


async def test_followup_skips_when_session_busy(app_session: AppSessionFactory) -> None:
    tenant, _member_id, session_id, _, t_id = await _world(app_session)
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        db.add(
            m.AgentRun(
                tenant_id=tenant,
                agent_id=assistant.id,
                source="chat",
                state="running",
                context={"chat_session_id": str(session_id), "task": "typing"},
            )
        )
        await db.flush()
        trigger = await db.get(m.Trigger, t_id)
        assert trigger is not None
        assert await fire_followup(db, trigger, tenant_id=tenant) == "busy"


async def test_paused_profile_skips_and_resume_catches_up_once(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    # redis_url: firing publishes the run to the queue.
    tenant, member_id, _session_id, _, t_id = await _world(app_session)
    async with app_session(tenant) as db:
        await pause(db, tenant_id=tenant, member_id=member_id)
        trigger = await db.get(m.Trigger, t_id)
        assert trigger is not None
        assert await fire_followup(db, trigger, tenant_id=tenant) == "paused"
    async with app_session(tenant) as db:
        await resume(db, tenant_id=tenant, member_id=member_id)
        assert await catch_up(db, tenant_id=tenant, member_id=member_id) == 1
    async with app_session(tenant) as db:
        assert await catch_up(db, tenant_id=tenant, member_id=member_id) == 0


async def test_member_without_assigned_permission_fails_closed(
    app_session: AppSessionFactory,
) -> None:
    tenant, member_id, _, _, t_id = await _world(app_session)
    async with app_session(tenant) as db:
        # The role was taken away after the follow-up was scheduled.
        member = await db.get(m.OrgMember, member_id)
        assert member is not None
        member.role_id = None
    async with app_session(tenant) as db:
        trigger = await db.get(m.Trigger, t_id)
        assert trigger is not None
        assert await fire_followup(db, trigger, tenant_id=tenant) == "no_permission"
        assert (await db.execute(select(m.AgentRun))).scalars().all() == []


async def test_cap_of_twenty_active_followups(app_session: AppSessionFactory) -> None:
    tenant, member_id, _, r_id, _ = await _world(app_session)
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        for _ in range(19):
            await schedule_followup(
                db,
                tenant_id=tenant,
                member_id=member_id,
                responsibility_id=r_id,
                assistant_id=assistant.id,
                spec=SPEC,
                prompt="x",
                member_subject="lisa@example.com",
            )
        with pytest.raises(FollowupRejected, match="20"):
            await schedule_followup(
                db,
                tenant_id=tenant,
                member_id=member_id,
                responsibility_id=r_id,
                assistant_id=assistant.id,
                spec=SPEC,
                prompt="x",
                member_subject="lisa@example.com",
            )


async def test_followup_channel_delivery_uses_live_binding(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    # redis_url: firing publishes the run to the queue.
    tenant, member_id, _session_id, r_id, t_id = await _world(app_session)
    async with app_session(tenant) as db:
        r = await db.get(m.Responsibility, r_id)
        assert r is not None
        r.origin_channel = "telegram"
        db.add(
            m.ApprovalChannelBinding(
                tenant_id=tenant,
                channel="telegram",
                user_id=uuid.uuid4(),
                member_id=member_id,
                external_id="4711",
            )
        )
        await db.flush()
        trigger = await db.get(m.Trigger, t_id)
        assert trigger is not None
        await fire_followup(db, trigger, tenant_id=tenant)
    async with app_session(tenant) as db:
        run = (await db.execute(select(m.AgentRun))).scalar_one()
        assert run.context["chat_channel"] == "telegram"
        assert run.context["chat_channel_external_id"] == "4711"
        assert run.context["door"] == "followup"


async def test_followup_channel_falls_back_to_web_when_binding_revoked(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    # redis_url: firing publishes the run to the queue.
    tenant, member_id, _, r_id, t_id = await _world(app_session)
    async with app_session(tenant) as db:
        r = await db.get(m.Responsibility, r_id)
        assert r is not None
        r.origin_channel = "telegram"
        db.add(
            m.ApprovalChannelBinding(
                tenant_id=tenant,
                channel="telegram",
                user_id=uuid.uuid4(),
                member_id=member_id,
                external_id="4711",
                revoked_at=dt.datetime.now(tz=dt.UTC),
            )
        )
        await db.flush()
        trigger = await db.get(m.Trigger, t_id)
        assert trigger is not None
        await fire_followup(db, trigger, tenant_id=tenant)
    async with app_session(tenant) as db:
        run = (await db.execute(select(m.AgentRun))).scalar_one()
        assert "chat_channel" not in run.context


async def test_waiting_and_back(app_session: AppSessionFactory) -> None:
    from oc8.runtime.clarification import request_clarification, resolve_clarification

    tenant, _, session_id, r_id, _ = await _world(app_session)
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=assistant.id,
            source="chat",
            state="running",
            context={
                "chat_session_id": str(session_id),
                "followup": {"responsibility_id": str(r_id)},
            },
        )
        db.add(run)
        await db.flush()
        await request_clarification(db, run=run, question="Which venue?")
        from oc8.copilot.followups import load_responsibility_for_run

        resp = await load_responsibility_for_run(db, run=run)
        assert resp is not None
        resp.state = "waiting"  # what the executor does on park
        await db.flush()
        await resolve_clarification(db, run=run, answer="The lake one")
        await db.refresh(resp)
        assert resp.state == "active"


# ---- review fix round 1 ----

CRON_SPEC = FollowupSpec(
    "cron", "Europe/Berlin", None, "0 9 * * *", dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=30)
)


async def _recurring_world(app_session: AppSessionFactory):  # type: ignore[no-untyped-def]
    tenant, member_id, _session_id, r_id, _ = await _world(app_session)
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        t = await schedule_followup(
            db,
            tenant_id=tenant,
            member_id=member_id,
            responsibility_id=r_id,
            assistant_id=assistant.id,
            spec=CRON_SPEC,
            prompt="daily",
            member_subject="lisa@example.com",
        )
        await pause(db, tenant_id=tenant, member_id=member_id)
        trig = await db.get(m.Trigger, t.id)
        assert trig is not None
        assert await fire_followup(db, trig, tenant_id=tenant) == "paused"
        return tenant, member_id, r_id, t.id


async def _assert_stays_ended(app_session, tenant, member_id, t_id) -> None:  # type: ignore[no-untyped-def]
    async with app_session(tenant) as db:
        await resume(db, tenant_id=tenant, member_id=member_id)
        assert await catch_up(db, tenant_id=tenant, member_id=member_id) == 0
        assert (await db.execute(select(m.AgentRun))).scalars().all() == []
        t = await db.get(m.Trigger, t_id)
        assert t is not None and t.enabled is False


async def test_catch_up_does_not_resurrect_a_cancelled_followup(
    app_session: AppSessionFactory,
) -> None:
    tenant, member_id, _, t_id = await _recurring_world(app_session)
    async with app_session(tenant) as db:
        assert await cancel_followup(db, tenant_id=tenant, member_id=member_id, trigger_id=t_id)
    await _assert_stays_ended(app_session, tenant, member_id, t_id)


async def test_catch_up_does_not_resurrect_after_close(app_session: AppSessionFactory) -> None:
    tenant, member_id, r_id, t_id = await _recurring_world(app_session)
    async with app_session(tenant) as db:
        await close_responsibility(
            db,
            tenant_id=tenant,
            member_id=member_id,
            responsibility_id=r_id,
            state="cancelled",
            reason="no longer needed",
            actor_agent_id=None,
            member_subject="lisa@example.com",
        )
    await _assert_stays_ended(app_session, tenant, member_id, t_id)


async def test_once_followup_skipped_busy_stays_scheduled_and_fires_later(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant, _, session_id, _, t_id = await _world(app_session)
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        busy = m.AgentRun(
            tenant_id=tenant,
            agent_id=assistant.id,
            source="chat",
            state="running",
            context={"chat_session_id": str(session_id), "task": "typing"},
        )
        db.add(busy)
        await db.flush()
        trigger = await db.get(m.Trigger, t_id)
        assert trigger is not None
        assert await fire_followup(db, trigger, tenant_id=tenant) == "busy"
        busy_id = busy.id
    async with app_session(tenant) as db:
        trigger = await db.get(m.Trigger, t_id)
        assert trigger is not None
        assert trigger.enabled is True and trigger.last_skip_reason == "busy"
        run = await db.get(m.AgentRun, busy_id)
        assert run is not None
        run.state = "done"
        await db.flush()
        assert await fire_followup(db, trigger, tenant_id=tenant) == "fired"
        assert trigger.enabled is False and trigger.last_skip_reason is None


async def test_waiting_runs_count_as_busy(app_session: AppSessionFactory) -> None:
    tenant, _, session_id, _, t_id = await _world(app_session)
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        db.add(
            m.AgentRun(
                tenant_id=tenant,
                agent_id=assistant.id,
                source="chat",
                state="waiting_for_input",
                context={"chat_session_id": str(session_id), "task": "q"},
            )
        )
        await db.flush()
        trigger = await db.get(m.Trigger, t_id)
        assert trigger is not None
        assert await fire_followup(db, trigger, tenant_id=tenant) == "busy"


async def test_soft_deleted_member_is_no_member(app_session: AppSessionFactory) -> None:
    tenant, member_id, _, _, t_id = await _world(app_session)
    async with app_session(tenant) as db:
        member = await db.get(m.OrgMember, member_id)
        assert member is not None
        member.deleted_at = dt.datetime.now(tz=dt.UTC)
        await db.flush()
        trigger = await db.get(m.Trigger, t_id)
        assert trigger is not None
        assert await fire_followup(db, trigger, tenant_id=tenant) == "no_member"
        assert (await db.execute(select(m.AgentRun))).scalars().all() == []


async def test_cron_past_ends_at_is_ended(app_session: AppSessionFactory) -> None:
    tenant, _member_id, _r_id, t_id = await _recurring_world(app_session)
    async with app_session(tenant) as db:
        trigger = await db.get(m.Trigger, t_id)
        assert trigger is not None
        trigger.ends_at = dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=1)
        await db.flush()
        assert await fire_followup(db, trigger, tenant_id=tenant) == "ended"
        assert trigger.enabled is False
        assert (await db.execute(select(m.AgentRun))).scalars().all() == []


async def test_door_and_reserved_extra_context(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    from oc8.chat.service import send_message

    tenant, _member_id, session_id, _, _ = await _world(app_session)
    async with app_session(tenant) as db:
        session = await db.get(m.ChatSession, session_id)
        assert session is not None
        _msg, web = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="hi",
            originating_operator="lisa@example.com",
            extra_context={"originating_operator": "evil", "door": "x", "keep": 1},
        )
        assert web is not None and web.context["door"] == "web"
        assert web.context["originating_operator"] == "lisa@example.com"
        assert web.context["keep"] == 1
        web.state = "done"
        await db.flush()
        _msg, tg = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="hi",
            originating_operator="lisa@example.com",
            chat_channel="telegram",
            chat_channel_external_id="1",
        )
        assert tg is not None and tg.context["door"] == "telegram"


async def test_failed_vs_quiet_vs_reporting_followup_replies(
    app_session: AppSessionFactory,
) -> None:
    from oc8.chat.service import record_assistant_reply

    tenant, _, session_id, r_id, _ = await _world(app_session)
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        resp = await db.get(m.Responsibility, r_id)
        assert resp is not None

        async def reply(state: str, reported: bool) -> int:
            run = m.AgentRun(
                tenant_id=tenant,
                agent_id=assistant.id,
                source="chat",
                state=state,
                context={
                    "chat_session_id": str(session_id),
                    "followup": {"responsibility_id": str(r_id)},
                },
            )
            db.add(run)
            await db.flush()
            resp.last_report_run_id = run.id if reported else None
            before = len(
                (
                    await db.execute(
                        select(m.ChatMessage).where(m.ChatMessage.session_id == session_id)
                    )
                )
                .scalars()
                .all()
            )
            await record_assistant_reply(db, run=run, output="out")
            await db.flush()
            after = len(
                (
                    await db.execute(
                        select(m.ChatMessage).where(m.ChatMessage.session_id == session_id)
                    )
                )
                .scalars()
                .all()
            )
            return after - before

        assert await reply("failed", False) == 1
        assert await reply("done", False) == 0
        assert await reply("done", True) == 1


async def test_executor_sender_uses_current_binding(app_session: AppSessionFactory) -> None:
    from oc8.runtime.executor import _current_chat_sender

    tenant, member_id, session_id, r_id, _ = await _world(app_session)
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        binding = m.ApprovalChannelBinding(
            tenant_id=tenant,
            channel="telegram",
            user_id=uuid.uuid4(),
            member_id=member_id,
            external_id="new",
        )
        db.add(binding)
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=assistant.id,
            source="chat",
            state="done",
            context={
                "chat_session_id": str(session_id),
                "chat_channel": "telegram",
                "chat_channel_external_id": "old",
                "followup": {"responsibility_id": str(r_id)},
            },
        )
        db.add(run)
        await db.flush()
        assert await _current_chat_sender(db, run) == ("telegram", "new")
        binding.revoked_at = dt.datetime.now(tz=dt.UTC)
        await db.flush()
        assert await _current_chat_sender(db, run) is None


class _ParkingRuntime:
    """A runtime whose run always parks on a question (ask_user)."""

    async def execute(self, db, **kw):  # type: ignore[no-untyped-def]
        from oc8.agent.engine import RunResult

        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="waiting_for_input",
            output="Which venue?",
            tool_calls=[],
            steps=1,
        )


async def _park(app_session: AppSessionFactory, source: str) -> tuple[str, str]:
    """Park a run with a followup context; returns the responsibility's state after
    the park and after the question is resolved."""
    from oc8.runtime.clarification import resolve_clarification
    from oc8.runtime.executor import execute_run
    from oc8.runtime.queue import RunMessage

    tenant, _member, session_id, r_id, _t = await _world(app_session)
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=assistant.id,
            source=source,
            state="queued",
            context={
                "task": "x",
                "chat_session_id": str(session_id),
                "followup": {"responsibility_id": str(r_id)},
            },
        )
        db.add(run)
        await db.flush()
        run_id = run.id
        await db.commit()
    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_ParkingRuntime(),
    )
    async with app_session(tenant) as db:
        parked = (await db.get(m.Responsibility, r_id)).state  # type: ignore[union-attr]
    async with app_session(tenant) as db:
        parked_run = await db.get(m.AgentRun, run_id)
        assert parked_run is not None
        await resolve_clarification(db, run=parked_run, answer="the lake")
        resolved = (await db.get(m.Responsibility, r_id)).state  # type: ignore[union-attr]
    return parked, resolved


async def test_a_chat_followup_park_flips_waiting_and_back(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    parked, resolved = await _park(app_session, "chat")
    assert (parked, resolved) == ("waiting", "active")


async def test_a_delegated_workers_question_leaves_the_responsibility_alone(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    parked, resolved = await _park(app_session, "delegation")
    assert (parked, resolved) == ("active", "active")


async def test_subscription_bound_copilot_cannot_schedule_followups(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64

    from oc8 import config
    from oc8.credentials.service import create_credential

    monkeypatch.setattr(
        config.get_settings(), "secret_kek", base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )  # fmt: skip
    tenant, member_id, _, r_id, first = await _world(app_session)
    async with app_session(tenant) as db:
        # The first follow-up predates the binding; disable it so only the
        # new one could make the pairing.
        existing = await db.get(m.Trigger, first)
        assert existing is not None
        existing.enabled = False
        cred = await create_credential(
            db, tenant_id=tenant, name=f"cg-{uuid.uuid4().hex[:8]}",
            credential_type="openai_chatgpt_subscription",
            field_values={"oauth_connection_id": str(uuid.uuid4())},
        )  # fmt: skip
        mc = m.ModelConfig(
            tenant_id=tenant, provider="openai_chatgpt", model="gpt-5", credential_id=cred.id
        )
        db.add(mc)
        await db.flush()
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        assistant.model_config_id = mc.id
        await db.flush()
        with pytest.raises(FollowupRejected, match="personal ChatGPT subscription"):
            await schedule_followup(
                db, tenant_id=tenant, member_id=member_id, responsibility_id=r_id,
                assistant_id=assistant.id, spec=SPEC, prompt="x",
                member_subject="lisa@example.com",
            )  # fmt: skip
        enabled = await db.execute(select(m.Trigger).where(m.Trigger.enabled.is_(True)))
        assert enabled.scalars().all() == []


async def test_member_without_assigned_role_cannot_schedule(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        seat = await copilot_seat(db, tenant, "tom@example.com", with_role=False)
        r = await open_responsibility(
            db, tenant_id=tenant, member_id=seat.member_id, chat_session_id=seat.session_id,
            title="t", goal="g", origin_channel=None, run_id=None,
            actor_agent_id=assistant.id, member_subject=seat.subject,
        )  # fmt: skip
        with pytest.raises(FollowupRejected, match="assigned role with Copilot access"):
            await schedule_followup(
                db, tenant_id=tenant, member_id=seat.member_id, responsibility_id=r.id,
                assistant_id=assistant.id, spec=SPEC, prompt="x", member_subject=seat.subject,
            )  # fmt: skip
        assert (await db.execute(select(m.Trigger))).scalars().all() == []


async def test_quiet_followup_leaves_no_prompt_but_reporting_and_failed_keep_it(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    from oc8.chat.service import record_assistant_reply

    tenant, _member_id, session_id, r_id, t_id = await _world(app_session)
    outcomes: dict[str, list[str]] = {}
    for label, state, reported in (
        ("quiet", "done", False),
        ("reporting", "done", True),
        ("failed", "failed", False),
    ):
        async with app_session(tenant) as db:
            trigger = await db.get(m.Trigger, t_id)
            assert trigger is not None
            trigger.enabled = True
            for old in (
                await db.execute(select(m.AgentRun).where(m.AgentRun.source == "chat"))
            ).scalars():
                old.state = "done"
            await db.flush()
            assert await fire_followup(db, trigger, tenant_id=tenant) == "fired"
        async with app_session(tenant) as db:
            run = (
                await db.execute(
                    select(m.AgentRun)
                    .where(m.AgentRun.source == "chat")
                    .order_by(m.AgentRun.created_at.desc())
                    .limit(1)
                )
            ).scalar_one()
            prompt = (
                await db.execute(
                    select(m.ChatMessage).where(
                        m.ChatMessage.session_id == session_id,
                        m.ChatMessage.role == "followup",
                        m.ChatMessage.run_id == run.id,
                    )
                )
            ).scalar_one()
            assert prompt.content.startswith("Follow-up for")
            run.state = state
            resp = await db.get(m.Responsibility, r_id)
            assert resp is not None
            resp.last_report_run_id = run.id if reported else None
            await db.flush()
            await record_assistant_reply(db, run=run, output="out")
            await db.flush()
            outcomes[label] = [
                msg.role
                for msg in (
                    await db.execute(
                        select(m.ChatMessage)
                        .where(m.ChatMessage.session_id == session_id)
                        .where(m.ChatMessage.run_id == run.id)
                        .order_by(m.ChatMessage.created_at)
                    )
                ).scalars()
            ]
            await db.commit()
    assert outcomes["quiet"] == []
    assert sorted(outcomes["reporting"]) == ["assistant", "followup"]
    assert sorted(outcomes["failed"]) == ["assistant", "followup"]


async def test_the_tenant_cap_is_enforced(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(
            m.Organization(
                id=tenant,
                slug=f"t{tenant.hex[:6]}",
                name="T",
                tier="standard",
                region="eu",
                settings={"copilot_max_active_followups": 2},
            )
        )
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        seat = await copilot_seat(db, tenant, "lisa@example.com")
        assert await max_active_followups(db, tenant_id=tenant) == 2
        for i in range(3):
            r = await open_responsibility(
                db,
                tenant_id=tenant,
                member_id=seat.member_id,
                chat_session_id=seat.session_id,
                title=f"R{i}",
                goal="Keep it on track",
                origin_channel=None,
                run_id=None,
                actor_agent_id=assistant.id,
                member_subject=seat.subject,
            )
            call = schedule_followup(
                db,
                tenant_id=tenant,
                member_id=seat.member_id,
                responsibility_id=r.id,
                assistant_id=assistant.id,
                spec=SPEC,
                prompt="check",
                member_subject=seat.subject,
            )
            if i < 2:
                await call
            else:
                with pytest.raises(FollowupRejected, match="2 active"):
                    await call


async def test_an_invalid_stored_cap_falls_back_to_the_default(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assert await max_active_followups(db, tenant_id=tenant) == MAX_ACTIVE_FOLLOWUPS
        db.add(
            m.Organization(
                id=tenant,
                slug=f"t{tenant.hex[:6]}",
                name="T",
                tier="standard",
                region="eu",
                settings={"copilot_max_active_followups": 500},
            )
        )
        await db.flush()
        assert await max_active_followups(db, tenant_id=tenant) == MAX_ACTIVE_FOLLOWUPS
