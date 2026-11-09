"""A messenger turn is its chat owner's run (Ruling 58): a second message while
it is still being answered steers it (Ruling 52), a stop word stops it, and a
parked question still takes its answer first."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.channels.binding import issue_code, redeem_code
from oc8.channels.dispatch import (
    _NOT_STEERABLE,
    _STEERED,
    _TOO_LONG,
    _TOO_MANY_WAITING,
    REFUSED,
    process_inbound,
)
from oc8.runtime.clarification import SECRET_REFUSAL
from oc8.runtime.run_inputs import MAX_INPUT_CHARS, MAX_PENDING_INPUTS, InputRefused
from tests.channels.test_free_text_end_to_end import CHANNEL, FakeChannel, _grant_copilot_manage
from tests.channels.test_messenger_answers import _bound_member_with_parked_run
from tests.conftest import AppSessionFactory


async def _bound_owner(app_session: AppSessionFactory, tenant: uuid.UUID) -> uuid.UUID:
    async with app_session(tenant) as db:
        member = m.OrgMember(
            tenant_id=tenant, subject="pat", all_departments=True, subject_uuid=uuid.uuid4()
        )
        db.add(member)
        await db.flush()
        row = await issue_code(
            db, tenant_id=tenant, user_id=uuid.uuid4(), channel=CHANNEL, member_id=member.id
        )
        code, member_id = row.code, member.id
        await db.commit()
    async with app_session(tenant) as db:
        await redeem_code(db, tenant_id=tenant, channel=CHANNEL, code=str(code), external_id="777")
        await db.commit()
    async with app_session(tenant) as db:
        reloaded = await db.get(m.OrgMember, member_id)
        assert reloaded is not None
        await _grant_copilot_manage(db, tenant_id=tenant, member=reloaded)
        await db.commit()
    return member_id


async def _say(app_session: AppSessionFactory, tenant: uuid.UUID, impl: Any, text: str) -> None:
    async with app_session(tenant) as db:
        await process_inbound(
            db,
            impl,
            tenant_id=tenant,
            channel=CHANNEL,
            update={"message": {"text": text, "from": {"id": 777}}},
        )


async def _runs(app_session: AppSessionFactory, tenant: uuid.UUID) -> list[m.AgentRun]:
    async with app_session(tenant) as db:
        return list(
            (await db.execute(select(m.AgentRun).where(m.AgentRun.tenant_id == tenant))).scalars()
        )


async def messenger_turn(app_session: AppSessionFactory, tenant: uuid.UUID) -> uuid.UUID:
    """A bound owner `pat` and the run answering her first messenger message.
    Shared with the web door's test (`tests/api/test_run_inputs_api.py`)."""
    await _bound_owner(app_session, tenant)
    await _say(app_session, tenant, FakeChannel(), "Wie viele offene Tickets?")
    [run] = await _runs(app_session, tenant)
    return run.id


async def test_a_messenger_turn_is_its_owners_run_without_acting_for_her(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    await messenger_turn(app_session, tenant)
    [run] = await _runs(app_session, tenant)
    # Whose run (steer, stop, watch) -- and still nobody's authority: the
    # secret gate, the audit trail and the PDP see no operator, as before.
    assert run.context["on_behalf_of"] == "pat"
    assert "originating_operator" not in run.context
    assert "operator_role" not in run.context


async def test_a_second_message_while_running_steers_the_run(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    run_id = await messenger_turn(app_session, tenant)
    impl = FakeChannel()

    await _say(app_session, tenant, impl, "Nur die aus Berlin, bitte.")

    runs = await _runs(app_session, tenant)
    assert [r.id for r in runs] == [run_id], "no second run beside the running one"
    async with app_session(tenant) as db:
        inputs = list(
            (await db.execute(select(m.RunInput).where(m.RunInput.run_id == run_id))).scalars()
        )
        messages = list(
            (await db.execute(select(m.ChatMessage).where(m.ChatMessage.tenant_id == tenant)))
            .scalars()
            .all()
        )
    assert [i.text for i in inputs] == ["Nur die aus Berlin, bitte."]
    assert len(messages) == 1, "the steering text is an input, not a new turn"
    sent = impl.say.await_args
    assert sent is not None and sent.args[1] == _STEERED


async def test_the_steering_acknowledgement_obeys_the_channel_gate(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    run_id = await messenger_turn(app_session, tenant)
    impl = FakeChannel(max_classification="public")

    await _say(app_session, tenant, impl, "Nur die aus Berlin, bitte.")

    sent = impl.say.await_args
    assert sent is not None and "laufende Antwort" not in sent.args[1]
    assert [r.id for r in await _runs(app_session, tenant)] == [run_id]


async def test_abbrechen_stops_the_running_run(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    run_id = await messenger_turn(app_session, tenant)
    impl = FakeChannel()

    await _say(app_session, tenant, impl, "Abbrechen")

    assert [r.id for r in await _runs(app_session, tenant)] == [run_id]
    async with app_session(tenant) as db:
        cancels = list(
            (
                await db.execute(
                    select(m.RunCancellation).where(m.RunCancellation.run_id == run_id)
                )
            ).scalars()
        )
        events = list(
            (
                await db.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.tenant_id == tenant, m.AuditEvent.action == "run.cancel"
                    )
                )
            ).scalars()
        )
        inputs = list(
            (await db.execute(select(m.RunInput).where(m.RunInput.run_id == run_id))).scalars()
        )
    assert len(cancels) == 1 and cancels[0].cancellation_kind == "operator_interrupted"
    assert events and events[0].resource["door"] == CHANNEL
    assert inputs == [], "the stop word is not passed on as text"
    sent = impl.say.await_args
    assert sent is not None and sent.args[1] == "Gestoppt."


async def test_somebody_elses_running_run_is_not_steered(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """A run in her chat that is not hers (here: nobody's, as messenger turns
    were before Ruling 58) is left alone; the message starts a turn."""
    tenant = uuid.uuid4()
    run_id = await messenger_turn(app_session, tenant)
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {k: v for k, v in run.context.items() if k != "on_behalf_of"}
        await db.commit()

    await _say(app_session, tenant, FakeChannel(), "Noch eine Frage")

    assert len(await _runs(app_session, tenant)) == 2


async def test_a_parked_question_still_takes_the_answer(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    run_id, _sid = await _bound_member_with_parked_run(app_session, tenant, structured=True)
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {**run.context, "on_behalf_of": "pat"}
        await db.commit()
    impl = FakeChannel()

    with patch("oc8.runtime.intake.publish_run"):
        await _say(app_session, tenant, impl, "1,3")

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None and run.state == "queued"
        assert run.context["clarifications"][-1]["answers"] == {"ch": ["mail", "push"]}
        inputs = list(
            (await db.execute(select(m.RunInput).where(m.RunInput.run_id == run_id))).scalars()
        )
    assert inputs == []
    sent = impl.say.await_args
    assert sent is not None and sent.args[1] == "Danke, ich mache weiter."


async def _inputs(
    app_session: AppSessionFactory, tenant: uuid.UUID, run_id: uuid.UUID
) -> list[str]:
    async with app_session(tenant) as db:
        return [
            row.text
            for row in (
                await db.execute(
                    select(m.RunInput).where(m.RunInput.run_id == run_id).order_by(m.RunInput.seq)
                )
            ).scalars()
        ]


async def test_a_run_that_ended_before_the_write_gets_a_new_turn(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 409 race (Ruling 59): the run was found running, then ended before
    the input was written. The message is a new turn, not a refusal."""
    tenant = uuid.uuid4()
    run_id = await messenger_turn(app_session, tenant)

    async def ended(*_a: Any, **_kw: Any) -> None:
        raise InputRefused(409, "run has already finished", "finished")

    monkeypatch.setattr("oc8.runtime.run_inputs.add_run_input", ended)
    impl = FakeChannel()

    await _say(app_session, tenant, impl, "Noch eine Frage")

    runs = await _runs(app_session, tenant)
    assert len(runs) == 2 and run_id in [r.id for r in runs]
    sent = impl.say.await_args
    assert sent is not None and sent.args[1] == "Bin dran, melde mich gleich."


async def test_a_runtime_that_takes_no_messages_says_so_and_starts_nothing(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    run_id = await messenger_turn(app_session, tenant)

    async def no(*_a: Any, **_kw: Any) -> bool:
        return False

    monkeypatch.setattr("oc8.runtime.run_inputs.run_accepts_inputs", no)
    impl = FakeChannel()

    await _say(app_session, tenant, impl, "Nur Berlin")

    assert [r.id for r in await _runs(app_session, tenant)] == [run_id]
    assert await _inputs(app_session, tenant, run_id) == []
    sent = impl.say.await_args
    assert sent is not None and sent.args[1] == _NOT_STEERABLE


async def test_a_stop_word_stops_a_run_that_takes_no_messages(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    run_id = await messenger_turn(app_session, tenant)

    async def no(*_a: Any, **_kw: Any) -> bool:
        return False

    monkeypatch.setattr("oc8.runtime.run_inputs.run_accepts_inputs", no)
    impl = FakeChannel()

    await _say(app_session, tenant, impl, "stop")

    async with app_session(tenant) as db:
        cancel = (
            await db.execute(select(m.RunCancellation).where(m.RunCancellation.run_id == run_id))
        ).scalar_one_or_none()
    assert cancel is not None
    sent = impl.say.await_args
    assert sent is not None and sent.args[1] == "Gestoppt."


async def test_a_full_queue_is_refused_in_german(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    run_id = await messenger_turn(app_session, tenant)
    async with app_session(tenant) as db:
        member = (
            await db.execute(select(m.OrgMember).where(m.OrgMember.tenant_id == tenant))
        ).scalar_one()
        for i in range(MAX_PENDING_INPUTS):
            db.add(
                m.RunInput(tenant_id=tenant, run_id=run_id, author_member_id=member.id, text=f"{i}")
            )
        await db.commit()
    impl = FakeChannel()

    await _say(app_session, tenant, impl, "Noch was")

    assert len(await _inputs(app_session, tenant, run_id)) == MAX_PENDING_INPUTS
    assert len(await _runs(app_session, tenant)) == 1
    sent = impl.say.await_args
    assert sent is not None and sent.args[1] == _TOO_MANY_WAITING


async def test_an_overlong_message_is_refused_in_german(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    run_id = await messenger_turn(app_session, tenant)
    impl = FakeChannel()

    await _say(app_session, tenant, impl, "x" * (MAX_INPUT_CHARS + 1))

    assert await _inputs(app_session, tenant, run_id) == []
    assert len(await _runs(app_session, tenant)) == 1
    sent = impl.say.await_args
    assert sent is not None and sent.args[1] == _TOO_LONG


async def test_a_secret_to_the_running_answer_is_refused(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    run_id = await messenger_turn(app_session, tenant)
    impl = FakeChannel()

    await _say(app_session, tenant, impl, "password=hunter2")

    assert await _inputs(app_session, tenant, run_id) == []
    assert len(await _runs(app_session, tenant)) == 1
    sent = impl.say.await_args
    assert sent is not None and sent.args[1] == SECRET_REFUSAL


async def test_a_deleted_member_is_refused(app_session: AppSessionFactory, redis_url: str) -> None:
    tenant = uuid.uuid4()
    member_id = await _bound_owner(app_session, tenant)
    async with app_session(tenant) as db:
        member = await db.get(m.OrgMember, member_id)
        assert member is not None
        member.deleted_at = dt.datetime.now(tz=dt.UTC)
        await db.commit()
    impl = FakeChannel()

    await _say(app_session, tenant, impl, "Hallo")

    assert await _runs(app_session, tenant) == []
    sent = impl.say.await_args
    assert sent is not None and sent.args[1] == REFUSED
