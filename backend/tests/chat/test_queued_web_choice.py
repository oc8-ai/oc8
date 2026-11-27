"""A message queued behind a running answer keeps the person's "Web" switch
(spec §10, §5.5): the opt-in belongs to the message, so the turn it becomes
may search the web; a `steer` input never changes the running run's web
state, and a withdrawn input never runs."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.engine import RunResult
from oc8.main import create_app
from oc8.runtime.run_inputs import (
    CANCELLED_STEP,
    CARRIED_OVER_STEP,
    add_run_input,
    cancel_queued_input,
)
from oc8.websearch.service import RUN_CONTEXT_KEY
from tests.api.test_chat import _headers, _open_chat_with_a_running_turn, _seed_agent
from tests.api.test_run_inputs_api import _h, _run
from tests.conftest import AppSessionFactory
from tests.runtime.test_carried_over_inputs import _execute, _state, _World, _world
from tests.runtime.test_executor import _FnRuntime

pytestmark = pytest.mark.asyncio


def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_a_plain_send_queued_behind_an_answer_keeps_its_web_switch(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        session_id, run_id = await _open_chat_with_a_running_turn(c, tenant, agent_id)
        queued = await c.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            json={"message": "Was ist heute neu?", "web": True},
            headers=_headers(tenant),
        )
        assert queued.status_code == 202, queued.text
        assert queued.json()["input"]["web"] is True
        steering = (await c.get(f"/api/v1/runs/{run_id}/steering", headers=_headers(tenant))).json()
        assert [i["web"] for i in steering["inputs"]] == [True]
    async with app_session(tenant) as db:
        [row] = list(
            (
                await db.execute(select(m.RunInput).where(m.RunInput.run_id == uuid.UUID(run_id)))
            ).scalars()
        )
        assert row.delivery == "next_turn" and row.web is True


async def test_run_inputs_take_the_web_switch_and_an_edit_keeps_it(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    run_id = await _run(app_session, tenant, operator="alice")
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.post(
            f"/api/v1/runs/{run_id}/inputs",
            json={"text": "after this", "delivery": "next_turn", "web": True},
            headers=_h(tenant, "alice"),
        )
        assert r.status_code == 201, r.text
        assert r.json()["web"] is True
        input_id = r.json()["id"]

        edited = await c.patch(
            f"/api/v1/runs/{run_id}/inputs/{input_id}",
            json={"text": "after this, in German"},
            headers=_h(tenant, "alice"),
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["web"] is True, "an edit of the text keeps the switch"

        off = await c.patch(
            f"/api/v1/runs/{run_id}/inputs/{input_id}",
            json={"text": "after this, in German", "web": False},
            headers=_h(tenant, "alice"),
        )
        assert off.status_code == 200, off.text
        assert off.json()["web"] is False

        plain = await c.post(
            f"/api/v1/runs/{run_id}/inputs",
            json={"text": "and this", "delivery": "next_turn"},
            headers=_h(tenant, "alice"),
        )
        assert plain.json()["web"] is False, "off unless switched on"


async def test_a_steer_input_never_changes_the_running_runs_web_state(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    run_id = await _run(app_session, tenant, operator="alice")
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.post(
            f"/api/v1/runs/{run_id}/inputs",
            json={"text": "use EU", "delivery": "steer", "web": True},
            headers=_h(tenant, "alice"),
        )
        assert r.status_code == 201, r.text
        assert r.json()["web"] is False
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        row = (await db.execute(select(m.RunInput).where(m.RunInput.run_id == run_id))).scalar_one()
        assert run is not None and RUN_CONTEXT_KEY not in (run.context or {})
        assert row.web is False


def _ending(
    app_session: AppSessionFactory,
    world: _World,
    *,
    status: str,
    writes: tuple[tuple[str, str, bool], ...],
) -> _FnRuntime:
    """A run written to on its last step (text, delivery, web) ends in `status`."""

    async def fn(db: Any, **kw: Any) -> RunResult:
        async with app_session(world.tenant) as w:
            run = await w.get(m.AgentRun, world.run_id)
            member = await w.get(m.OrgMember, world.member_id)
            assert run is not None and member is not None
            for text, delivery, web in writes:
                await add_run_input(
                    w,
                    run=run,
                    member=member,
                    text=text,
                    delivery=delivery,  # type: ignore[arg-type]
                    web=web,
                )
            await w.commit()
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status=status,
            output="Es sind 3 offen.",
            tool_calls=[],
            steps=1,
        )

    return _FnRuntime(fn)


@pytest.mark.parametrize("status", ["done", "failed"])
async def test_the_carried_over_turn_may_search_the_web(
    app_session: AppSessionFactory, redis_url: str, status: str
) -> None:
    world = await _world(app_session, messenger=False)

    await _execute(
        world,
        _ending(
            app_session,
            world,
            status=status,
            writes=(("Was ist heute neu?", "next_turn", True),),
        ),
    )

    runs, messages, inputs = await _state(app_session, world)
    assert [r.state for r in runs] == [status, "queued"]
    assert [i.consumed_step for i in inputs] == [CARRIED_OVER_STEP]
    assert messages[-1].content == "Was ist heute neu?"
    assert runs[1].context.get(RUN_CONTEXT_KEY) is True


async def test_a_carried_over_turn_without_the_switch_does_not(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    world = await _world(app_session, messenger=False)

    await _execute(
        world,
        _ending(
            app_session,
            world,
            status="done",
            # A steer input switched "on" changes nothing either.
            writes=(("Zuerst.", "steer", True), ("Dann das.", "next_turn", False)),
        ),
    )

    runs, _messages, _inputs = await _state(app_session, world)
    assert [r.state for r in runs] == ["done", "queued"]
    assert RUN_CONTEXT_KEY not in runs[1].context


async def test_a_withdrawn_web_input_never_runs(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    world = await _world(app_session, messenger=False)

    async def fn(db: Any, **kw: Any) -> RunResult:
        async with app_session(world.tenant) as w:
            run = await w.get(m.AgentRun, world.run_id)
            member = await w.get(m.OrgMember, world.member_id)
            assert run is not None and member is not None
            row = await add_run_input(
                w, run=run, member=member, text="Suche das.", delivery="next_turn", web=True
            )
            await cancel_queued_input(w, run=run, member=member, input_id=row.id)
            await w.commit()
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="done",
            output="Es sind 3 offen.",
            tool_calls=[],
            steps=1,
        )

    await _execute(world, _FnRuntime(fn))

    runs, _messages, inputs = await _state(app_session, world)
    assert [r.state for r in runs] == ["done"], "no turn from a withdrawn message"
    assert [i.consumed_step for i in inputs] == [CANCELLED_STEP]
