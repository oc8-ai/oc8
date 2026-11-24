"""What a reader who is not the owner sees of a personal agent: metadata only.

Spec decision 3: a tenant-wide `department:manage` holder (org_admin,
dept_manager) reaches a personal department's agent by id, but never its
content. `GET /agents/{id}` answers with the agent's metadata and blank content
fields; the sub-resources that are nothing but content (instruction history,
versions, instruction files, triggers) answer 404, as if the agent did not
exist. The owner's seat sees everything, and a standard agent is unchanged.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.permissions import SEAT_APPROVER
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_MISSION = "Answer my private mail in my voice."
_PRESENTATION = {
    "last_action": "Drafted a reply to the landlord",
    "last_run": "2 min ago",
    "prompt_starters": ["Summarise my inbox"],
    "guardrails": ["Never mention my divorce"],
    "tools": ["mail"],
    "i18n": {
        "de": {
            "mission": "Beantworte meine private Post.",
            "last_action": "Antwort an den Vermieter entworfen",
            "last_run": "vor 2 Min.",
            "guardrails": ["Nie die Scheidung erwähnen"],
        }
    },
}
_TASK = "Every morning, read my private inbox"

# Sub-resources that are nothing but the agent's content.
_CONTENT_PATHS = (
    "instructions/history",
    "versions",
    "versions/1",
    "versions/diff?from=1",
    "instruction-files",
    "triggers",
)


@dataclass(frozen=True)
class _World:
    tenant: uuid.UUID
    personal_agent: uuid.UUID
    standard_agent: uuid.UUID


def _subject_uuid(subject: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"oc8:subject:{subject}")


def _headers(tenant: uuid.UUID, subject: str, role: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _world(app_session: AppSessionFactory) -> _World:
    """`owner`'s personal department with agent Pia (mission, presentation,
    an instruction file, a trigger), and a standard "Vertrieb" with agent Nora
    carrying the same content."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        members = {}
        for subject in ("owner", "admin", "manager"):
            member = m.OrgMember(
                tenant_id=tenant,
                subject=subject,
                subject_uuid=_subject_uuid(subject),
                display_name=subject.title(),
            )
            db.add(member)
            members[subject] = member
        shared = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(shared)
        await db.flush()
        personal = m.Department(
            tenant_id=tenant,
            name="Owner",
            frame={"closed": True},
            kind="personal",
            owner_member_id=members["owner"].id,
        )
        db.add(personal)
        await db.flush()
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=members["owner"].id,
                department_id=personal.id,
                seat_role=SEAT_APPROVER,
                agent_manage=True,
            )
        )
        pia = m.Agent(
            tenant_id=tenant,
            department_id=personal.id,
            name="Pia",
            role_title="Mail assistant",
            mission=_MISSION,
            presentation=_PRESENTATION,
        )
        nora = m.Agent(
            tenant_id=tenant,
            department_id=shared.id,
            name="Nora",
            role_title="Sales",
            mission=_MISSION,
            presentation=_PRESENTATION,
        )
        db.add_all([pia, nora])
        await db.flush()
        db.add(
            m.FileAttachment(
                tenant_id=tenant,
                owner_type="agent_instructions",
                owner_id=pia.id,
                filename="letters.txt",
                content_type="text/plain",
                size_bytes=1,
                bucket_key=f"{tenant}/letters.txt",
            )
        )
        db.add(
            m.Trigger(
                tenant_id=tenant,
                agent_id=pia.id,
                kind="cron",
                cron_expression="0 9 * * *",
                task_text=_TASK,
            )
        )
        await db.flush()
        return _World(tenant=tenant, personal_agent=pia.id, standard_agent=nora.id)


def _assert_content_blank(body: dict[str, object]) -> None:
    assert body["contentHidden"] is True
    assert body["mission"] == ""
    assert body["missionTranslations"] == {}
    assert body["lastAction"] == ""
    assert body["lastActionTranslations"] == {}
    assert body["lastRun"] == ""
    assert body["lastRunTranslations"] == {}
    assert body["promptStarters"] == []
    assert body["guardrails"] == []
    assert body["guardrailsTranslations"] == {}


async def test_the_owner_sees_the_content_of_their_agent(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    h = _headers(w.tenant, "owner", "member")
    async with _http() as http:
        r = await http.get(f"/api/v1/agents/{w.personal_agent}", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["contentHidden"] is False
        assert body["mission"] == _MISSION
        assert body["lastAction"] == _PRESENTATION["last_action"]
        assert body["promptStarters"] == ["Summarise my inbox"]
        assert body["guardrails"] == ["Never mention my divorce"]
        assert body["missionTranslations"] == {"de": "Beantworte meine private Post."}

        files = await http.get(f"/api/v1/agents/{w.personal_agent}/instruction-files", headers=h)
        assert files.status_code == 200, files.text
        assert [f["filename"] for f in files.json()] == ["letters.txt"]


@pytest.mark.parametrize(("subject", "role"), [("admin", "org_admin"), ("manager", "dept_manager")])
async def test_a_manager_reads_the_metadata_but_never_the_content(
    app_session: AppSessionFactory, subject: str, role: str
) -> None:
    w = await _world(app_session)
    async with _http() as http:
        r = await http.get(
            f"/api/v1/agents/{w.personal_agent}", headers=_headers(w.tenant, subject, role)
        )
    assert r.status_code == 200, r.text
    body = r.json()
    _assert_content_blank(body)
    assert _MISSION not in r.text
    assert "landlord" not in r.text
    assert "divorce" not in r.text.lower()
    # The metadata stays.
    assert body["id"] == str(w.personal_agent)
    assert body["name"] == "Pia"
    assert body["role"] == "Mail assistant"
    assert body["tools"] == ["mail"]
    assert body["departmentName"] == "Owner"
    assert "autonomy" in body
    assert "effectiveTools" in body


@pytest.mark.parametrize("path", _CONTENT_PATHS)
async def test_an_admin_gets_a_404_on_the_content_sub_resources(
    app_session: AppSessionFactory, path: str
) -> None:
    w = await _world(app_session)
    async with _http() as http:
        r = await http.get(
            f"/api/v1/agents/{w.personal_agent}/{path}",
            headers=_headers(w.tenant, "admin", "org_admin"),
        )
    assert r.status_code == 404, r.text
    assert _TASK not in r.text
    assert "letters.txt" not in r.text


@pytest.mark.parametrize("path", ["instructions/history", "instruction-files"])
async def test_the_owner_still_reaches_the_content_sub_resources(
    app_session: AppSessionFactory, path: str
) -> None:
    """Only the seat-gated reads: `versions` needs `agent_version:view` and
    `triggers` needs `trigger:view`, neither of which a seat (and so a plain
    member owner) carries -- those answer 403 for a reason unrelated to this."""
    w = await _world(app_session)
    async with _http() as http:
        r = await http.get(
            f"/api/v1/agents/{w.personal_agent}/{path}",
            headers=_headers(w.tenant, "owner", "member"),
        )
    assert r.status_code == 200, r.text


async def test_a_standard_agent_is_unchanged_for_an_admin(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    h = _headers(w.tenant, "admin", "org_admin")
    async with _http() as http:
        r = await http.get(f"/api/v1/agents/{w.standard_agent}", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["contentHidden"] is False
        assert body["mission"] == _MISSION
        assert body["lastAction"] == _PRESENTATION["last_action"]
        assert body["guardrails"] == ["Never mention my divorce"]
        for path in ("instructions/history", "versions", "instruction-files", "triggers"):
            sub = await http.get(f"/api/v1/agents/{w.standard_agent}/{path}", headers=h)
            assert sub.status_code == 200, (path, sub.text)
