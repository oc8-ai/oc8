from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

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


async def _exists(db: AsyncSession, model: type, row_id: uuid.UUID) -> bool:
    # a query, not db.get(): the session's identity map still holds the cascaded row
    return (await db.execute(sa.select(model.id).where(model.id == row_id))).first() is not None


async def _member(app_session: AppSessionFactory, tenant: uuid.UUID, name: str) -> m.OrgMember:
    async with app_session(tenant) as db:
        member = m.OrgMember(tenant_id=tenant, subject=name, subject_uuid=uuid.uuid4())
        db.add(member)
        await db.flush()
    return member


def _personal_connection(tenant: uuid.UUID, owner: uuid.UUID | None) -> m.OAuthConnection:
    return m.OAuthConnection(
        tenant_id=tenant,
        provider="microsoft",
        account_label="a@acme.test",
        scopes=[],
        access_secret_ref="",
        client_source="platform",
        owner_member_id=owner,
    )


async def test_deleting_a_member_removes_their_personal_connection_they_bound_to_an_agent(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session)
    tenant = agent.tenant_id
    member = await _member(app_session, tenant, "anna@acme.test")
    async with app_session(tenant) as db:
        conn = _personal_connection(tenant, member.id)
        org_conn = _personal_connection(tenant, None)
        db.add_all([conn, org_conn])
        await db.flush()
        row = m.AgentToolAccess(
            tenant_id=tenant,
            agent_id=agent.id,
            tool_key="microsoft365",
            mode="fixed",
            oauth_connection_id=conn.id,
            set_by_member_id=member.id,
        )
        db.add(row)
        await db.flush()
        conn_id, org_id = conn.id, org_conn.id
        await db.delete(await db.get(m.OrgMember, member.id))
        await db.flush()
        await db.refresh(row)
        assert (row.mode, row.oauth_connection_id, row.set_by_member_id) == ("fixed", None, None)
        assert await _exists(db, m.OAuthConnection, conn_id) is False
        assert await _exists(db, m.OAuthConnection, org_id) is True


async def test_deleting_a_member_removes_their_personal_credential_they_bound_to_an_agent(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session)
    tenant = agent.tenant_id
    member = await _member(app_session, tenant, "ben@acme.test")
    async with app_session(tenant) as db:
        cred = m.Credential(
            tenant_id=tenant, name="Odoo", credential_type="odoo_login", owner_member_id=member.id
        )
        db.add(cred)
        await db.flush()
        row = m.AgentToolAccess(
            tenant_id=tenant,
            agent_id=agent.id,
            tool_key="odoo",
            mode="fixed",
            credential_id=cred.id,
            set_by_member_id=member.id,
        )
        db.add(row)
        await db.flush()
        cred_id = cred.id
        await db.delete(await db.get(m.OrgMember, member.id))
        await db.flush()
        await db.refresh(row)
        assert (row.mode, row.credential_id, row.set_by_member_id) == ("fixed", None, None)
        assert await _exists(db, m.Credential, cred_id) is False


async def test_a_same_tenant_insert_and_update_pass_the_guard_under_row_level_security(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session)
    tenant = agent.tenant_id
    member = await _member(app_session, tenant, "cara@acme.test")
    async with app_session(tenant) as db:
        conn = _personal_connection(tenant, member.id)
        cred = m.Credential(tenant_id=tenant, name="Odoo", credential_type="odoo_login")
        db.add_all([conn, cred])
        await db.flush()
        row = m.AgentToolAccess(
            tenant_id=tenant,
            agent_id=agent.id,
            tool_key="odoo",
            mode="fixed",
            credential_id=cred.id,
            set_by_member_id=member.id,
        )
        db.add(row)
        await db.flush()
        row.credential_id = None
        row.oauth_connection_id = conn.id
        await db.flush()
        cred.owner_member_id = member.id
        await db.flush()


async def _foreign_ids(app_session: AppSessionFactory) -> dict[str, uuid.UUID]:
    other = (await _make_agent(app_session)).tenant_id
    member = await _member(app_session, other, "x@other.test")
    async with app_session(other) as db:
        conn = _personal_connection(other, None)
        cred = m.Credential(tenant_id=other, name="Odoo", credential_type="odoo_login")
        db.add_all([conn, cred])
        await db.flush()
    other_agent = await _make_agent(app_session, tenant_id=other)
    return {
        "member": member.id,
        "connection": conn.id,
        "credential": cred.id,
        "agent": other_agent.id,
    }


@pytest.mark.parametrize(
    "target",
    [
        "credential.owner_member_id",
        "trigger.created_by_member_id",
        "access.agent_id",
        "access.oauth_connection_id",
        "access.credential_id",
        "access.set_by_member_id",
    ],
)
async def test_every_guarded_reference_refuses_another_tenant(
    app_session: AppSessionFactory, target: str
) -> None:
    agent = await _make_agent(app_session)
    tenant = agent.tenant_id
    foreign = await _foreign_ids(app_session)
    with pytest.raises(sa.exc.IntegrityError, match="same tenant"):
        async with app_session(tenant) as db:
            if target == "credential.owner_member_id":
                db.add(
                    m.Credential(
                        tenant_id=tenant,
                        name="Odoo",
                        credential_type="odoo_login",
                        owner_member_id=foreign["member"],
                    )
                )
            elif target == "trigger.created_by_member_id":
                db.add(
                    m.Trigger(
                        tenant_id=tenant,
                        agent_id=agent.id,
                        kind="cron",
                        task_text="t",
                        cron_expression="0 8 * * *",
                        created_by_member_id=foreign["member"],
                    )
                )
            else:
                column = target.split(".", 1)[1]
                values = {
                    "agent_id": foreign["agent"],
                    "oauth_connection_id": foreign["connection"],
                    "credential_id": foreign["credential"],
                    "set_by_member_id": foreign["member"],
                }
                kwargs = {"agent_id": agent.id, "mode": "fixed", column: values[column]}
                db.add(m.AgentToolAccess(tenant_id=tenant, tool_key="odoo", **kwargs))
            await db.flush()
