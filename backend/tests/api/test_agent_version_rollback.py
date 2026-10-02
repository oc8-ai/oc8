"""`POST /agents/{id}/versions/{version_no}/rollback`.

Spec decision 3, which deviates from the Skill precedent on purpose: this does
NOT repoint `current_version_id` at an old row, it creates a NEW version
carrying the old payload. Every assertion about "a v5 now exists" is therefore
load-bearing -- a future refactor that "simplified" this into a repoint would
pass a test that only checked the agent's mission.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.publish_hooks import (
    _reset_publish_hooks_for_tests,
    register_publish_hook,
)
from oc8.auth import get_identity_provider
from oc8.constants import ACME_TENANT_ID
from oc8.credentials.service import create_credential
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _token(tenant: uuid.UUID, role: str = "org_admin") -> str:
    return get_identity_provider().mint(tenant_id=tenant, subject="u", role=role)


@pytest.fixture(autouse=True)
def _clean_hooks() -> Iterator[None]:
    """Empty on BOTH sides: the registry is a process-global, and a hook left
    registered here would veto every `POST /agents` in the next test module."""
    _reset_publish_hooks_for_tests()
    yield
    _reset_publish_hooks_for_tests()


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    """`create_credential` (the subscription-model test) envelope-encrypts."""
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


async def _hire(client: AsyncClient, headers: dict[str, str]) -> str:
    dept = await client.post(
        "/api/v1/departments",
        json={"name": f"D-{uuid.uuid4().hex}", "icon": "Bot"},
        headers=headers,
    )
    assert dept.status_code in (200, 201), dept.text
    created = await client.post(
        "/api/v1/agents",
        json={
            "departmentId": dept.json()["id"],
            "name": f"A-{uuid.uuid4().hex[:8]}",
            "roleTitle": "Tester",
            "mission": "v1",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def _publish_missions(
    client: AsyncClient, headers: dict[str, str], agent_id: str, missions: list[str]
) -> None:
    for index, mission in enumerate(missions, start=1):
        patched = await client.patch(
            f"/api/v1/agents/{agent_id}/instructions",
            json={"instructions": mission},
            headers=headers,
        )
        assert patched.status_code == 200, patched.text
        published = await client.post(
            f"/api/v1/agents/{agent_id}/versions",
            json={"expectedCurrentVersionNo": index},
            headers=headers,
        )
        assert published.status_code == 201, published.text


async def test_rolling_back_creates_a_new_version_rather_than_repointing(
    app_session: AppSessionFactory,
) -> None:
    """Spec decision 3. v1..v4 exist; rolling back to v1 must produce a v5 whose
    payload equals v1's, with `current_version_id` on v5 -- NOT on v1."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            agent_id = await _hire(client, headers)
            await _publish_missions(client, headers, agent_id, ["v2", "v3", "v4"])

            v1 = await client.get(f"/api/v1/agents/{agent_id}/versions/1", headers=headers)
            assert v1.status_code == 200, v1.text

            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions/1/rollback", headers=headers
            )
            assert res.status_code == 201, res.text
            body = res.json()
            assert body["versionNo"] == 5
            assert body["isCurrent"] is True
            assert body["rolledBackFrom"] == 1
            assert body["note"] == "Rollback to v1"
            assert body["payload"] == v1.json()["payload"]

            listed = await client.get(f"/api/v1/agents/{agent_id}/versions", headers=headers)
            rows = listed.json()["items"]
            assert [row["versionNo"] for row in rows] == [5, 4, 3, 2, 1]
            # v1 is NOT current -- the whole point of not repointing.
            assert [row["isCurrent"] for row in rows] == [
                True,
                False,
                False,
                False,
                False,
            ]

            # And the working copy now matches the rolled-back configuration.
            agent = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
            assert agent.json()["mission"] == "v1"
            status_ = await client.get(f"/api/v1/agents/{agent_id}/draft-status", headers=headers)
            assert status_.json()["dirty"] is False
            assert status_.json()["currentVersionNo"] == 5


async def test_rolling_back_re_enters_the_publish_gate(
    app_session: AppSessionFactory,
) -> None:
    """The second reason spec §2.7 refuses a repoint: a rollback must re-run the
    compliance and eval hooks. A repoint would have skipped them, which is how a
    tenant would roll back to a configuration its own policy now forbids."""
    seen: list[int] = []

    async def record(_db: AsyncSession, version: m.AgentVersion) -> None:
        seen.append(int(version.version_no))

    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            agent_id = await _hire(client, headers)
            await _publish_missions(client, headers, agent_id, ["v2"])
            register_publish_hook("record", record)
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions/1/rollback", headers=headers
            )
            assert res.status_code == 201, res.text
    assert seen == [3]


async def test_a_refusing_hook_blocks_the_rollback(
    app_session: AppSessionFactory,
) -> None:
    async def refuse(_db: object, _version: object) -> None:
        raise RuntimeError("policy forbids that configuration")

    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            agent_id = await _hire(client, headers)
            await _publish_missions(client, headers, agent_id, ["v2"])
            register_publish_hook("refuse", refuse)
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions/1/rollback", headers=headers
            )
            assert res.status_code == 422, res.text
            assert res.json()["detail"]["error"] == "publish_hook_rejected"
            _reset_publish_hooks_for_tests()
            # The rollback left nothing behind: still v2, still mission "v2".
            agent = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
            assert agent.json()["mission"] == "v2"
            status_ = await client.get(f"/api/v1/agents/{agent_id}/draft-status", headers=headers)
            assert status_.json()["currentVersionNo"] == 2


async def test_rolling_back_to_the_current_version_is_a_no_op_409(
    app_session: AppSessionFactory,
) -> None:
    """Falls out of `publish_version`'s own hash check, and is the right answer:
    "roll back to what is already running" is always a mistake, and a version
    whose payload equals its predecessor's would trigger a pointless compliance
    re-check."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            agent_id = await _hire(client, headers)
            await _publish_missions(client, headers, agent_id, ["v2"])
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions/2/rollback", headers=headers
            )
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["error"] == "no_changes_to_publish"


async def test_a_rollback_that_would_exceed_todays_frame_is_refused(
    app_session: AppSessionFactory,
) -> None:
    """Not in the spec, ruled in this plan. Every other writer of
    `agent.narrowing` validates against the department frame first; a rollback
    that skipped it would be the one door that stores an out-of-frame narrowing.
    """
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        department = m.Department(
            tenant_id=tenant,
            name=f"D-{uuid.uuid4().hex}",
            frame={"tools": {"odoo": {"enabled": True, "read": True, "modify": True}}},
        )
        db.add(department)
        await db.flush()
        department_id = department.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            created = await client.post(
                "/api/v1/agents",
                json={
                    "departmentId": str(department_id),
                    "name": f"A-{uuid.uuid4().hex[:8]}",
                    "roleTitle": "Tester",
                    "mission": "m",
                    "narrowing": {
                        "tools": {"odoo": {"enabled": True, "read": True, "modify": True}}
                    },
                },
                headers=headers,
            )
            assert created.status_code == 201, created.text
            agent_id = str(created.json()["id"])

            # Narrow the agent, publish v2, then TIGHTEN the frame so v1's
            # narrowing no longer fits inside it.
            narrowed = await client.put(
                f"/api/v1/agents/{agent_id}/narrowing",
                json={
                    "narrowing": {
                        "tools": {"odoo": {"enabled": True, "read": True, "modify": False}}
                    }
                },
                headers=headers,
            )
            assert narrowed.status_code == 200, narrowed.text
            published = await client.post(
                f"/api/v1/agents/{agent_id}/versions",
                json={"expectedCurrentVersionNo": 1},
                headers=headers,
            )
            assert published.status_code == 201, published.text

    async with app_session(tenant) as db:
        dept = await db.get(m.Department, department_id)
        assert dept is not None
        dept.frame = {"tools": {"odoo": {"enabled": True, "read": True, "modify": False}}}

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions/1/rollback", headers=headers
            )
    assert res.status_code == 422, res.text
    assert res.json()["detail"]["error"] == "narrowing_exceeds_frame"


async def test_rolling_back_appends_its_own_audit_event(
    app_session: AppSessionFactory,
) -> None:
    """Two events, not one: `agent.version.published` (the fact that a version
    exists, appended by `publish_version` so every publish path records it) and
    `agent.version.rolled_back` (the operator action). Collapsing them would
    make "who rolled this agent back and to what" unanswerable from the chain."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            agent_id = await _hire(client, headers)
            await _publish_missions(client, headers, agent_id, ["v2"])
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions/1/rollback", headers=headers
            )
            assert res.status_code == 201, res.text

    async with app_session(tenant) as db:
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.action == "agent.version.rolled_back",
                        m.AuditEvent.resource["agent_id"].astext == agent_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert int(events[0].resource["rolled_back_from"]) == 1
    assert int(events[0].resource["version_no"]) == 3


async def test_rolling_back_to_an_unknown_version_is_a_404(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            agent_id = await _hire(client, headers)
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions/99/rollback", headers=headers
            )
    assert res.status_code == 404, res.text


async def test_rollback_requires_the_publish_permission(
    app_session: AppSessionFactory,
) -> None:
    """Spec §6: rollback requires `agent_version:publish`, not a permission of
    its own -- it IS a publish."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            agent_id = await _hire(client, headers)
            await _publish_missions(client, headers, agent_id, ["v2"])
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions/1/rollback",
                headers={"Authorization": f"Bearer {_token(tenant, 'operator')}"},
            )
    assert res.status_code == 403, res.text


# ---------------------------------------------------------------- destructive
#
# Everything below pins what a rollback does to state that is NOT a scalar
# column: rows that exist now but not in the target, references the target
# holds that have since been deleted, and an operator's unpublished draft.


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(tenant)}"}


async def _skill_version(db: AsyncSession, tenant: uuid.UUID) -> m.SkillVersion:
    skill = m.Skill(tenant_id=tenant, name=f"S-{uuid.uuid4().hex}")
    db.add(skill)
    await db.flush()
    version = m.SkillVersion(
        tenant_id=tenant,
        skill_id=skill.id,
        semver="1.0.0",
        definition={},
        artifact_hash=b"\x00" * 32,
    )
    db.add(version)
    await db.flush()
    return version


async def test_rollback_drops_skills_and_grants_added_after_the_target(
    app_session: AppSessionFactory,
) -> None:
    """v1 has nothing; v2 adds a skill and a knowledge grant. Rolling back to v1
    must leave the agent holding NEITHER -- the skill row disabled (not
    deleted, so a re-assign finds it), the agent grant removed -- while a
    department-wide grant on the same knowledge base is untouched."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            agent_id = await _hire(client, _headers(tenant))

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, uuid.UUID(agent_id))
        assert agent is not None
        sv = await _skill_version(db, tenant)
        kb = m.KnowledgeBase(tenant_id=tenant, name=f"KB-{uuid.uuid4().hex}", embedding_model="e")
        db.add(kb)
        await db.flush()
        db.add_all(
            [
                m.SkillAssignment(
                    tenant_id=tenant, agent_id=agent.id, skill_version_id=sv.id, enabled=True
                ),
                m.KnowledgeGrant(
                    tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id
                ),
                m.KnowledgeGrant(
                    tenant_id=tenant,
                    kb_id=kb.id,
                    grantee_type="department",
                    grantee_id=agent.department_id,
                ),
            ]
        )
        sv_id, kb_id, dept_id = sv.id, kb.id, agent.department_id

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            headers = _headers(tenant)
            v2 = await client.post(
                f"/api/v1/agents/{agent_id}/versions",
                json={"expectedCurrentVersionNo": 1},
                headers=headers,
            )
            assert v2.status_code == 201, v2.text
            assert v2.json()["payload"]["skill_assignments"] == [{"skill_version_id": str(sv_id)}]
            assert v2.json()["payload"]["knowledge_grants"] == [str(kb_id)]

            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions/1/rollback", headers=headers
            )
            assert res.status_code == 201, res.text
            payload = res.json()["payload"]
            assert payload["skill_assignments"] == []
            assert payload["knowledge_grants"] == []

    async with app_session(tenant) as db:
        rows = (
            (
                await db.execute(
                    select(m.SkillAssignment).where(
                        m.SkillAssignment.agent_id == uuid.UUID(agent_id)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [(r.skill_version_id, r.enabled, r.deleted_at) for r in rows] == [
            (sv_id, False, None)
        ]
        grants = (
            (await db.execute(select(m.KnowledgeGrant).where(m.KnowledgeGrant.kb_id == kb_id)))
            .scalars()
            .all()
        )
        assert [(g.grantee_type, g.grantee_id) for g in grants] == [("department", dept_id)]


async def test_rollback_restores_a_skill_and_grant_removed_since_the_target(
    app_session: AppSessionFactory,
) -> None:
    """The other direction: v1 held a skill and a grant, v2 dropped both.
    Rolling back re-enables the SAME assignment row and re-creates the grant."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            agent_id = await _hire(client, _headers(tenant))

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, uuid.UUID(agent_id))
        assert agent is not None
        sv = await _skill_version(db, tenant)
        kb = m.KnowledgeBase(tenant_id=tenant, name=f"KB-{uuid.uuid4().hex}", embedding_model="e")
        db.add(kb)
        await db.flush()
        assignment = m.SkillAssignment(
            tenant_id=tenant, agent_id=agent.id, skill_version_id=sv.id, enabled=True
        )
        grant = m.KnowledgeGrant(
            tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id
        )
        db.add_all([assignment, grant])
        await db.flush()
        kb_id, assignment_id = kb.id, assignment.id

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            published = await client.post(
                f"/api/v1/agents/{agent_id}/versions",
                json={"expectedCurrentVersionNo": 1},
                headers=_headers(tenant),
            )
            assert published.status_code == 201, published.text  # v2: with both

    async with app_session(tenant) as db:
        row = await db.get(m.SkillAssignment, assignment_id)
        assert row is not None
        row.enabled = False
        g = (
            await db.execute(
                select(m.KnowledgeGrant).where(
                    m.KnowledgeGrant.kb_id == kb_id,
                    m.KnowledgeGrant.grantee_id == uuid.UUID(agent_id),
                )
            )
        ).scalar_one()
        await db.delete(g)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            headers = _headers(tenant)
            v3 = await client.post(
                f"/api/v1/agents/{agent_id}/versions",
                json={"expectedCurrentVersionNo": 2},
                headers=headers,
            )
            assert v3.status_code == 201, v3.text  # v3: with neither
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions/2/rollback", headers=headers
            )
            assert res.status_code == 201, res.text
            assert res.json()["versionNo"] == 4

    async with app_session(tenant) as db:
        rows = (
            (
                await db.execute(
                    select(m.SkillAssignment).where(
                        m.SkillAssignment.agent_id == uuid.UUID(agent_id)
                    )
                )
            )
            .scalars()
            .all()
        )
        # The same row, re-enabled -- not a second row beside it.
        assert [(r.id, r.enabled) for r in rows] == [(assignment_id, True)]
        grants = (
            (
                await db.execute(
                    select(m.KnowledgeGrant).where(
                        m.KnowledgeGrant.grantee_type == "agent",
                        m.KnowledgeGrant.grantee_id == uuid.UUID(agent_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [g.kb_id for g in grants] == [kb_id]


async def test_rollback_to_a_version_whose_knowledge_base_was_deleted_is_refused(
    app_session: AppSessionFactory,
) -> None:
    """Re-creating a grant on a deleted knowledge base would resurrect exactly
    the dangling row `delete_base` removes as an authz hazard. Silently
    dropping it instead would publish a version that is not the one asked
    for. So: refuse, name what is missing, change nothing."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            agent_id = await _hire(client, _headers(tenant))

    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(tenant_id=tenant, name=f"KB-{uuid.uuid4().hex}", embedding_model="e")
        db.add(kb)
        await db.flush()
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=uuid.UUID(agent_id)
            )
        )
        kb_id = kb.id

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            headers = _headers(tenant)
            v2 = await client.post(
                f"/api/v1/agents/{agent_id}/versions",
                json={"expectedCurrentVersionNo": 1},
                headers=headers,
            )
            assert v2.status_code == 201, v2.text
            patched = await client.patch(
                f"/api/v1/agents/{agent_id}/instructions",
                json={"instructions": "v3"},
                headers=headers,
            )
            assert patched.status_code == 200
            v3 = await client.post(
                f"/api/v1/agents/{agent_id}/versions",
                json={"expectedCurrentVersionNo": 2},
                headers=headers,
            )
            assert v3.status_code == 201, v3.text

    # Delete the base the way `delete_base` does: grants go, base tombstoned.
    async with app_session(tenant) as db:
        for g in (
            (await db.execute(select(m.KnowledgeGrant).where(m.KnowledgeGrant.kb_id == kb_id)))
            .scalars()
            .all()
        ):
            await db.delete(g)
        kb_row = await db.get(m.KnowledgeBase, kb_id)
        assert kb_row is not None
        kb_row.deleted_at = dt.datetime.now(tz=dt.UTC)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            headers = _headers(tenant)
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions/2/rollback", headers=headers
            )
            assert res.status_code == 422, res.text
            detail = res.json()["detail"]
            assert detail["error"] == "version_references_missing"
            assert detail["missing"] == {"knowledge_grants": [str(kb_id)]}
            agent = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
            assert agent.json()["mission"] == "v3"

    async with app_session(tenant) as db:
        assert (
            await db.execute(select(m.KnowledgeGrant).where(m.KnowledgeGrant.kb_id == kb_id))
        ).first() is None


async def test_rollback_overwrites_an_unpublished_draft_and_audits_what_it_discarded(
    app_session: AppSessionFactory,
) -> None:
    """Spec §2.7: the target is copied ONTO the working copy, so unpublished
    edits are replaced -- the UI confirms with the diff before sending. What
    was thrown away is recorded on the rollback event so it is never lost
    without trace."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            headers = _headers(tenant)
            agent_id = await _hire(client, headers)
            await _publish_missions(client, headers, agent_id, ["v2"])
            await client.patch(
                f"/api/v1/agents/{agent_id}/instructions",
                json={"instructions": "unpublished draft"},
                headers=headers,
            )
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions/1/rollback", headers=headers
            )
            assert res.status_code == 201, res.text
            agent = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
            assert agent.json()["mission"] == "v1"

    async with app_session(tenant) as db:
        event = (
            await db.execute(
                select(m.AuditEvent).where(
                    m.AuditEvent.action == "agent.version.rolled_back",
                    m.AuditEvent.resource["agent_id"].astext == agent_id,
                )
            )
        ).scalar_one()
    assert event.resource["discarded_draft_fields"] == ["mission"]


async def test_a_refused_no_op_rollback_keeps_the_unpublished_draft(
    app_session: AppSessionFactory,
) -> None:
    """Rolling back to the version already current is a no-op 409 -- and the
    working copy was overwritten on the way to discovering that. The 409 must
    roll that overwrite back, or a refused request would still have destroyed
    the operator's draft."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            headers = _headers(tenant)
            agent_id = await _hire(client, headers)
            await _publish_missions(client, headers, agent_id, ["v2"])
            await client.patch(
                f"/api/v1/agents/{agent_id}/instructions",
                json={"instructions": "unpublished draft"},
                headers=headers,
            )
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions/2/rollback", headers=headers
            )
            assert res.status_code == 409, res.text
            assert res.json()["detail"] == {
                "error": "no_changes_to_publish",
                "currentVersionNo": 2,
            }
            agent = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
            assert agent.json()["mission"] == "unpublished draft"


async def test_rolling_back_to_a_rolled_back_payload_is_still_a_no_op(
    app_session: AppSessionFactory,
) -> None:
    """v3 is a rollback to v1. Rolling back to v1 AGAIN must 409 -- `_meta`
    stays out of the hash -- and must report v3 as current, not v1."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            headers = _headers(tenant)
            agent_id = await _hire(client, headers)
            await _publish_missions(client, headers, agent_id, ["v2"])
            first = await client.post(
                f"/api/v1/agents/{agent_id}/versions/1/rollback", headers=headers
            )
            assert first.status_code == 201, first.text
            again = await client.post(
                f"/api/v1/agents/{agent_id}/versions/1/rollback", headers=headers
            )
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["currentVersionNo"] == 3


async def test_rolling_back_onto_a_subscription_model_with_triggers_is_refused(
    app_session: AppSessionFactory,
) -> None:
    """`switch_model` refuses a ChatGPT-subscription model for an agent with an
    enabled trigger. A rollback restoring `model_config_id` must not be the
    door around that."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            agent_id = await _hire(client, _headers(tenant))

    async with app_session(tenant) as db:
        cred = await create_credential(
            db,
            tenant_id=tenant,
            name=f"cg-{uuid.uuid4().hex[:8]}",
            credential_type="openai_chatgpt_subscription",
            field_values={"oauth_connection_id": str(uuid.uuid4())},
        )
        sub = m.ModelConfig(
            tenant_id=tenant, provider="openai_chatgpt", model="gpt-5", credential_id=cred.id
        )
        db.add(sub)
        await db.flush()
        agent = await db.get(m.Agent, uuid.UUID(agent_id))
        assert agent is not None
        agent.model_config_id = sub.id

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            headers = _headers(tenant)
            v2 = await client.post(
                f"/api/v1/agents/{agent_id}/versions",
                json={"expectedCurrentVersionNo": 1},
                headers=headers,
            )
            assert v2.status_code == 201, v2.text

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, uuid.UUID(agent_id))
        assert agent is not None
        agent.model_config_id = None
        db.add(
            m.Trigger(
                tenant_id=tenant,
                agent_id=agent.id,
                kind="cron",
                task_text="x",
                cron_expression="0 * * * *",
                enabled=True,
            )
        )

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            headers = _headers(tenant)
            v3 = await client.post(
                f"/api/v1/agents/{agent_id}/versions",
                json={"expectedCurrentVersionNo": 2},
                headers=headers,
            )
            assert v3.status_code == 201, v3.text
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions/2/rollback", headers=headers
            )
    assert res.status_code == 422, res.text
    assert "ChatGPT subscription" in str(res.json()["detail"])


# ------------------------------------------------ fix round 1: grants + model


async def _hire_with_grant_history(
    app_session: AppSessionFactory, tenant: uuid.UUID
) -> tuple[str, uuid.UUID]:
    """v1: no grant. v2: one agent grant on a fresh KB. Returns (agent, kb)."""
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            agent_id = await _hire(client, _headers(tenant))

    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(tenant_id=tenant, name=f"KB-{uuid.uuid4().hex}", embedding_model="e")
        db.add(kb)
        await db.flush()
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=uuid.UUID(agent_id)
            )
        )
        kb_id = kb.id

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            v2 = await client.post(
                f"/api/v1/agents/{agent_id}/versions",
                json={"expectedCurrentVersionNo": 1},
                headers=_headers(tenant),
            )
            assert v2.status_code == 201, v2.text
    return agent_id, kb_id


async def test_a_dept_manager_may_not_change_knowledge_grants_through_a_rollback(
    app_session: AppSessionFactory,
) -> None:
    """`POST /knowledge/grants` needs `knowledge:manage`, which `dept_manager`
    does not hold. Rolling back to a version with a different grant set is a
    grant write, so it needs the same permission -- otherwise rollback is the
    side door that removes (or re-creates) a grant an admin decided on."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id, kb_id = await _hire_with_grant_history(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions/1/rollback",
                headers={"Authorization": f"Bearer {_token(tenant, 'dept_manager')}"},
            )
    assert res.status_code == 403, res.text
    assert "knowledge:manage" in str(res.json()["detail"])

    async with app_session(tenant) as db:
        grants = (
            (await db.execute(select(m.KnowledgeGrant).where(m.KnowledgeGrant.kb_id == kb_id)))
            .scalars()
            .all()
        )
        assert len(grants) == 1
        count = (
            await db.execute(
                select(m.AgentVersion).where(m.AgentVersion.agent_id == uuid.UUID(agent_id))
            )
        ).all()
        assert len(count) == 2


async def test_a_dept_manager_may_roll_back_when_the_grants_do_not_change(
    app_session: AppSessionFactory,
) -> None:
    """No blanket requirement: a rollback that leaves the grant set alone (here
    v3 -> v2, both holding the same grant, differing only in mission) needs
    nothing beyond `agent_version:publish`."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id, kb_id = await _hire_with_grant_history(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            await _publish_missions(client, _headers(tenant), agent_id, [])
            patched = await client.patch(
                f"/api/v1/agents/{agent_id}/instructions",
                json={"instructions": "v3"},
                headers=_headers(tenant),
            )
            assert patched.status_code == 200
            v3 = await client.post(
                f"/api/v1/agents/{agent_id}/versions",
                json={"expectedCurrentVersionNo": 2},
                headers=_headers(tenant),
            )
            assert v3.status_code == 201, v3.text
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions/2/rollback",
                headers={"Authorization": f"Bearer {_token(tenant, 'dept_manager')}"},
            )
            assert res.status_code == 201, res.text
            assert res.json()["payload"]["knowledge_grants"] == [str(kb_id)]


async def test_the_rollback_event_records_which_grants_changed(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id, kb_id = await _hire_with_grant_history(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions/1/rollback", headers=_headers(tenant)
            )
            assert res.status_code == 201, res.text

    async with app_session(tenant) as db:
        event = (
            await db.execute(
                select(m.AuditEvent).where(
                    m.AuditEvent.action == "agent.version.rolled_back",
                    m.AuditEvent.resource["agent_id"].astext == agent_id,
                )
            )
        ).scalar_one()
    assert event.resource["knowledge_grants_added"] == []
    assert event.resource["knowledge_grants_removed"] == [str(kb_id)]


async def test_rolling_back_the_model_refreshes_the_displayed_model(
    app_session: AppSessionFactory,
) -> None:
    """`presentation.llm/provider` is not versioned, so rollback must refresh
    it itself when it changes `model_config_id` -- otherwise the agent list
    names the model the agent ran BEFORE the rollback."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        first = m.ModelConfig(
            tenant_id=tenant,
            provider="anthropic",
            model="claude-first",
            display_name=f"First-{uuid.uuid4().hex[:6]}",
        )
        second = m.ModelConfig(tenant_id=tenant, provider="openai", model="gpt-second")
        db.add_all([first, second])
        await db.flush()
        first_id, first_name, second_id = first.id, first.display_name, second.id

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            headers = _headers(tenant)
            agent_id = await _hire(client, headers)
            for index, model_id in enumerate([first_id, second_id], start=1):
                switched = await client.patch(
                    f"/api/v1/agents/{agent_id}/model-config",
                    json={"modelConfigId": str(model_id)},
                    headers=headers,
                )
                assert switched.status_code == 200, switched.text
                published = await client.post(
                    f"/api/v1/agents/{agent_id}/versions",
                    json={"expectedCurrentVersionNo": index},
                    headers=headers,
                )
                assert published.status_code == 201, published.text

            before = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
            assert (before.json()["llm"], before.json()["provider"]) == ("gpt-second", "openai")

            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions/2/rollback", headers=headers
            )
            assert res.status_code == 201, res.text
            after = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
    assert (after.json()["llm"], after.json()["provider"]) == (first_name, "anthropic")
