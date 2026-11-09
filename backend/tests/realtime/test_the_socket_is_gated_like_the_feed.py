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
from oc8.authz.permissions import (
    AUDITOR,
    BUILTIN_ROLE_PERMISSIONS,
    COPILOT_USE,
    MEMBER_ROLE,
    OPERATOR,
    ORG_ADMIN,
    RUN,
    VIEW,
    perm,
)
from oc8.main import create_app


def _token(tenant: uuid.UUID, role: str) -> str:
    return get_identity_provider().mint(subject="somebody", tenant_id=tenant, role=role)


@pytest.mark.parametrize("role", [MEMBER_ROLE, "menber", ""])
def test_a_role_without_run_view_is_refused_the_feed(redis_url: str, role: str) -> None:
    """`member` is the role this slice mints for the employee whose authority is a
    seat; `menber` is a typo nobody mapped. Neither holds `run:view`, both are
    403'd at `GET /activity`, and both must be refused here for the same reason.

    A seat is deliberately NOT enough either -- and could not be: a seat's
    vocabulary is closed at four permissions and `run:view` is not one of them
    (`tests/authz/test_seat_vocabulary.py`), so there is nothing to check against.
    """
    tenant = uuid.uuid4()
    app = create_app()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/api/v1/events/ws?token={_token(tenant, role)}"):
                pass


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
            assert ws.receive_json()["type"] == "realtime.connected"


def test_run_view_without_a_tenant_wide_agent_read_is_refused_the_feed(
    redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`GET /activity` narrows a caller who holds `run:view` but reads agents
    only through seats to her departments. The socket is the whole tenant's
    fan-out and cannot narrow, so it admits only an every-agent run reader
    (`agents.repo.reads_every_run`) -- holding `run:view` alone is refused.

    The role is the one the E2E hit: `member` with `run:view` and no
    `agent:view`, recreated here for one test.
    """
    monkeypatch.setitem(
        BUILTIN_ROLE_PERMISSIONS, MEMBER_ROLE, frozenset({COPILOT_USE, perm(RUN, VIEW)})
    )
    tenant = uuid.uuid4()
    app = create_app()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/api/v1/events/ws?token={_token(tenant, MEMBER_ROLE)}"):
                pass
