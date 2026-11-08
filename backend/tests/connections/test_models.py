from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from oc8 import models as m
from tests.conftest import AppSessionFactory
from tests.factories import _make_agent

pytestmark = pytest.mark.asyncio


async def test_a_user_mode_row_names_no_target(app_session: AppSessionFactory) -> None:
    agent = await _make_agent(app_session)
    async with app_session(agent.tenant_id) as db:
        db.add(
            m.AgentToolAccess(
                tenant_id=agent.tenant_id, agent_id=agent.id, tool_key="microsoft365", mode="user"
            )
        )
        await db.flush()
    with pytest.raises(sa.exc.IntegrityError):
        async with app_session(agent.tenant_id) as db:
            db.add(
                m.AgentToolAccess(
                    tenant_id=agent.tenant_id,
                    agent_id=agent.id,
                    tool_key="odoo",
                    mode="user",
                    credential_id=uuid.uuid4(),
                )
            )
            await db.flush()


async def test_a_fixed_row_names_at_most_one_target(app_session: AppSessionFactory) -> None:
    agent = await _make_agent(app_session)
    with pytest.raises(sa.exc.IntegrityError):
        async with app_session(agent.tenant_id) as db:
            cred = m.Credential(
                tenant_id=agent.tenant_id, name="Odoo", credential_type="odoo_login"
            )
            conn = m.OAuthConnection(
                tenant_id=agent.tenant_id,
                provider="microsoft",
                account_label="a@acme.test",
                scopes=[],
                access_secret_ref="",
                client_source="platform",
            )
            db.add_all([cred, conn])
            await db.flush()
            db.add(
                m.AgentToolAccess(
                    tenant_id=agent.tenant_id,
                    agent_id=agent.id,
                    tool_key="odoo",
                    mode="fixed",
                    credential_id=cred.id,
                    oauth_connection_id=conn.id,
                )
            )
            await db.flush()


async def test_a_deleted_target_leaves_a_fixed_row_without_target(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session)
    async with app_session(agent.tenant_id) as db:
        conn = m.OAuthConnection(
            tenant_id=agent.tenant_id,
            provider="microsoft",
            account_label="a@acme.test",
            scopes=[],
            access_secret_ref="",
            client_source="platform",
        )
        db.add(conn)
        await db.flush()
        row = m.AgentToolAccess(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            tool_key="microsoft365",
            mode="fixed",
            oauth_connection_id=conn.id,
        )
        db.add(row)
        await db.flush()
        await db.delete(conn)
        await db.flush()
        await db.refresh(row)
        assert (row.mode, row.oauth_connection_id) == ("fixed", None)


async def test_a_removed_creator_leaves_the_trigger_without_owner(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session)
    async with app_session(agent.tenant_id) as db:
        member = m.OrgMember(
            tenant_id=agent.tenant_id, subject="anna@acme.test", subject_uuid=uuid.uuid4()
        )
        db.add(member)
        await db.flush()
        trigger = m.Trigger(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            kind="cron",
            task_text="t",
            cron_expression="0 8 * * *",
            created_by_member_id=member.id,
        )
        db.add(trigger)
        await db.flush()
        await db.delete(member)
        await db.flush()
        await db.refresh(trigger)
        assert trigger.created_by_member_id is None
