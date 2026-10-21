"""The sandbox output sync against a real Docker sandbox: the GNU find listing,
one get_archive per file, names with spaces and tabs, a symlink and a file
over the attachment cap. Runtime images are Debian-based; a busybox userland
fails the listing loudly instead of silently syncing nothing."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.runtime.workspace import sync_sandbox_output
from oc8.sandbox.docker_driver import DockerSandboxDriver
from oc8.sandbox.types import SandboxSpec, ScratchDir
from oc8.storage.attachments import MAX_ATTACHMENT_BYTES
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_DEBIAN = "debian:bookworm-slim"
_ALPINE = "alpine:latest"


async def _run(db: Any, tenant: uuid.UUID) -> m.AgentRun:
    dept = m.Department(tenant_id=tenant, name="Ops", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Ada",
        status="running",
        definition={},
        presentation={},
        narrowing={},
    )
    db.add(agent)
    await db.flush()
    run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running")
    db.add(run)
    await db.flush()
    return run


async def test_a_debian_sandbox_output_is_listed_and_fetched_per_file(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    driver = DockerSandboxDriver()
    handle = await driver.provision(
        SandboxSpec(image=_DEBIAN, scratch=(ScratchDir("/workspace", 128),))
    )
    try:
        script = (
            "set -e; mkdir -p '/workspace/output/sub dir'; cd /workspace/output; "
            "printf 'Q3' > report.txt; printf 'deep' > 'sub dir/a b.txt'; "
            "printf 'tab' > \"$(printf 'tab\\there.txt')\"; "
            "ln -s /etc/shadow link.txt; "
            f"head -c {MAX_ATTACHMENT_BYTES + 1} /dev/zero > big.txt"
        )
        res = await driver.exec(handle, ["sh", "-c", script])
        assert res.exit_code == 0, res.output

        tenant = uuid.uuid4()
        async with app_session(tenant) as db:
            run = await _run(db, tenant)
            synced = await sync_sandbox_output(
                db, tenant_id=tenant, run_id=run.id, driver=driver, handle=handle
            )
            names = sorted(f.filename for f in synced)
            assert names == ["report.txt", "sub dir/a b.txt", "tabhere.txt"]
            rows = (
                (
                    await db.execute(
                        select(m.FileAttachment).where(m.FileAttachment.owner_id == run.id)
                    )
                )
                .scalars()
                .all()
            )
            assert {r.filename: r.size_bytes for r in rows}["sub dir/a b.txt"] == 4
            again = await sync_sandbox_output(
                db, tenant_id=tenant, run_id=run.id, driver=driver, handle=handle
            )
            assert again == []
    finally:
        await driver.teardown(handle)


async def test_a_sandbox_without_an_output_dir_syncs_nothing_quietly(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    driver = DockerSandboxDriver()
    handle = await driver.provision(SandboxSpec(image=_DEBIAN))
    try:
        tenant = uuid.uuid4()
        async with app_session(tenant) as db:
            run = await _run(db, tenant)
            assert (
                await sync_sandbox_output(
                    db, tenant_id=tenant, run_id=run.id, driver=driver, handle=handle
                )
                == []
            )
    finally:
        await driver.teardown(handle)


async def test_a_busybox_sandbox_fails_the_listing_loudly(
    app_session: AppSessionFactory, minio_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    driver = DockerSandboxDriver()
    handle = await driver.provision(SandboxSpec(image=_ALPINE))
    try:
        res = await driver.exec(
            handle, ["sh", "-c", "mkdir -p /workspace/output && echo x > /workspace/output/a.txt"]
        )
        assert res.exit_code == 0, res.output
        tenant = uuid.uuid4()
        async with app_session(tenant) as db:
            run = await _run(db, tenant)
            with caplog.at_level("WARNING", logger="oc8.runtime.workspace"):
                synced = await sync_sandbox_output(
                    db, tenant_id=tenant, run_id=run.id, driver=driver, handle=handle
                )
        assert synced == []
        assert "needs GNU find and head" in caplog.text
    finally:
        await driver.teardown(handle)
