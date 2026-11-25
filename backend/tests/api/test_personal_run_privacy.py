"""A4: what a personal agent did is readable only by its owner.

The run reach (`agents.repo.run_reach`) used to hand every tenant-wide
`agent:view` + `run:view` reader EVERY agent's runs, a personal department's
included. Now it carries the departments that are private to the caller
(`departments.repo.is_private_to`) and every run reader leaves them out --
the every-agent branch too. Metadata stays (R2): usage and cost still count
the personal run. An admin may still stop one (U1), seeing nothing of it.

The world: `owner` owns a personal department with agent Pia, which finished
one run with output, an activity row and a run file; a standard "Vertrieb"
with agent Nora has a run too. `admin` is an `org_admin`, every-agent reach.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.permissions import SEAT_APPROVER, SEAT_VIEWER
from oc8.copilot.profile import offboard_member
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_SECRET_OUTPUT = "the owner's private answer"
_SECRET_TASK = "read my private mail"
_SECRET_ARG = "private-argument-value"


@dataclass(frozen=True)
class _World:
    tenant: uuid.UUID
    owner_member: uuid.UUID
    personal: uuid.UUID
    pia: uuid.UUID
    nora: uuid.UUID
    pia_run: uuid.UUID
    pia_running: uuid.UUID
    nora_run: uuid.UUID


def _subject_uuid(subject: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"oc8:subject:{subject}")


def _headers(tenant: uuid.UUID, subject: str, role: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


def _owner(w: _World) -> dict[str, str]:
    return _headers(w.tenant, "owner", "member")


def _admin(w: _World) -> dict[str, str]:
    return _headers(w.tenant, "admin", "org_admin")


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _world(app_session: AppSessionFactory) -> _World:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(m.Organization(id=tenant, slug=f"t-{tenant.hex[:8]}", name="T", settings={}))
        shared = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(shared)
        members: dict[str, m.OrgMember] = {}
        for subject in ("owner", "admin"):
            member = m.OrgMember(
                tenant_id=tenant,
                subject=subject,
                subject_uuid=_subject_uuid(subject),
                display_name=subject.title(),
            )
            db.add(member)
            members[subject] = member
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
        db.add_all(
            [
                m.OrgMemberDepartment(
                    tenant_id=tenant,
                    member_id=members["owner"].id,
                    department_id=personal.id,
                    seat_role=SEAT_APPROVER,
                    agent_manage=True,
                ),
                m.OrgMemberDepartment(
                    tenant_id=tenant,
                    member_id=members["admin"].id,
                    department_id=shared.id,
                    seat_role=SEAT_VIEWER,
                ),
            ]
        )
        nora = m.Agent(tenant_id=tenant, department_id=shared.id, name="Nora")
        pia = m.Agent(tenant_id=tenant, department_id=personal.id, name="Pia")
        db.add_all([nora, pia])
        await db.flush()
        content = {
            "task": _SECRET_TASK,
            "output": _SECRET_OUTPUT,
            "toolCalls": [{"name": "search", "arguments": {"q": _SECRET_ARG}}],
        }
        pia_run = m.AgentRun(tenant_id=tenant, agent_id=pia.id, state="done", context=content)
        pia_running = m.AgentRun(
            tenant_id=tenant, agent_id=pia.id, state="running", context=dict(content)
        )
        nora_run = m.AgentRun(
            tenant_id=tenant, agent_id=nora.id, state="done", context={"output": "public"}
        )
        db.add_all([pia_run, pia_running, nora_run])
        await db.flush()
        db.add_all(
            [
                m.ActivityEvent(
                    tenant_id=tenant, agent_id=pia.id, status="info", message=_SECRET_OUTPUT
                ),
                m.ActivityEvent(
                    tenant_id=tenant, agent_id=nora.id, status="info", message="nora did it"
                ),
                m.FileAttachment(
                    tenant_id=tenant,
                    owner_type="agent_run",
                    owner_id=pia_run.id,
                    bucket_key=f"{tenant}/runs/{pia_run.id}/report.md",
                    filename="report.md",
                    content_type="text/markdown",
                    size_bytes=12,
                ),
                m.TokenUsageRecord(
                    tenant_id=tenant,
                    agent_id=pia.id,
                    department_id=personal.id,
                    model="m",
                    provider="p",
                    tokens_in=111,
                    tokens_out=222,
                    request_id=uuid.uuid4(),
                ),
            ]
        )
        await db.flush()
        return _World(
            tenant=tenant,
            owner_member=members["owner"].id,
            personal=personal.id,
            pia=pia.id,
            nora=nora.id,
            pia_run=pia_run.id,
            pia_running=pia_running.id,
            nora_run=nora_run.id,
        )


async def test_admin_gets_404_on_a_personal_run(app_session: AppSessionFactory) -> None:
    w = await _world(app_session)
    async with _http() as http:
        admin = await http.get(f"/api/v1/runs/{w.pia_run}", headers=_admin(w))
        assert admin.status_code == 404, admin.text
        assert _SECRET_OUTPUT not in admin.text

        owner = await http.get(f"/api/v1/runs/{w.pia_run}", headers=_owner(w))
        assert owner.status_code == 200, owner.text
        assert owner.json()["output"] == _SECRET_OUTPUT


async def test_admin_runs_list_leaves_out_personal_runs(app_session: AppSessionFactory) -> None:
    w = await _world(app_session)
    async with _http() as http:
        admin = await http.get("/api/v1/runs", params={"limit": 20}, headers=_admin(w))
        assert admin.status_code == 200, admin.text
        admin_ids = {r["id"] for r in admin.json()}
        assert str(w.nora_run) in admin_ids, "a standard department's run stays listed"
        assert str(w.pia_run) not in admin_ids
        assert str(w.pia_running) not in admin_ids

        owner = await http.get("/api/v1/runs", params={"limit": 20}, headers=_owner(w))
        assert owner.status_code == 200, owner.text
        assert str(w.pia_run) in {r["id"] for r in owner.json()}


async def test_admin_history_tab_is_empty_for_a_personal_agent(
    app_session: AppSessionFactory,
) -> None:
    """Ruling P1: the run list is content (task text, output), so a non-owner
    gets the same 404 as an agent they cannot see."""
    w = await _world(app_session)
    async with _http() as http:
        admin = await http.get(f"/api/v1/agents/{w.pia}/runs", headers=_admin(w))
        assert admin.status_code == 404, admin.text

        owner = await http.get(f"/api/v1/agents/{w.pia}/runs", headers=_owner(w))
        assert owner.status_code == 200, owner.text
        assert str(w.pia_run) in {r["id"] for r in owner.json()["items"]}


async def test_admin_cannot_list_personal_workspace_files(app_session: AppSessionFactory) -> None:
    w = await _world(app_session)
    async with _http() as http:
        admin = await http.get(f"/api/v1/agents/{w.pia}/workspace/files", headers=_admin(w))
        assert admin.status_code == 404, admin.text

        owner = await http.get(f"/api/v1/agents/{w.pia}/workspace/files", headers=_owner(w))
        assert owner.status_code == 200, owner.text
        assert [f["filename"] for f in owner.json()["files"]] == ["report.md"]


async def test_admin_activity_feed_leaves_out_personal_agents(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    async with _http() as http:
        admin = await http.get("/api/v1/activity", headers=_admin(w))
        assert admin.status_code == 200, admin.text
        agents = {row["agentId"] for row in admin.json()}
        assert str(w.nora) in agents, "a standard agent's activity stays in the feed"
        assert str(w.pia) not in agents
        assert _SECRET_OUTPUT not in admin.text

        scoped = await http.get(
            "/api/v1/activity", params={"agentId": str(w.pia)}, headers=_admin(w)
        )
        assert scoped.status_code == 200, scoped.text
        assert scoped.json() == []

        owner = await http.get(
            "/api/v1/activity", params={"agentId": str(w.pia)}, headers=_owner(w)
        )
        assert owner.status_code == 200, owner.text
        assert [row["message"] for row in owner.json()] == [_SECRET_OUTPUT]


async def test_usage_still_counts_personal_runs(app_session: AppSessionFactory) -> None:
    """R2: cost and usage are metadata an admin keeps."""
    w = await _world(app_session)
    async with _http() as http:
        r = await http.get(
            "/api/v1/usage", params={"group_by": "agent", "period": "all"}, headers=_admin(w)
        )
    assert r.status_code == 200, r.text
    rows = {row["group"]: row for row in r.json()}
    assert str(w.pia) in rows, r.text
    assert rows[str(w.pia)]["tokensIn"] == 111
    assert rows[str(w.pia)]["tokensOut"] == 222
    assert "providerCostMicros" in rows[str(w.pia)]


async def test_admin_may_cancel_a_personal_run(app_session: AppSessionFactory) -> None:
    """U1: the emergency stop stays an admin's, and reveals nothing: neither
    the response nor the audit entry carries output, task text or arguments."""
    w = await _world(app_session)
    async with _http() as http:
        r = await http.post(f"/api/v1/runs/{w.pia_running}/cancel", headers=_admin(w))
    assert r.status_code == 200, r.text
    assert r.json()["id"] == str(w.pia_running)
    assert r.json()["state"] == "running"
    for secret in (_SECRET_OUTPUT, _SECRET_TASK, _SECRET_ARG):
        assert secret not in r.text

    async with app_session(w.tenant) as db:
        admin_member = (
            await db.execute(select(m.OrgMember.id).where(m.OrgMember.subject == "admin"))
        ).scalar_one()
        cancel = (
            await db.execute(
                select(m.RunCancellation).where(m.RunCancellation.run_id == w.pia_running)
            )
        ).scalar_one_or_none()
        assert cancel is not None
        audit = (
            (
                await db.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.tenant_id == w.tenant, m.AuditEvent.action == "run.cancel"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(audit) == 1
    entry = audit[0]
    assert entry.actor_id == admin_member
    assert entry.resource["run_id"] == str(w.pia_running)
    recorded = f"{entry.resource} {entry.reason}"
    for secret in (_SECRET_OUTPUT, _SECRET_TASK, _SECRET_ARG):
        assert secret not in recorded


async def test_a_departed_owners_runs_stay_private(app_session: AppSessionFactory) -> None:
    """R1 fails closed: a personal department whose owner has left is private
    to everybody, and the run is kept, not deleted."""
    w = await _world(app_session)
    async with app_session(w.tenant) as db:
        owner = await db.get(m.OrgMember, w.owner_member)
        assert owner is not None
        owner.deleted_at = dt.datetime.now(dt.UTC)
        await offboard_member(db, tenant_id=w.tenant, member_id=w.owner_member)
    async with _http() as http:
        r = await http.get(f"/api/v1/runs/{w.pia_run}", headers=_admin(w))
        assert r.status_code == 404, r.text
        listed = await http.get("/api/v1/runs", params={"limit": 20}, headers=_admin(w))
        assert str(w.pia_run) not in {row["id"] for row in listed.json()}
    async with app_session(w.tenant) as db:
        assert await db.get(m.AgentRun, w.pia_run) is not None
