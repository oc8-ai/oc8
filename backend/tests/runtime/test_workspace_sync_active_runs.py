"""sync_active_run_outputs: the periodic half of the sync design. Regression
coverage for a real bug the happy-path tests in test_workspace_sync.py
caught indirectly: sync_run_output must never commit internally, because
`tenant_session`'s tenant binding is transaction-local
(`set_config('app.tenant_id', ..., true)`) -- an internal commit would
silently drop RLS for every run after the first in this function's own
per-tenant loop, and no exception would surface until a much later,
unrelated-looking failure. See oc8.runtime.workspace's sync_run_output
docstring."""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.config import get_settings
from oc8.runtime.workspace import output_dir, sync_active_run_outputs
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _use_tmp_session_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("OC8_RUNTIME_SESSION_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _new_tenant_with_org(app_session: AppSessionFactory) -> uuid.UUID:
    """sync_active_run_outputs discovers tenants via list_active_tenant_ids,
    which enumerates `organization` rows (see triggers/test_scheduler.py's
    identical fixture) -- a bare tenant UUID with no Organization row is
    invisible to the sweep."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(
            m.Organization(
                id=tenant,
                slug=f"test-{tenant.hex[:12]}",
                name="Test Org",
                tier="standard",
                region="eu",
            )
        )
        await db.flush()
    return tenant


async def _dept_agent_run(db: Any, tenant: uuid.UUID, *, state: str) -> m.AgentRun:
    dept = m.Department(tenant_id=tenant, name="Compliance", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Nora",
        status="running",
        definition={},
        presentation={},
        narrowing={},
    )
    db.add(agent)
    await db.flush()
    run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state=state)
    db.add(run)
    await db.flush()
    return run


def _write_output_file(run_id: uuid.UUID, name: str, content: bytes) -> None:
    root = output_dir(run_id)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, name), "wb") as fh:
        fh.write(content)


async def test_syncs_every_running_run_across_the_tenant_not_just_the_first(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    """Two RUNNING runs under the same tenant, both with an output file: if
    sync_run_output's internal commit ever comes back, the second run's
    insert (or the dedup SELECT before it) hits an unbound tenant GUC and
    this fails, either by raising or by silently syncing only the first
    run's file."""
    tenant = await _new_tenant_with_org(app_session)
    async with app_session(tenant) as db:
        run_a = await _dept_agent_run(db, tenant, state="running")
        run_b = await _dept_agent_run(db, tenant, state="running")
        _write_output_file(run_a.id, "a.txt", b"from run a")
        _write_output_file(run_b.id, "b.txt", b"from run b")

    # >=, not ==: the sweep is tenant-global, so a leftover RUNNING run from
    # another test in the same session could also contribute a sync -- this
    # test only asserts its own two files land, not that it's the only work
    # done.
    total = await sync_active_run_outputs()
    assert total >= 2

    async with app_session(tenant) as db:
        rows = (
            (
                await db.execute(
                    select(m.FileAttachment).where(
                        m.FileAttachment.owner_id.in_([run_a.id, run_b.id])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {r.filename for r in rows} == {"a.txt", "b.txt"}


async def test_skips_a_run_that_is_no_longer_running(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = await _new_tenant_with_org(app_session)
    async with app_session(tenant) as db:
        done_run = await _dept_agent_run(db, tenant, state="done")
        _write_output_file(done_run.id, "report.txt", b"finished already")

    total = await sync_active_run_outputs()
    assert total == 0

    async with app_session(tenant) as db:
        rows = (
            (
                await db.execute(
                    select(m.FileAttachment).where(m.FileAttachment.owner_id == done_run.id)
                )
            )
            .scalars()
            .all()
        )
        assert rows == []
