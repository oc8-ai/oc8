"""The realtime socket must not be a way round `GET /activity`'s permission.

Reproduced before this gate existed, against the running app: an employee token
-- role `member`, which holds `frozenset()`, holding no seat at all -- was
accepted onto `/events/ws` and received

    {'type': 'activity.logged', 'data': {
        'agent_id': 'engineering-agent',
        'message': 'Ada oeffnet Produktionszugang fuer Kunde Gartenholz',
        'detail': 'prod.deploy(env=live, secret_ref=...)'}}

while `GET /activity` answered him 403. Same rows, same tenant, one door open.

The producers are not incidental. `agent/control_tools.py` writes
`"Entscheidung angefragt: {question}"` with `context[:500]` as detail -- the very
question `GET /clarifications` scopes by department -- and `realtime/emit.py`'s
`note_focus` writes what an agent is working on right now.

This is not per-department rooms; those are §10 item 5 and slice 2. It is the
narrower claim §9 already makes for this slice and which did not hold: the
socket's audience is exactly the HTTP route's.
"""

from __future__ import annotations

import uuid

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from oc8.auth import get_identity_provider
from oc8.authz.permissions import AUDITOR, MEMBER_ROLE, OPERATOR, ORG_ADMIN, RUN, VIEW, perm
from oc8.main import create_app
from tests.conftest import AppSessionFactory


@pytest.fixture
def app_session_sync_member(app_session: AppSessionFactory) -> tuple[uuid.UUID, str]:
    import asyncio

    from oc8 import models as m

    tenant, subject = uuid.uuid4(), "member-ws"

    async def _seed() -> None:
        async with app_session(tenant) as db:
            db.add(m.OrgMember(tenant_id=tenant, subject=subject, subject_uuid=uuid.uuid4()))

    asyncio.run(_seed())
    return tenant, subject


def _token(tenant: uuid.UUID, role: str) -> str:
    return get_identity_provider().mint(subject="somebody", tenant_id=tenant, role=role)


@pytest.mark.parametrize("role", [MEMBER_ROLE, "menber", ""])
def test_a_role_without_run_view_and_without_a_member_row_is_refused(
    redis_url: str, role: str
) -> None:
    """`member` is the role this slice mints for the employee whose authority is a
    seat; `menber` is a typo nobody mapped. Neither holds `run:view`, both are
    403'd at `GET /activity` -- and without a member row there is nothing a
    member-only socket could carry, so the socket stays shut.

    A seat is deliberately NOT enough for the feed either -- and could not be: a
    seat's vocabulary is closed at four permissions and `run:view` is not one of
    them (`tests/authz/test_seat_vocabulary.py`), so there is nothing to check
    against.
    """
    tenant = uuid.uuid4()
    app = create_app()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/api/v1/events/ws?token={_token(tenant, role)}"):
                pass


def test_a_member_gets_a_member_only_socket_without_the_feed(
    redis_url: str, app_session_sync_member: tuple[uuid.UUID, str]
) -> None:
    """A member (no `run:view`) is joined for their own events only. The feed
    (`activity.logged`, approvals, other people's runs) never reaches them --
    the guarantee this file exists for."""
    tenant, subject = app_session_sync_member
    app = create_app()
    token = get_identity_provider().mint(subject=subject, tenant_id=tenant, role=MEMBER_ROLE)
    with TestClient(app) as client:
        with client.websocket_connect(f"/api/v1/events/ws?token={token}") as ws:
            hello = ws.receive_json()
            assert hello == {"type": "realtime.connected", "feed": False}


@pytest.mark.parametrize("role", [ORG_ADMIN, OPERATOR, AUDITOR])
def test_every_role_that_may_read_the_feed_over_http_still_gets_the_socket(
    redis_url: str, role: str
) -> None:
    """The guard against fixing the leak by breaking the product.

    `run:view` is swept into `_VIEW_EVERYTHING`, so all three tenant-wide roles
    hold it -- which is the whole point: this subtracts the population the slice
    created and nobody else. Asserted through the same permission the endpoint
    reads, so a change to `_VIEW_EVERYTHING` cannot leave this test agreeing with
    a stale copy of the answer.
    """
    from oc8.authz.permissions import role_has

    assert role_has(role, perm(RUN, VIEW)), role
    tenant = uuid.uuid4()
    app = create_app()
    with TestClient(app) as client:
        with client.websocket_connect(f"/api/v1/events/ws?token={_token(tenant, role)}") as ws:
            assert ws.receive_json() == {"type": "realtime.connected", "feed": True}


def test_run_watch_answers_only_for_a_run_the_member_may_watch(
    redis_url: str, app_session: AppSessionFactory
) -> None:
    """A foreign run id gets no answer at all; the member's own run gets
    `run.watching`. The order of the two replies proves the first was silent."""
    import asyncio

    from oc8 import models as m
    from tests.copilot.helpers import copilot_seat

    tenant = uuid.uuid4()
    run_ids: dict[str, uuid.UUID] = {}

    async def _seed() -> None:
        async with app_session(tenant) as db:
            for subject in ("alice", "bob"):
                seat = await copilot_seat(db, tenant, subject)
                session = await db.get(m.ChatSession, seat.session_id)
                assert session is not None
                run = m.AgentRun(
                    tenant_id=tenant,
                    agent_id=session.agent_id,
                    source="chat",
                    context={
                        "chat_session_id": str(seat.session_id),
                        "originating_operator": subject,
                    },
                )
                db.add(run)
                await db.flush()
                run_ids[subject] = run.id

    asyncio.run(_seed())
    token = get_identity_provider().mint(subject="alice", tenant_id=tenant, role=MEMBER_ROLE)
    app = create_app()
    with TestClient(app) as client:
        with client.websocket_connect(f"/api/v1/events/ws?token={token}") as ws:
            assert ws.receive_json() == {"type": "realtime.connected", "feed": False}
            ws.send_json({"type": "run.watch", "runId": str(run_ids["bob"])})
            ws.send_json({"type": "run.watch", "runId": "not-a-uuid"})
            ws.send_json({"type": "run.watch", "runId": str(run_ids["alice"])})
            assert ws.receive_json() == {"type": "run.watching", "runId": str(run_ids["alice"])}
