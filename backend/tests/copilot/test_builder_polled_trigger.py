"""A whole new agent from the Copilot whose trigger listens on a polled event:
a brand-new agent has no fixed login yet, so the agent is created without that
trigger and the card says what the person does next -- the build is not lost."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.api.v1.copilot_builder import builder_dto
from oc8.auth import Principal
from oc8.copilot.builder import apply_builder_proposal, load_steps
from oc8.copilot.builder_notice import REASONS
from oc8.events.sources import PollResult
from tests.conftest import AppSessionFactory
from tests.copilot.builder_helpers import builder_office, invoice_checker_ops, propose_as_admin
from tests.events.polled import EVENT_TYPE, FakePoller, Polled, install_capa, register, use_kek

pytestmark = pytest.mark.asyncio
NOW = dt.datetime(2026, 11, 20, 9, 0, tzinfo=dt.UTC)


def _never(*_args: Any) -> PollResult:
    raise AssertionError("not polled in these tests")


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    use_kek(monkeypatch)


async def test_a_new_agent_is_built_without_its_polled_trigger_and_says_so(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    source = f"src_{uuid.uuid4().hex[:10]}"
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
    await install_capa(app_session, tenant, source, f"{source}_tools")
    register(
        Polled(tenant, uuid.uuid4(), uuid.uuid4(), source, f"{source}_tools"), FakePoller(_never)
    )
    ops = [
        *invoice_checker_ops(office),
        {
            "type": "trigger.create",
            "agentRef": "$new",
            "kind": "event",
            "taskText": "Sortier die neue Post.",
            "eventSource": source,
            "eventType": EVENT_TYPE,
            "eventConfig": {"folder": "Orders"},
        },
    ]

    async with app_session(tenant) as db:
        proposal = await propose_as_admin(db, office, ops, now=NOW)
        agent_id = await apply_builder_proposal(
            db,
            proposal_id=proposal.id,
            expect_status="draft",
            applied_by="admin",
            principal=Principal(subject="admin", tenant_id=tenant, role="org_admin"),
            now=NOW,
        )
        triggers = (
            (await db.execute(select(m.Trigger).where(m.Trigger.agent_id == agent_id)))
            .scalars()
            .all()
        )
        # The rest of the build stands, the schedule included.
        assert [t.kind for t in triggers] == ["cron"]
        assert proposal.status == "applied"
        assert proposal.result["notices"] == ["trigger_needs_login"]
        assert "refusal" not in proposal.result
        card = await builder_dto(
            db,
            proposal,
            await load_steps(db, proposal),
            may_create=True,
            read_only=False,
            show_mission=True,
        )
        assert card.notices == ["trigger_needs_login"]
        assert card.agent is not None and card.agent.id == str(agent_id)
        event = next(t for t in card.triggers if t.kind == "event")
        assert [(s.key, s.label, s.value) for s in event.settings] == [
            ("folder", "Folder", "Orders")
        ]


def test_the_copilot_is_told_the_real_remedy() -> None:
    text = REASONS["trigger_needs_login"]
    assert "tool settings" in text
    assert "the person" in text
