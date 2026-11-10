"""An office for the agent builder: two departments (one with Odoo in its
frame), a chat-enabled and a disabled model, a skill with an old and a current
version, and three Copilot members -- an admin (token role decides), an
employee with a plain seat, and a lead with an approver seat and the
agent_manage toggle."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.authz.authority import authority_for_member, tenant_wide_read
from oc8.authz.permissions import DEPARTMENT, SEAT_APPROVER, SEAT_VIEWER, VIEW, perm
from oc8.authz.scope import scope_for_member
from oc8.copilot.builder import create_builder_proposal
from oc8.copilot.models import CopilotProposal
from tests.copilot.helpers import Seat, copilot_seat


@dataclass(frozen=True)
class BuilderOffice:
    tenant: uuid.UUID
    assistant_id: uuid.UUID
    department_id: uuid.UUID
    other_department_id: uuid.UUID
    model_id: uuid.UUID
    disabled_model_id: uuid.UUID
    skill_version_id: uuid.UUID
    old_skill_version_id: uuid.UUID
    admin: Seat
    employee: Seat
    lead: Seat


async def builder_office(db: AsyncSession, tenant: uuid.UUID) -> BuilderOffice:
    assistant = await get_or_create_assistant(db, tenant_id=tenant)
    books = m.Department(
        tenant_id=tenant,
        name="Buchhaltung",
        frame={
            "tools": {"odoo": {"enabled": True, "read": True, "modify": True}},
            "kbs": [],
            "memory": {"department": ["read", "write"], "company": ["read"]},
        },
    )
    other = m.Department(tenant_id=tenant, name="Entwicklung", frame={"tools": {}})
    db.add_all([books, other])
    await db.flush()
    model = m.ModelConfig(
        tenant_id=tenant, provider="ollama", model="stub", locality="local", chat_enabled=True
    )
    off = m.ModelConfig(
        tenant_id=tenant, provider="ollama", model="off", locality="local", chat_enabled=False
    )
    db.add_all([model, off])
    await db.flush()
    skill = m.Skill(
        tenant_id=tenant, name="Rechnungen prüfen", origin="local", trust_level="first_party"
    )
    db.add(skill)
    await db.flush()
    old = m.SkillVersion(
        tenant_id=tenant,
        skill_id=skill.id,
        semver="1.0.0",
        definition={"requires": {}},
        artifact_hash=uuid.uuid4().bytes,
    )
    new = m.SkillVersion(
        tenant_id=tenant,
        skill_id=skill.id,
        semver="1.1.0",
        definition={"requires": {}},
        artifact_hash=uuid.uuid4().bytes,
    )
    db.add_all([old, new])
    await db.flush()
    skill.current_version_id = new.id
    db.add(
        m.McpConnection(
            tenant_id=tenant,
            name="odoo",
            server_url="stdio://odoo",
            transport="stdio",
            connected=True,
            config={"_plugin_name": "odoo_mcp", "_connection_key": "odoo"},
        )
    )
    admin = await copilot_seat(db, tenant, "admin", with_role=False)
    employee = await copilot_seat(db, tenant, "emp")
    lead = await copilot_seat(db, tenant, "lead")
    db.add_all(
        [
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=employee.member_id,
                department_id=books.id,
                seat_role=SEAT_VIEWER,
                agent_manage=False,
            ),
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=lead.member_id,
                department_id=books.id,
                seat_role=SEAT_APPROVER,
                agent_manage=True,
            ),
        ]
    )
    await db.flush()
    return BuilderOffice(
        tenant=tenant,
        assistant_id=assistant.id,
        department_id=books.id,
        other_department_id=other.id,
        model_id=model.id,
        disabled_model_id=off.id,
        skill_version_id=new.id,
        old_skill_version_id=old.id,
        admin=admin,
        employee=employee,
        lead=lead,
    )


def invoice_checker_ops(
    office: BuilderOffice, *, name: str = "Rechnungsprüfer"
) -> list[dict[str, Any]]:
    return [
        {
            "type": "agent.create",
            "ref": "$new",
            "departmentId": str(office.department_id),
            "name": name,
            "roleTitle": "Buchhaltung",
            "mission": "Prüft jeden Morgen offene Rechnungen und meldet Auffälligkeiten.",
        },
        {"type": "agent.model.switch", "agentRef": "$new", "modelConfigId": str(office.model_id)},
        {
            "type": "agent.skill.assign",
            "agentRef": "$new",
            "skillVersionId": str(office.skill_version_id),
        },
        {
            "type": "trigger.create",
            "agentRef": "$new",
            "kind": "cron",
            "taskText": "Prüfe die offenen Rechnungen.",
            "cronExpression": "0 7 * * 1-5",
        },
    ]


async def propose_as_admin(
    db: AsyncSession,
    office: BuilderOffice,
    ops: list[dict[str, Any]] | None = None,
    *,
    now: dt.datetime | None = None,
) -> CopilotProposal:
    """A builder proposal in the admin's chat, made the way `propose_agent`
    makes one."""
    member = await db.get(m.OrgMember, office.admin.member_id)
    assert member is not None
    scope = await scope_for_member(db, member, token_role="org_admin")
    authority = await authority_for_member(db, member, token_role="org_admin")
    return await create_builder_proposal(
        db,
        tenant_id=office.tenant,
        member=member,
        scope=scope,
        tenant_wide=tenant_wide_read(authority, perm(DEPARTMENT, VIEW)),
        session_id=office.admin.session_id,
        operations=invoice_checker_ops(office) if ops is None else ops,
        now=now or dt.datetime.now(tz=dt.UTC),
    )
