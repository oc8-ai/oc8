"""Direct chat with one agent (oc8.chat.service). Each message is a real
AgentRun (source="chat"), so this exercises the full HTTP surface --
session create/list, message send/list -- against a real app instance."""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.chat.reach import assistant_visible
from oc8.auth import get_identity_provider
from oc8.authz.authority import Authority
from oc8.authz.permissions import COPILOT, COPILOT_USE, MANAGE, perm
from oc8.authz.scope import subject_uuid_for
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def _seed_agent(app_session: AppSessionFactory, tenant: uuid.UUID) -> uuid.UUID:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Helpdesk", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Lennart")
        db.add(agent)
        await db.flush()
        return agent.id


async def test_create_session_requires_a_visible_agent(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                "/api/v1/chat/sessions",
                json={"agentId": str(uuid.uuid4())},
                headers=_headers(tenant),
            )
            assert r.status_code == 404, r.text


async def test_create_session_then_list_it(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            create_r = await c.post(
                "/api/v1/chat/sessions",
                json={"agentId": str(agent_id)},
                headers=_headers(tenant),
            )
            assert create_r.status_code == 201, create_r.text
            session_id = create_r.json()["id"]

            list_r = await c.get(
                f"/api/v1/chat/sessions?agentId={agent_id}", headers=_headers(tenant)
            )
            assert list_r.status_code == 200, list_r.text
            ids = [s["id"] for s in list_r.json()]
            assert session_id in ids


async def test_listing_sessions_by_agent_excludes_another_agents_sessions(
    app_session: AppSessionFactory,
) -> None:
    """`GET /chat/sessions?agentId=...` binds via `Query(alias="agentId")` --
    the frontend sends camelCase on the wire (see hooks-chat.ts's
    useChatSessions), and FastAPI does not fold camelCase onto a snake_case
    parameter name on its own. Without the alias this filter silently no-ops
    (`agent_id` stays None), and every member's sessions across every agent
    come back regardless of which agent was asked for -- exactly the bug a
    single-agent test like test_create_session_then_list_it above can never
    catch, since with only one agent in play an unfiltered and a correctly
    filtered result look identical."""
    tenant = uuid.uuid4()
    lennart_id = await _seed_agent(app_session, tenant)
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Helpdesk", frame={})
        db.add(dept)
        await db.flush()
        tim = m.Agent(tenant_id=tenant, department_id=dept.id, name="Tim")
        db.add(tim)
        await db.flush()
        tim_id = tim.id
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            lennart_session = (
                await c.post(
                    "/api/v1/chat/sessions",
                    json={"agentId": str(lennart_id)},
                    headers=_headers(tenant),
                )
            ).json()["id"]
            tim_session = (
                await c.post(
                    "/api/v1/chat/sessions",
                    json={"agentId": str(tim_id)},
                    headers=_headers(tenant),
                )
            ).json()["id"]

            tim_list = await c.get(
                f"/api/v1/chat/sessions?agentId={tim_id}", headers=_headers(tenant)
            )
            tim_ids = [s["id"] for s in tim_list.json()]
            assert tim_session in tim_ids
            assert lennart_session not in tim_ids

            lennart_list = await c.get(
                f"/api/v1/chat/sessions?agentId={lennart_id}", headers=_headers(tenant)
            )
            lennart_ids = [s["id"] for s in lennart_list.json()]
            assert lennart_session in lennart_ids
            assert tim_session not in lennart_ids


async def test_send_message_creates_a_chat_source_run(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    # redis_url must be REQUESTED, not merely available: enqueue_run publishes
    # onto the run queue, which lazily binds to the testcontainer only when a
    # test in the current session has asked for it first.
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            create_r = await c.post(
                "/api/v1/chat/sessions",
                json={"agentId": str(agent_id)},
                headers=_headers(tenant),
            )
            session_id = create_r.json()["id"]

            send_r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"message": "Hallo!"},
                headers=_headers(tenant),
            )
            assert send_r.status_code == 201, send_r.text
            body = send_r.json()
            assert body["role"] == "user"
            assert body["content"] == "Hallo!"

    async with app_session(tenant) as db:
        run = (
            await db.execute(
                select(m.AgentRun).where(
                    m.AgentRun.agent_id == agent_id, m.AgentRun.source == "chat"
                )
            )
        ).scalar_one()
        assert run.context["chat_session_id"] == session_id
        assert run.context["task"] == "User: Hallo!"


async def test_send_message_response_carries_the_run_id_it_triggered_but_does_not_persist_it(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """The POST response's `runId` (api/v1/chat.py's `post_message`) is a
    same-response-only correlation signal, NOT a persisted column value --
    the user's own ChatMessage row stays runId: null forever, exactly as it
    did before this field was added (every later GET of it still reports
    null). It exists so a caller can correlate a later assistant reply to
    THIS specific request by matching runId instead of transcript position
    -- needed once two surfaces (the floating Copilot dock and the agent
    Instructions panel's "Draft with copilot") can both post into the same
    tenant-wide Assistant chat session and interleave."""
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            create_r = await c.post(
                "/api/v1/chat/sessions",
                json={"agentId": str(agent_id)},
                headers=_headers(tenant),
            )
            session_id = create_r.json()["id"]

            send_r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"message": "Hallo!"},
                headers=_headers(tenant),
            )
            assert send_r.status_code == 201, send_r.text
            run_id_on_response = send_r.json()["runId"]
            assert run_id_on_response is not None

            list_r = await c.get(
                f"/api/v1/chat/sessions/{session_id}/messages", headers=_headers(tenant)
            )
            assert list_r.status_code == 200, list_r.text
            assert list_r.json()[0]["runId"] is None

    async with app_session(tenant) as db:
        run = (
            await db.execute(
                select(m.AgentRun).where(
                    m.AgentRun.agent_id == agent_id, m.AgentRun.source == "chat"
                )
            )
        ).scalar_one()
        assert run_id_on_response == str(run.id)


async def test_rendered_components_reach_the_wire_as_camel_case(
    app_session: AppSessionFactory,
) -> None:
    """Regression test: `rendered_components` is stored as plain
    `{"component_key": ..., "props": ...}` dicts (control_tools.py's
    ControlOutcome, never itself a CamelModel). A DTO field typed as a bare
    `dict[str, object]` bypasses CamelModel's alias generator entirely, so
    the wire response silently kept the snake_case key -- the frontend's
    `c.componentKey` lookup then read `undefined` and rendered nothing,
    even though the data had round-tripped through the database correctly."""
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            create_r = await c.post(
                "/api/v1/chat/sessions",
                json={"agentId": str(agent_id)},
                headers=_headers(tenant),
            )
            session_id = create_r.json()["id"]

    async with app_session(tenant) as db:
        db.add(
            m.ChatMessage(
                tenant_id=tenant,
                session_id=session_id,
                role="assistant",
                content="Hier ist die Tabelle.",
                rendered_components=[
                    {"component_key": "data_table", "props": {"title": "Tickets"}}
                ],
            )
        )

    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(
                f"/api/v1/chat/sessions/{session_id}/messages", headers=_headers(tenant)
            )
            assert r.status_code == 200, r.text
            components = r.json()[0]["renderedComponents"]
            assert components == [{"componentKey": "data_table", "props": {"title": "Tickets"}}]


async def test_send_message_to_a_foreign_session_is_refused(
    app_session: AppSessionFactory,
) -> None:
    """A session belongs to the member who created it -- another operator's
    token must not be able to post into it, even inside the same tenant."""
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    app = create_app()
    other_token = get_identity_provider().mint(
        tenant_id=tenant, subject="other-operator", role="org_admin"
    )
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            create_r = await c.post(
                "/api/v1/chat/sessions",
                json={"agentId": str(agent_id)},
                headers=_headers(tenant),
            )
            session_id = create_r.json()["id"]

            r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"message": "Hallo!"},
                headers={"Authorization": f"Bearer {other_token}"},
            )
            assert r.status_code == 404, r.text


async def test_get_messages_returns_them_in_order(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            create_r = await c.post(
                "/api/v1/chat/sessions",
                json={"agentId": str(agent_id)},
                headers=_headers(tenant),
            )
            session_id = create_r.json()["id"]
            await c.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"message": "erstens"},
                headers=_headers(tenant),
            )
            await c.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"message": "zweitens"},
                headers=_headers(tenant),
            )
            r = await c.get(
                f"/api/v1/chat/sessions/{session_id}/messages", headers=_headers(tenant)
            )
            assert r.status_code == 200, r.text
            contents = [m["content"] for m in r.json()]
            assert contents == ["erstens", "zweitens"]


# --- Task 4: chat visibility bypass for the tenant Assistant + GET /assistant ---
#
# The Assistant lives in its OWN dedicated department (`get_or_create_assistant`),
# so the ordinary department-scoped `visible_agent` check would 404 a caller who
# holds `copilot:manage` but no seat there. NOTE ON THIS SUITE'S ROLE CHOICE:
# `copilot:manage` is `NEVER_DELEGATABLE` (permissions.py) and is granted to
# exactly one built-in role -- `org_admin` -- which already holds tenant-wide
# `agent:view` via the pre-existing token-floor bypass (`tenant_wide_read`).
# There is today no real, non-`org_admin` caller who holds `copilot:manage`
# without ALSO already bypassing department scope by that older mechanism, so
# `create_chat_session`'s department-carve-out cannot be isolated end-to-end
# through the ordinary role-assignment path -- `test_assistant_visible_...`
# below tests that exact code path directly instead. `get_messages`/
# `post_message`'s carve-out, by contrast, IS independently observable through
# HTTP even for two `org_admin`s: their ownership check is per-member
# (`session.member_id != actor.member.id`), which `org_admin`'s blanket
# tenant-wide read has never touched.


async def test_get_assistant_lazily_provisions_and_is_idempotent(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            first = await c.get("/api/v1/assistant", headers=_headers(tenant))
            assert first.status_code == 200, first.text
            agent_id = first.json()["agentId"]
            assert agent_id

            second = await c.get("/api/v1/assistant", headers=_headers(tenant))
            assert second.status_code == 200, second.text
            assert second.json()["agentId"] == agent_id

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, uuid.UUID(agent_id))
        assert agent is not None
        assert agent.is_tenant_assistant is True


async def test_copilot_manage_holder_can_open_a_session_with_the_assistant(
    app_session: AppSessionFactory,
) -> None:
    """A member who holds copilot:manage but has NO seat in the Assistant's
    own department must still be able to create a session with it."""
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assistant_r = await c.get("/api/v1/assistant", headers=_headers(tenant))
            assert assistant_r.status_code == 200, assistant_r.text
            assistant_agent_id = assistant_r.json()["agentId"]

            create_r = await c.post(
                "/api/v1/chat/sessions",
                json={"agentId": assistant_agent_id},
                headers=_headers(tenant),
            )
            assert create_r.status_code == 201, create_r.text


async def test_create_chat_session_actually_calls_the_assistant_visible_bypass(
    app_session: AppSessionFactory,
) -> None:
    """The previous test cannot, by itself, prove `create_chat_session`'s
    `tenant_wide_read(...) or await assistant_visible(...)` line is wired in
    correctly -- `org_admin` (the only real copilot:manage holder in today's
    permission catalog) already satisfies `tenant_wide_read` on its own, so
    that `or` never even needs to evaluate its right-hand side for org_admin.
    This test forces `tenant_wide_read` to return False, so the ONLY way the
    request can still succeed is if `assistant_visible` is actually being
    called, actually being awaited, and its result actually being OR'd in --
    catching a reverted or dropped `or` clause that the helper's own unit
    test (`test_assistant_visible_requires_...`) cannot catch."""
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assistant_r = await c.get("/api/v1/assistant", headers=_headers(tenant))
            assert assistant_r.status_code == 200, assistant_r.text
            assistant_agent_id = assistant_r.json()["agentId"]

            with patch("oc8.chat.reach.tenant_wide_read", return_value=False):
                create_r = await c.post(
                    "/api/v1/chat/sessions",
                    json={"agentId": assistant_agent_id},
                    headers=_headers(tenant),
                )
            assert create_r.status_code == 201, create_r.text


async def test_a_bare_member_now_reaches_the_assistant_via_copilot_use(
    app_session: AppSessionFactory,
) -> None:
    """The whole point of this slice: a caller who holds agent:view only
    through a seat in an UNRELATED department -- and no copilot:manage -- now
    reaches the tenant Assistant anyway, because `member` holds copilot:use
    tenant-wide by default. This replaces the old
    test_seat_only_member_without_copilot_manage_still_cannot_reach_the_assistant,
    which asserted the opposite (404) before copilot:use existed."""
    tenant = uuid.uuid4()
    subject = "seat-only-viewer"
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        other_dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(other_dept)
        await db.flush()
        member = m.OrgMember(
            tenant_id=tenant, subject=subject, subject_uuid=subject_uuid_for(subject)
        )
        db.add(member)
        await db.flush()
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=member.id,
                department_id=other_dept.id,
                seat_role="dept_viewer",
            )
        )
        await db.flush()
        assistant_agent_id = assistant.id

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role="member")
            r = await c.post(
                "/api/v1/chat/sessions",
                json={"agentId": str(assistant_agent_id)},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 201, r.text


async def test_an_unmapped_role_still_cannot_reach_the_assistant(
    app_session: AppSessionFactory,
) -> None:
    """The negative control for the test above: copilot:use is what admits a
    bare member, not merely holding SOME token -- a role name `permissions_for`
    does not recognise resolves to the empty set (fail-closed) and still 403s,
    proving the enforcement boundary is real rather than "any token works"."""
    tenant = uuid.uuid4()
    subject = "unmapped-role-holder"
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        assistant_agent_id = assistant.id

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            token = get_identity_provider().mint(
                tenant_id=tenant, subject=subject, role="not_a_real_role"
            )
            r = await c.post(
                "/api/v1/chat/sessions",
                json={"agentId": str(assistant_agent_id)},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 403, r.text


async def test_copilot_manage_holder_can_read_and_reply_in_a_colleagues_assistant_session(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """The Assistant session ownership check (`session.member_id !=
    actor.member.id`) is per-member and untouched by department/tenant-wide
    scope -- so even a second org_admin (a real copilot:manage holder) would
    404 on a colleague's Assistant session without this bypass. Regression
    for the get_messages/post_message carve-out specifically."""
    tenant = uuid.uuid4()
    app = create_app()
    other_token = get_identity_provider().mint(
        tenant_id=tenant, subject="other-operator", role="org_admin"
    )
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assistant_r = await c.get("/api/v1/assistant", headers=_headers(tenant))
            assistant_agent_id = assistant_r.json()["agentId"]

            create_r = await c.post(
                "/api/v1/chat/sessions",
                json={"agentId": assistant_agent_id},
                headers=_headers(tenant),
            )
            session_id = create_r.json()["id"]

            get_r = await c.get(
                f"/api/v1/chat/sessions/{session_id}/messages",
                headers={"Authorization": f"Bearer {other_token}"},
            )
            assert get_r.status_code == 200, get_r.text

            post_r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"message": "Hallo!"},
                headers={"Authorization": f"Bearer {other_token}"},
            )
            assert post_r.status_code == 201, post_r.text


async def test_a_foreign_seat_only_member_without_copilot_manage_still_cannot_read_a_session(
    app_session: AppSessionFactory,
) -> None:
    """The negative control for the previous test: a foreign member with a
    seat elsewhere, but no copilot:manage, still 404s on someone else's
    Assistant session."""
    tenant = uuid.uuid4()
    subject = "seat-only-viewer"
    async with app_session(tenant) as db:
        other_dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(other_dept)
        await db.flush()
        member = m.OrgMember(
            tenant_id=tenant, subject=subject, subject_uuid=subject_uuid_for(subject)
        )
        db.add(member)
        await db.flush()
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=member.id,
                department_id=other_dept.id,
                seat_role="dept_viewer",
            )
        )
        await db.flush()

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assistant_r = await c.get("/api/v1/assistant", headers=_headers(tenant))
            assistant_agent_id = assistant_r.json()["agentId"]
            create_r = await c.post(
                "/api/v1/chat/sessions",
                json={"agentId": assistant_agent_id},
                headers=_headers(tenant),
            )
            session_id = create_r.json()["id"]

            token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role="member")
            r = await c.get(
                f"/api/v1/chat/sessions/{session_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 404, r.text


async def test_assistant_visible_defaults_to_copilot_use(
    app_session: AppSessionFactory,
) -> None:
    """Direct test of `assistant_visible`'s DEFAULT parameter: copilot:use
    alone is necessary but not sufficient -- it must also name the tenant's
    actual Assistant agent, never any other agent, even one the same caller
    could otherwise see."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        other_dept = m.Department(tenant_id=tenant, name="Helpdesk", frame={})
        db.add(other_dept)
        await db.flush()
        other_agent = m.Agent(tenant_id=tenant, department_id=other_dept.id, name="Lennart")
        db.add(other_agent)
        await db.flush()

        no_grant = Authority(tenant_wide=frozenset(), unrestricted=False, decides_everywhere=False)
        assert (
            await assistant_visible(
                db, tenant_id=tenant, authority=no_grant, agent_id=assistant.id
            )
            is False
        )

        with_grant = Authority(
            tenant_wide=frozenset({COPILOT_USE}), unrestricted=False, decides_everywhere=False
        )
        assert (
            await assistant_visible(
                db, tenant_id=tenant, authority=with_grant, agent_id=assistant.id
            )
            is True
        )
        assert (
            await assistant_visible(
                db, tenant_id=tenant, authority=with_grant, agent_id=other_agent.id
            )
            is False
        )


async def test_assistant_visible_with_an_explicit_copilot_manage_requirement(
    app_session: AppSessionFactory,
) -> None:
    """The stricter, explicit-permission call `_owned_session`'s foreign-session
    branch makes: copilot:use ALONE must not satisfy it, only copilot:manage."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)

        use_only = Authority(
            tenant_wide=frozenset({COPILOT_USE}), unrestricted=False, decides_everywhere=False
        )
        assert (
            await assistant_visible(
                db,
                tenant_id=tenant,
                authority=use_only,
                agent_id=assistant.id,
                permission=perm(COPILOT, MANAGE),
            )
            is False
        )

        manage_grant = Authority(
            tenant_wide=frozenset({perm(COPILOT, MANAGE)}),
            unrestricted=False,
            decides_everywhere=False,
        )
        assert (
            await assistant_visible(
                db,
                tenant_id=tenant,
                authority=manage_grant,
                agent_id=assistant.id,
                permission=perm(COPILOT, MANAGE),
            )
            is True
        )


async def test_the_first_turn_of_a_new_session_already_carries_its_task(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """`ChatSession.task_id` used to be written only by `record_assistant_reply`,
    which runs AFTER a run reaches a terminal state -- so for the whole of turn
    one the session had no task_id at all.

    That is not cosmetic: `control_tools._member_may_reach_department` resolves
    the human behind a run by looking the ChatSession up BY `task_id`, finds
    nothing, and fails closed. The Assistant's very first cross-department
    `delegate_task` in every new session was therefore refused with "the person
    you are acting for does not have access to that department" -- which was
    never true.

    Driven through the REAL pipeline (HTTP POST -> send_message -> enqueue_run),
    not a hand-built `ChatSession(task_id=task.id)`: the hand-built one is
    exactly the state this bug meant never existed on turn one.
    """
    from oc8.agent.control_tools import _member_may_reach_department

    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assistant_id = (await c.get("/api/v1/assistant", headers=_headers(tenant))).json()[
                "agentId"
            ]
            create_r = await c.post(
                "/api/v1/chat/sessions",
                json={"agentId": assistant_id},
                headers=_headers(tenant),
            )
            assert create_r.status_code == 201, create_r.text
            session_id = create_r.json()["id"]

            async with app_session(tenant) as db:
                fresh = await db.get(m.ChatSession, uuid.UUID(session_id))
                assert fresh is not None
                assert fresh.task_id is None, "no task before the first message"

            send_r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"message": "Bitte ein Ticket bearbeiten lassen"},
                headers=_headers(tenant),
            )
            assert send_r.status_code == 201, send_r.text
            run_id = send_r.json()["runId"]

    async with app_session(tenant) as db:
        session = await db.get(m.ChatSession, uuid.UUID(session_id))
        assert session is not None
        assert session.task_id is not None, "turn one left the session task-less"

        run = await db.get(m.AgentRun, uuid.UUID(run_id))
        assert run is not None
        assert run.task_id == session.task_id, (
            "the run must carry the task the session owns, not open a second one"
        )
        # The token's role claim travels with the run so the guard can still
        # resolve the acting human's reach mid-run, with no request in sight.
        assert run.context["operator_role"] == "org_admin"

        task = await db.get(m.Task, session.task_id)
        assert task is not None

        # The exact question `_delegate` asks mid-run, on turn one. Before the
        # fix this was False for everybody, always.
        other = m.Department(tenant_id=tenant, name="Buchhaltung", frame={})
        db.add(other)
        await db.flush()
        assert (
            await _member_may_reach_department(
                db, tenant_id=tenant, task=task, department_id=other.id, run_id=run.id
            )
            is True
        )


# --- Rename / delete a chat session ---


async def test_rename_session_persists_the_new_title(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            session_id = (
                await c.post(
                    "/api/v1/chat/sessions",
                    json={"agentId": str(agent_id)},
                    headers=_headers(tenant),
                )
            ).json()["id"]

            r = await c.patch(
                f"/api/v1/chat/sessions/{session_id}",
                json={"title": "VPN Tickets"},
                headers=_headers(tenant),
            )
            assert r.status_code == 200, r.text
            assert r.json()["title"] == "VPN Tickets"

            list_r = await c.get(
                f"/api/v1/chat/sessions?agentId={agent_id}", headers=_headers(tenant)
            )
            renamed = next(s for s in list_r.json() if s["id"] == session_id)
            assert renamed["title"] == "VPN Tickets"


async def test_rename_a_foreign_session_is_refused(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    app = create_app()
    other_token = get_identity_provider().mint(
        tenant_id=tenant, subject="other-operator", role="org_admin"
    )
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            session_id = (
                await c.post(
                    "/api/v1/chat/sessions",
                    json={"agentId": str(agent_id)},
                    headers=_headers(tenant),
                )
            ).json()["id"]

            r = await c.patch(
                f"/api/v1/chat/sessions/{session_id}",
                json={"title": "Hijacked"},
                headers={"Authorization": f"Bearer {other_token}"},
            )
            assert r.status_code == 404, r.text


async def test_delete_session_removes_it_and_its_transcript(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            session_id = (
                await c.post(
                    "/api/v1/chat/sessions",
                    json={"agentId": str(agent_id)},
                    headers=_headers(tenant),
                )
            ).json()["id"]
            await c.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"message": "Hallo!"},
                headers=_headers(tenant),
            )

            r = await c.delete(
                f"/api/v1/chat/sessions/{session_id}", headers=_headers(tenant)
            )
            assert r.status_code == 204, r.text

            list_r = await c.get(
                f"/api/v1/chat/sessions?agentId={agent_id}", headers=_headers(tenant)
            )
            assert session_id not in [s["id"] for s in list_r.json()]

            messages_r = await c.get(
                f"/api/v1/chat/sessions/{session_id}/messages", headers=_headers(tenant)
            )
            assert messages_r.status_code == 404, messages_r.text

    async with app_session(tenant) as db:
        orphans = (
            await db.execute(
                select(m.ChatMessage).where(m.ChatMessage.session_id == uuid.UUID(session_id))
            )
        ).scalars().all()
        assert orphans == []


async def test_delete_a_foreign_session_is_refused(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    app = create_app()
    other_token = get_identity_provider().mint(
        tenant_id=tenant, subject="other-operator", role="org_admin"
    )
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            session_id = (
                await c.post(
                    "/api/v1/chat/sessions",
                    json={"agentId": str(agent_id)},
                    headers=_headers(tenant),
                )
            ).json()["id"]

            r = await c.delete(
                f"/api/v1/chat/sessions/{session_id}",
                headers={"Authorization": f"Bearer {other_token}"},
            )
            assert r.status_code == 404, r.text

    async with app_session(tenant) as db:
        still_there = await db.get(m.ChatSession, uuid.UUID(session_id))
        assert still_there is not None


async def test_a_second_turn_reuses_the_task_the_first_one_opened(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """One chat session is one item on the department board, not one per
    message -- the property `send_message` opening the task up front must not
    break."""
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            session_id = (
                await c.post(
                    "/api/v1/chat/sessions",
                    json={"agentId": str(agent_id)},
                    headers=_headers(tenant),
                )
            ).json()["id"]
            for text in ("Erste Frage", "Zweite Frage"):
                r = await c.post(
                    f"/api/v1/chat/sessions/{session_id}/messages",
                    json={"message": text},
                    headers=_headers(tenant),
                )
                assert r.status_code == 201, r.text

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
        assert len(runs) == 2
        assert len({r.task_id for r in runs}) == 1, "both turns share one Task"
        tasks = (
            (await db.execute(select(m.Task).where(m.Task.tenant_id == tenant))).scalars().all()
        )
        assert len(tasks) == 1


# --- Task 6: attachment_ids wiring (oc8.api.v1.files, chat/service.py) ---


async def test_send_message_with_a_text_attachment_appends_extracted_text_and_repoints_it(
    app_session: AppSessionFactory, redis_url: str, minio_url: str
) -> None:
    """A file uploaded before the message is sent is owned by the
    `ChatSession` (`owner_id == session.id`); sending the message that
    references it must both (a) fold its extracted text into the run's
    `task`, labelled with its filename, and (b) re-point `owner_id` to the
    new `ChatMessage`, matching `chat/service.py::send_message`'s docstring."""
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            session_id = (
                await c.post(
                    "/api/v1/chat/sessions",
                    json={"agentId": str(agent_id)},
                    headers=_headers(tenant),
                )
            ).json()["id"]

            upload_r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/attachments",
                files={"file": ("notes.txt", b"the quarterly numbers", "text/plain")},
                headers=_headers(tenant),
            )
            assert upload_r.status_code == 201, upload_r.text
            attachment_id = upload_r.json()["id"]

            send_r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"message": "see attached", "attachmentIds": [attachment_id]},
                headers=_headers(tenant),
            )
            assert send_r.status_code == 201, send_r.text
            user_message_id = send_r.json()["id"]
            run_id = send_r.json()["runId"]

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, uuid.UUID(run_id))
        assert run is not None
        assert "[Attached file: notes.txt]" in run.context["task"]
        assert "the quarterly numbers" in run.context["task"]
        assert "task_images" not in run.context

        att = await db.get(m.FileAttachment, uuid.UUID(attachment_id))
        assert att is not None
        assert att.owner_type == "chat_message"
        assert str(att.owner_id) == user_message_id


async def test_send_message_with_an_image_attachment_populates_task_images(
    app_session: AppSessionFactory, redis_url: str, minio_url: str
) -> None:
    """An image attachment must not be inlined into `task` text (no
    extractable text exists for it) -- instead it lands in
    `context["task_images"]` as a `{bucket_key, content_type}` pointer, left
    for the run-preamble step (Task 7) to fetch the actual bytes."""
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            session_id = (
                await c.post(
                    "/api/v1/chat/sessions",
                    json={"agentId": str(agent_id)},
                    headers=_headers(tenant),
                )
            ).json()["id"]

            upload_r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/attachments",
                files={"file": ("photo.png", b"\x89PNG\r\n\x1a\n" + b"fake", "image/png")},
                headers=_headers(tenant),
            )
            assert upload_r.status_code == 201, upload_r.text
            attachment_id = upload_r.json()["id"]
            assert upload_r.json()["isImage"] is True

            send_r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"message": "what's this?", "attachmentIds": [attachment_id]},
                headers=_headers(tenant),
            )
            assert send_r.status_code == 201, send_r.text
            run_id = send_r.json()["runId"]

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, uuid.UUID(run_id))
        assert run is not None
        assert "[Attached file:" not in run.context["task"]

        att = await db.get(m.FileAttachment, uuid.UUID(attachment_id))
        assert att is not None
        assert run.context["task_images"] == [
            {"bucket_key": att.bucket_key, "content_type": "image/png"}
        ]


async def test_send_message_ignores_an_attachment_from_another_session(
    app_session: AppSessionFactory, redis_url: str, minio_url: str
) -> None:
    """The re-point is scoped to THIS session's own pending uploads, not just
    to the tenant: re-pointing is the one act that makes an attachment
    readable through `files.py`'s `_owned_attachment` (which resolves a sent
    attachment through its message's session), so an id naming someone
    else's upload must simply not be found."""
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            sessions = [
                (
                    await c.post(
                        "/api/v1/chat/sessions",
                        json={"agentId": str(agent_id)},
                        headers=_headers(tenant),
                    )
                ).json()["id"]
                for _ in range(2)
            ]
            other_attachment_id = (
                await c.post(
                    f"/api/v1/chat/sessions/{sessions[1]}/attachments",
                    files={"file": ("theirs.txt", b"not yours", "text/plain")},
                    headers=_headers(tenant),
                )
            ).json()["id"]

            send_r = await c.post(
                f"/api/v1/chat/sessions/{sessions[0]}/messages",
                json={"message": "see attached", "attachmentIds": [other_attachment_id]},
                headers=_headers(tenant),
            )
            assert send_r.status_code == 201, send_r.text
            run_id = send_r.json()["runId"]

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, uuid.UUID(run_id))
        assert run is not None
        assert "not yours" not in run.context["task"]
        att = await db.get(m.FileAttachment, uuid.UUID(other_attachment_id))
        assert att is not None
        assert str(att.owner_id) == sessions[1]  # untouched, still the other session's


async def test_deleting_a_session_deletes_its_attachments_and_their_blobs(
    app_session: AppSessionFactory, redis_url: str, minio_url: str
) -> None:
    """The design doc: a deleted session's attachments are cleaned up "the
    same way (DB row + object-store blob)". Covers both shapes -- one sent
    (owner_id is a ChatMessage) and one uploaded but never sent (owner_id is
    still the ChatSession)."""
    from oc8.storage import s3

    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            session_id = (
                await c.post(
                    "/api/v1/chat/sessions",
                    json={"agentId": str(agent_id)},
                    headers=_headers(tenant),
                )
            ).json()["id"]

            sent_id = (
                await c.post(
                    f"/api/v1/chat/sessions/{session_id}/attachments",
                    files={"file": ("sent.txt", b"sent", "text/plain")},
                    headers=_headers(tenant),
                )
            ).json()["id"]
            unsent_id = (
                await c.post(
                    f"/api/v1/chat/sessions/{session_id}/attachments",
                    files={"file": ("unsent.txt", b"unsent", "text/plain")},
                    headers=_headers(tenant),
                )
            ).json()["id"]
            send_r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"message": "see attached", "attachmentIds": [sent_id]},
                headers=_headers(tenant),
            )
            assert send_r.status_code == 201, send_r.text

            async with app_session(tenant) as db:
                keys = [
                    (await db.get(m.FileAttachment, uuid.UUID(i))).bucket_key  # type: ignore[union-attr]
                    for i in (sent_id, unsent_id)
                ]
            for key in keys:
                assert await s3.object_exists(key)

            del_r = await c.delete(f"/api/v1/chat/sessions/{session_id}", headers=_headers(tenant))
            assert del_r.status_code == 204, del_r.text

    async with app_session(tenant) as db:
        for i in (sent_id, unsent_id):
            assert await db.get(m.FileAttachment, uuid.UUID(i)) is None
    for key in keys:
        assert not await s3.object_exists(key)


# --- Task 12: GET /chat/sessions/{session_id}/runs/{run_id} ---


async def test_get_session_run_returns_the_owners_own_run(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """The owner reads their own run."""
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            create_r = await c.post(
                "/api/v1/chat/sessions",
                json={"agentId": str(agent_id)},
                headers=_headers(tenant),
            )
            session_id = create_r.json()["id"]

            send_r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"message": "Hallo!"},
                headers=_headers(tenant),
            )
            assert send_r.status_code == 201, send_r.text

    async with app_session(tenant) as db:
        run = (
            await db.execute(
                select(m.AgentRun).where(
                    m.AgentRun.agent_id == agent_id, m.AgentRun.source == "chat"
                )
            )
        ).scalar_one()

    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            get_r = await c.get(
                f"/api/v1/chat/sessions/{session_id}/runs/{run.id}",
                headers=_headers(tenant),
            )
            assert get_r.status_code == 200, get_r.text
            assert get_r.json()["id"] == str(run.id)


async def test_get_session_run_lets_an_oversight_admin_read_a_colleagues_assistant_run(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """copilot:manage oversight carve-out, same as get_messages."""
    tenant = uuid.uuid4()
    app = create_app()
    other_token = get_identity_provider().mint(
        tenant_id=tenant, subject="other-operator", role="org_admin"
    )
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assistant_r = await c.get("/api/v1/assistant", headers=_headers(tenant))
            assistant_agent_id = assistant_r.json()["agentId"]

            create_r = await c.post(
                "/api/v1/chat/sessions",
                json={"agentId": assistant_agent_id},
                headers=_headers(tenant),
            )
            session_id = create_r.json()["id"]

            send_r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"message": "Hallo!"},
                headers=_headers(tenant),
            )
            assert send_r.status_code == 201, send_r.text
            run_id = send_r.json()["runId"]

            get_r = await c.get(
                f"/api/v1/chat/sessions/{session_id}/runs/{run_id}",
                headers={"Authorization": f"Bearer {other_token}"},
            )
            assert get_r.status_code == 200, get_r.text
            assert get_r.json()["id"] == run_id


async def test_get_session_run_404s_for_a_run_belonging_to_a_different_session(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Even a session the caller DOES own does not unlock a run belonging to
    a different session -- proving the context["chat_session_id"] check,
    not just the ownership check, is load-bearing."""
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            session_a_id = (
                await c.post(
                    "/api/v1/chat/sessions",
                    json={"agentId": str(agent_id)},
                    headers=_headers(tenant),
                )
            ).json()["id"]
            session_b_id = (
                await c.post(
                    "/api/v1/chat/sessions",
                    json={"agentId": str(agent_id)},
                    headers=_headers(tenant),
                )
            ).json()["id"]

            send_r = await c.post(
                f"/api/v1/chat/sessions/{session_a_id}/messages",
                json={"message": "Hallo!"},
                headers=_headers(tenant),
            )
            assert send_r.status_code == 201, send_r.text
            run_a_id = send_r.json()["runId"]

            get_r = await c.get(
                f"/api/v1/chat/sessions/{session_b_id}/runs/{run_a_id}",
                headers=_headers(tenant),
            )
            assert get_r.status_code == 404, get_r.text


async def test_get_session_run_404s_for_a_nonexistent_run_id(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            session_id = (
                await c.post(
                    "/api/v1/chat/sessions",
                    json={"agentId": str(agent_id)},
                    headers=_headers(tenant),
                )
            ).json()["id"]

            get_r = await c.get(
                f"/api/v1/chat/sessions/{session_id}/runs/{uuid.uuid4()}",
                headers=_headers(tenant),
            )
            assert get_r.status_code == 404, get_r.text
