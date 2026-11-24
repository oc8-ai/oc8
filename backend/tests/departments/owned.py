"""A shared world for the tests of a department with an owner: a tenant with
personal departments switched on, one member, their personal department
(created through `get_or_create_personal_department`, so it starts from the
tenant's default frame) and one agent in it. With `tool_key`, the agent's tool
is fixed to the member's own login and stamped by the member through
`write_tool_access`, as the owner's confirmation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from oc8 import models as m
from oc8.agents.versioning import publish_version
from oc8.authz.scope import subject_uuid_for
from oc8.connections.access_write import write_tool_access
from oc8.departments.personal import ENABLED_KEY, FRAME_KEY, get_or_create_personal_department
from tests.conftest import AppSessionFactory
from tests.connections.helpers import credential_row


@dataclass(frozen=True)
class OwnedWorld:
    tenant: uuid.UUID
    member_id: uuid.UUID
    department_id: uuid.UUID
    agent_id: uuid.UUID
    #: The member's own login the agent's `tool_key` is fixed to, if any.
    credential_id: uuid.UUID | None = None


async def owned_world(
    app_session: AppSessionFactory,
    *,
    tools: dict[str, Any],
    tool_key: str | None = None,
    credential_type: str = "odoo_login",
    frame_capa_defaults: dict[str, Any] | None = None,
) -> OwnedWorld:
    """`frame_capa_defaults` is set before the owner confirms: it is part of
    what they confirm (`connections.confirmation`)."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(
            m.Organization(
                id=tenant,
                slug=f"t-{tenant.hex[:8]}",
                name="T",
                settings={ENABLED_KEY: True, FRAME_KEY: tools},
            )
        )
        subject = f"user-{uuid.uuid4().hex[:8]}"
        member = m.OrgMember(
            tenant_id=tenant,
            subject=subject,
            subject_uuid=subject_uuid_for(subject),
            display_name="Ada",
        )
        db.add(member)
        await db.flush()
        dept = await get_or_create_personal_department(db, member=member)
        dept.frame_capa_defaults = frame_capa_defaults
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Helper", mission="m")
        db.add(agent)
        await db.flush()
        await publish_version(db, agent)
        member_id, dept_id, agent_id = member.id, dept.id, agent.id

    credential_id: uuid.UUID | None = None
    if tool_key is not None:
        credential_id = await credential_row(
            app_session, tenant, owner=member_id, credential_type=credential_type
        )
        async with app_session(tenant) as db:
            live = await db.get(m.Agent, agent_id)
            assert live is not None
            await write_tool_access(
                db,
                agent=live,
                tool_key=tool_key,
                mode="fixed",
                oauth_connection_id=None,
                credential_id=credential_id,
                set_by_member_id=member_id,
                set_with_manage=False,
                actor_type="operator",
                actor_id=member_id,
            )
    return OwnedWorld(tenant, member_id, dept_id, agent_id, credential_id)
