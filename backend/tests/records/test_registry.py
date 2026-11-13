from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select, update

from oc8 import models as m
from oc8.records.registry import (
    canonical,
    issued_links,
    link_scope,
    linked_hosts,
    remember_links,
    verified_links,
)
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

S = "chat:00000000-0000-0000-0000-00000000000a"


async def test_a_remembered_link_is_issued_for_its_tenant_only(
    app_session: AppSessionFactory,
) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    url = "https://odoo.example.com/odoo/sale.order/42"
    async with app_session(a) as db:
        await remember_links(db, tenant_id=a, scope=S, urls=[url, url])
        assert await issued_links(
            db, tenant_id=a, scope=S, urls=[url, "https://odoo.example.com/x"]
        ) == {url}
        assert await linked_hosts(db, tenant_id=a) == {"odoo.example.com"}
    async with app_session(b) as db:
        assert await issued_links(db, tenant_id=b, scope=S, urls=[url]) == set()


async def test_an_old_link_is_no_longer_issued(app_session: AppSessionFactory) -> None:
    t = uuid.uuid4()
    url = "https://jira.example.com/browse/ABC-1"
    async with app_session(t) as db:
        await remember_links(db, tenant_id=t, scope=S, urls=[url])
        await db.execute(
            update(m.RecordLink).values(issued_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=31))
        )
        assert await issued_links(db, tenant_id=t, scope=S, urls=[url]) == set()


async def test_remembering_nothing_is_a_no_op(app_session: AppSessionFactory) -> None:
    t = uuid.uuid4()
    async with app_session(t) as db:
        await remember_links(db, tenant_id=t, scope=S, urls=[])
        assert await linked_hosts(db, tenant_id=t) == set()


async def test_a_link_is_stored_with_where_it_leads(app_session: AppSessionFactory) -> None:
    t = uuid.uuid4()
    url = "https://odoo.example.com/odoo/action-1?ids=[1,2]"
    async with app_session(t) as db:
        await remember_links(db, tenant_id=t, scope=S, urls=[url])
        stored = await db.scalar(select(m.RecordLink.canon).where(m.RecordLink.tenant_id == t))
    assert stored == canonical(url)
    assert stored == canonical("https://ODOO.example.com:443/odoo/action-1?ids=%5B1,2%5D")


async def test_remembering_again_fills_a_missing_canon(app_session: AppSessionFactory) -> None:
    """Rows written before `canon` existed get it the next time they are issued."""
    t = uuid.uuid4()
    url = "https://odoo.example.com/odoo/sale.order/42"
    async with app_session(t) as db:
        await remember_links(db, tenant_id=t, scope=S, urls=[url])
        await db.execute(update(m.RecordLink).values(canon=None))
        await remember_links(db, tenant_id=t, scope=S, urls=[url])
        assert await db.scalar(select(m.RecordLink.canon)) == canonical(url)


async def test_verified_links_match_by_target_not_spelling(
    app_session: AppSessionFactory,
) -> None:
    t = uuid.uuid4()
    issued = "https://odoo.example.com/odoo/action-1?ids=[1,2]"
    reencoded = "https://odoo.example.com/odoo/action-1?ids=%5B1,2%5D"
    backslash = "https://odoo.example.com\\odoo\\action-1?ids=[1,2]"
    other_port = "https://odoo.example.com:8443/odoo/action-1?ids=[1,2]"
    async with app_session(t) as db:
        await remember_links(db, tenant_id=t, scope=S, urls=[issued])
        got = await verified_links(
            db, tenant_id=t, scope=S, urls=[reencoded, backslash, other_port, "not a url"]
        )
    assert got == {reencoded, backslash}


async def test_canonical_refuses_what_is_not_an_http_url() -> None:
    assert canonical("javascript:alert(1)") is None
    assert canonical("https://[x") is None
    assert canonical("ftp://h.example/x") is None


async def test_a_link_is_issued_in_its_own_scope_only(app_session: AppSessionFactory) -> None:
    """The oracle the scope closes: a link one conversation was given is not
    issued for an answer in another one, by string or by target."""
    t = uuid.uuid4()
    url = "https://odoo.example.com/odoo/hr.employee/17"
    other = "chat:00000000-0000-0000-0000-00000000000b"
    async with app_session(t) as db:
        await remember_links(db, tenant_id=t, scope=S, urls=[url])
        assert await issued_links(db, tenant_id=t, scope=other, urls=[url]) == set()
        assert await verified_links(db, tenant_id=t, scope=other, urls=[url]) == set()
        assert await verified_links(db, tenant_id=t, scope=S, urls=[url]) == {url}
        # Hosts stay tenant-wide: they only decide what is checked at all.
        assert await linked_hosts(db, tenant_id=t) == {"odoo.example.com"}


async def test_the_same_url_in_two_scopes_is_two_rows(app_session: AppSessionFactory) -> None:
    t = uuid.uuid4()
    url = "https://odoo.example.com/odoo/sale.order/42"
    other = "task:00000000-0000-0000-0000-00000000000c"
    async with app_session(t) as db:
        await remember_links(db, tenant_id=t, scope=S, urls=[url])
        await remember_links(db, tenant_id=t, scope=other, urls=[url])
        await remember_links(db, tenant_id=t, scope=other, urls=[url])
        scopes = (await db.execute(select(m.RecordLink.scope))).scalars().all()
        assert sorted(scopes) == sorted([S, other])
        assert await issued_links(db, tenant_id=t, scope=other, urls=[url]) == {url}


async def _task(db: Any, tenant: uuid.UUID, parent: uuid.UUID | None = None) -> m.Task:
    task = m.Task(
        tenant_id=tenant,
        department_id=uuid.uuid4(),
        title="t",
        state="in_progress",
        parent_task_id=parent,
    )
    db.add(task)
    await db.flush()
    return task


async def test_a_chat_run_is_scoped_to_its_conversation(app_session: AppSessionFactory) -> None:
    t = uuid.uuid4()
    session = uuid.uuid4()
    async with app_session(t) as db:
        task = await _task(db, t)
        run = m.AgentRun(
            tenant_id=t,
            agent_id=uuid.uuid4(),
            task_id=task.id,
            context={"chat_session_id": str(session).upper()},
        )
        assert await link_scope(db, run=run) == f"chat:{session}"
        assert await link_scope(db, context={"chat_session_id": str(session)}) == (
            f"chat:{session}"
        )


async def test_a_delegated_run_shares_its_parent_s_scope(app_session: AppSessionFactory) -> None:
    """`_delegate` copies the session id into the child's context, so what the
    child issues counts in the conversation the copilot answers in."""
    t = uuid.uuid4()
    ctx = {"chat_session_id": str(uuid.uuid4())}
    async with app_session(t) as db:
        lead_task = await _task(db, t)
        child_task = await _task(db, t, parent=lead_task.id)
        lead = m.AgentRun(tenant_id=t, agent_id=uuid.uuid4(), task_id=lead_task.id, context=ctx)
        child = m.AgentRun(
            tenant_id=t,
            agent_id=uuid.uuid4(),
            task_id=child_task.id,
            context={**ctx, "parent_task_id": str(lead_task.id), "delegation_depth": 1},
        )
        assert await link_scope(db, run=child) == await link_scope(db, run=lead)


async def test_without_a_chat_the_root_task_is_the_scope(app_session: AppSessionFactory) -> None:
    t = uuid.uuid4()
    async with app_session(t) as db:
        root = await _task(db, t)
        middle = await _task(db, t, parent=root.id)
        leaf = await _task(db, t, parent=middle.id)
        run = m.AgentRun(tenant_id=t, agent_id=uuid.uuid4(), task_id=leaf.id, context={})
        assert await link_scope(db, run=run) == f"task:{root.id}"
        assert await link_scope(db, task_id=middle.id) == f"task:{root.id}"


async def test_a_broken_task_chain_falls_back_to_the_run_s_own_task(
    app_session: AppSessionFactory,
) -> None:
    t = uuid.uuid4()
    async with app_session(t) as db:
        dangling = await _task(db, t, parent=uuid.uuid4())
        a = await _task(db, t)
        b = await _task(db, t, parent=a.id)
        a.parent_task_id = b.id  # a cycle: never a root
        await db.flush()
        assert await link_scope(db, task_id=dangling.id) == f"task:{dangling.id}"
        assert await link_scope(db, task_id=b.id) == f"task:{b.id}"


async def test_a_failing_task_lookup_falls_back_to_the_own_task(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = uuid.uuid4()
    async with app_session(t) as db:
        task = await _task(db, t)
        child = await _task(db, t, parent=task.id)

        async def _boom(*_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("db down")

        monkeypatch.setattr(db, "execute", _boom)
        assert await link_scope(db, task_id=child.id) == f"task:{child.id}"


async def test_without_chat_or_task_the_run_is_the_scope(app_session: AppSessionFactory) -> None:
    t = uuid.uuid4()
    run_id = uuid.uuid4()
    async with app_session(t) as db:
        run = m.AgentRun(id=run_id, tenant_id=t, agent_id=uuid.uuid4(), context={})
        assert await link_scope(db, run=run) == f"run:{run_id}"
        assert (await link_scope(db)).startswith("run:")
