from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.chat.service import delete_session
from oc8.copilot.followups import schedule_followup
from oc8.copilot.responsibilities import close_responsibility, open_responsibility
from tests.conftest import AppSessionFactory
from tests.copilot.helpers import Seat, copilot_seat, once_in_an_hour


async def _open_with_followup(
    db: AsyncSession, tenant: uuid.UUID, seat: Seat, assistant_id: uuid.UUID, title: str
) -> tuple[m.Responsibility, m.Trigger]:
    r = await open_responsibility(
        db,
        tenant_id=tenant,
        member_id=seat.member_id,
        chat_session_id=seat.session_id,
        title=title,
        goal="Keep it on track",
        origin_channel=None,
        run_id=None,
        actor_agent_id=assistant_id,
        member_subject=seat.subject,
    )
    t = await schedule_followup(
        db,
        tenant_id=tenant,
        member_id=seat.member_id,
        responsibility_id=r.id,
        assistant_id=assistant_id,
        spec=once_in_an_hour(),
        prompt="check",
        member_subject=seat.subject,
    )
    return r, t


async def test_deleting_a_session_cancels_its_open_responsibilities(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        seat = await copilot_seat(db, tenant, "lisa@example.com")
        other = await copilot_seat(db, tenant, "tom@example.com")
        open_r, open_t = await _open_with_followup(db, tenant, seat, assistant.id, "open")
        done_r, _done_t = await _open_with_followup(db, tenant, seat, assistant.id, "done")
        await close_responsibility(
            db,
            tenant_id=tenant,
            member_id=seat.member_id,
            responsibility_id=done_r.id,
            state="done",
            reason="finished",
            actor_agent_id=None,
            member_subject=seat.subject,
        )
        other_r, other_t = await _open_with_followup(db, tenant, other, assistant.id, "other")
        session = await db.get(m.ChatSession, seat.session_id)
        assert session is not None
        await delete_session(db, tenant_id=tenant, session=session)
        await db.flush()

        for r_id, state, reason, enabled_t in (
            (open_r.id, "cancelled", "conversation deleted", open_t.id),
            (done_r.id, "done", "finished", None),
            (other_r.id, "active", None, other_t.id),
        ):
            r = (
                await db.execute(
                    select(m.Responsibility)
                    .where(m.Responsibility.id == r_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            assert (r.state, r.close_reason) == (state, reason)
            if enabled_t is not None:
                t = (
                    await db.execute(
                        select(m.Trigger)
                        .where(m.Trigger.id == enabled_t)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                assert t.enabled is (state == "active")
