"""Tests for the restore service (design doc §5, §8): single-transaction
delete-then-insert with tenant rewrite."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy import NullPool, delete, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from testcontainers.postgres import PostgresContainer

from oc8 import models as m
from oc8.audit import append_event
from oc8.backup.archive import export_archive
from oc8.backup.secrets_envelope import WrongPassphrase
from oc8.backup.service import preview_restore, restore_from_archive
from oc8.secrets.service import resolve_secret, store_secret
from tests.audit.helpers import flush_audit
from tests.conftest import _ROLE_BOOTSTRAP, AppSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    """`store_secret`/`resolve_secret` need a `secret_kek`. Set via the real
    env var (not `monkeypatch.setattr` on the cached settings object, as
    `test_archive_writer.py` does) because `other_instance` below calls
    `get_settings.cache_clear()` to point settings at a second database --
    an attribute patched onto the old cached object would not survive that,
    silently dropping the KEK for every test that uses `other_instance`."""
    import base64

    from oc8.config import get_settings

    monkeypatch.setenv("OC8_SECRET_KEK", base64.b64encode(bytes(range(32))).decode())
    get_settings.cache_clear()


async def _seed_tenant(app_session: AppSessionFactory, tenant_id: uuid.UUID, *, slug: str) -> None:
    """A real Organization, a Department, an Agent, and an AgentRun -- enough
    for the round-trip, the evidence-state, and the dependency-order
    behaviour to all be meaningful."""
    async with app_session(tenant_id) as db:
        db.add(m.Organization(id=tenant_id, slug=slug, name=f"{slug} GmbH", region="eu"))
        await db.flush()
        dept = m.Department(tenant_id=tenant_id, name="Vertrieb", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant_id,
            department_id=dept.id,
            name="Nora",
            narrowing={},
            definition={},
            presentation={},
        )
        db.add(agent)
        await db.flush()
        db.add(m.AgentRun(tenant_id=tenant_id, agent_id=agent.id, state="queued"))


@pytest.fixture
async def acme_tenant(app_session: AppSessionFactory) -> uuid.UUID:
    """A fresh tenant per test, not the shared `ACME_TENANT_ID` constant --
    row counts here must be exact."""
    tenant_id = uuid.uuid4()
    await _seed_tenant(app_session, tenant_id, slug=f"acme-{tenant_id.hex[:8]}")
    return tenant_id


@pytest.fixture
def other_instance(_pg: PostgresContainer) -> Iterator[AppSessionFactory]:
    """A second, freshly-migrated, empty database on the same Postgres
    server -- standing in for "a different oc8 instance", the real target of
    a portable cross-tenant restore (design doc §5.1).

    `agent.id` and friends are single-column, tenant-agnostic primary keys
    (see `oc8/models/_mixins.py::PkMixin`), so "an archive from another
    instance cannot collide with a row that already exists here" is only
    actually true when source and target do not share a row-id space --
    which a second *tenant* in the SAME database provably does not give us
    (a still-live source row and a restored copy of it, differing only in
    `tenant_id`, collide on the bare `id` primary key). A second physical
    database is the cheapest thing that genuinely provides that separate
    id space, without spinning up a whole second container.
    """
    host = _pg.get_container_host_ip()
    port = _pg.get_exposed_port(5432)
    su, pw = _pg.username, _pg.password
    dbname = f"oc8_other_{uuid.uuid4().hex[:12]}"
    admin_url = f"postgresql://{su}:{pw}@{host}:{port}/postgres"

    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{dbname}"')
    try:
        migrate_url = f"postgresql+psycopg://oc8_migrate:oc8@{host}:{port}/{dbname}"
        app_url = f"postgresql+asyncpg://oc8_app:oc8@{host}:{port}/{dbname}"
        # `oc8_migrate`/`oc8_app` are server-wide roles (already created by the
        # `_pg`/`settings_env` bootstrap against "oc8"), but schema ownership
        # and privileges are per-database -- so this new database needs its
        # own run of the exact same bootstrap SQL `settings_env` uses.
        db_url = f"postgresql://{su}:{pw}@{host}:{port}/{dbname}"
        with psycopg.connect(db_url, autocommit=True) as conn:
            conn.execute(_ROLE_BOOTSTRAP)

        from alembic import command
        from alembic.config import Config

        from oc8.config import get_settings

        prior_migration_url = os.environ.get("OC8_MIGRATION_URL")
        prior_database_url = os.environ.get("OC8_DATABASE_URL")
        os.environ["OC8_MIGRATION_URL"] = migrate_url
        os.environ["OC8_DATABASE_URL"] = app_url
        get_settings.cache_clear()
        try:
            cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
            command.upgrade(cfg, "head")
        finally:
            if prior_migration_url is not None:
                os.environ["OC8_MIGRATION_URL"] = prior_migration_url
            else:
                os.environ.pop("OC8_MIGRATION_URL", None)
            if prior_database_url is not None:
                os.environ["OC8_DATABASE_URL"] = prior_database_url
            else:
                os.environ.pop("OC8_DATABASE_URL", None)
            get_settings.cache_clear()

        @asynccontextmanager
        async def _open(tenant_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
            engine = create_async_engine(app_url, poolclass=NullPool)
            try:
                async with AsyncSession(engine, expire_on_commit=False) as s:
                    await s.execute(
                        text("SELECT set_config('app.tenant_id', :t, true)"),
                        {"t": str(tenant_id)},
                    )
                    try:
                        yield s
                        await s.commit()
                    except Exception:
                        await s.rollback()
                        raise
            finally:
                await engine.dispose()

        yield _open
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


@pytest.fixture
async def other_tenant(other_instance: AppSessionFactory) -> uuid.UUID:
    """A tenant living on the *other* instance -- the target of a genuinely
    cross-instance restore."""
    tenant_id = uuid.uuid4()
    await _seed_tenant(other_instance, tenant_id, slug=f"globex-{tenant_id.hex[:8]}")
    return tenant_id


@pytest.fixture
def actor() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
async def db_session(
    app_session: AppSessionFactory, acme_tenant: uuid.UUID
) -> AsyncIterator[AsyncSession]:
    async with app_session(acme_tenant) as session:
        yield session


async def test_round_trip_restores_the_pre_mutation_state(
    db_session: AsyncSession, acme_tenant: uuid.UUID, actor: uuid.UUID
) -> None:
    _, backup = await export_archive(db_session, tenant_id=acme_tenant, passphrase=None)
    before = (
        await db_session.execute(m.Agent.__table__.select().where(m.Agent.tenant_id == acme_tenant))
    ).all()

    await db_session.execute(delete(m.Agent).where(m.Agent.tenant_id == acme_tenant))

    result = await restore_from_archive(
        db_session,
        tenant_id=acme_tenant,
        archive_bytes=backup,
        passphrase=None,
        actor_type="human",
        actor_id=actor,
    )
    assert result.tables.get("agent", 0) == len(before)

    after = (
        await db_session.execute(m.Agent.__table__.select().where(m.Agent.tenant_id == acme_tenant))
    ).all()
    assert {row.id for row in after} == {row.id for row in before}


async def test_cross_tenant_restore_rewrites_tenant_id_and_leaves_source_untouched(
    app_session: AppSessionFactory,
    other_instance: AppSessionFactory,
    acme_tenant: uuid.UUID,
    other_tenant: uuid.UUID,
    actor: uuid.UUID,
) -> None:
    """Restoring into `other_tenant` runs on the *other instance*'s own
    RLS-bound session -- exactly like `export_archive`, the tenant isolation
    policy scopes both the read and the write to the session's own
    `app.tenant_id` GUC (migration 0001), not to the `tenant_id` kwarg
    alone."""
    async with app_session(acme_tenant) as source_db:
        _, backup = await export_archive(source_db, tenant_id=acme_tenant, passphrase=None)
        before_source = (
            await source_db.execute(
                m.Agent.__table__.select().where(m.Agent.tenant_id == acme_tenant)
            )
        ).all()

    async with other_instance(other_tenant) as target_db:
        await restore_from_archive(
            target_db,
            tenant_id=other_tenant,
            archive_bytes=backup,
            passphrase=None,
            actor_type="human",
            actor_id=actor,
        )
        restored = (
            await target_db.execute(
                m.Agent.__table__.select().where(m.Agent.tenant_id == other_tenant)
            )
        ).all()

    assert restored and all(row.tenant_id == other_tenant for row in restored)
    assert {row.id for row in restored} == {row.id for row in before_source}

    async with app_session(acme_tenant) as source_db_after:
        still_there = (
            await source_db_after.execute(
                m.Agent.__table__.select().where(m.Agent.tenant_id == acme_tenant)
            )
        ).all()
    assert {row.id for row in still_there} == {row.id for row in before_source}


async def test_wrong_passphrase_writes_nothing(
    db_session: AsyncSession, acme_tenant: uuid.UUID, actor: uuid.UUID
) -> None:
    await store_secret(db_session, tenant_id=acme_tenant, name="odoo/password", value="hunter2")
    _, backup = await export_archive(db_session, tenant_id=acme_tenant, passphrase="right")
    before = (
        await db_session.execute(m.Agent.__table__.select().where(m.Agent.tenant_id == acme_tenant))
    ).all()
    with pytest.raises(WrongPassphrase):
        await restore_from_archive(
            db_session,
            tenant_id=acme_tenant,
            archive_bytes=backup,
            passphrase="wrong",
            actor_type="human",
            actor_id=actor,
        )
    after = (
        await db_session.execute(m.Agent.__table__.select().where(m.Agent.tenant_id == acme_tenant))
    ).all()
    assert {row.id for row in after} == {row.id for row in before}
    # The existing secret must survive too: a failed decrypt must not touch
    # `secret`/`tenant_dek` at all.
    assert await resolve_secret(db_session, tenant_id=acme_tenant, ref="odoo/password") == (
        "hunter2"
    )


async def test_agent_run_evidence_state_is_forced_to_none_on_restore(
    db_session: AsyncSession, acme_tenant: uuid.UUID, actor: uuid.UUID
) -> None:
    _, backup = await export_archive(db_session, tenant_id=acme_tenant, passphrase=None)
    await restore_from_archive(
        db_session,
        tenant_id=acme_tenant,
        archive_bytes=backup,
        passphrase=None,
        actor_type="human",
        actor_id=actor,
    )
    rows = (
        await db_session.execute(
            m.AgentRun.__table__.select().where(m.AgentRun.tenant_id == acme_tenant)
        )
    ).all()
    assert rows and all(row.evidence_state == "none" for row in rows)


async def test_audit_events_survive_a_restore_and_a_new_one_is_appended(
    db_session: AsyncSession, acme_tenant: uuid.UUID, actor: uuid.UUID
) -> None:
    await append_event(
        db_session,
        tenant_id=acme_tenant,
        actor_type="human",
        actor_id=actor,
        category="agent",
        action="agent.created",
        resource={"name": "Nora"},
    )
    await flush_audit(db_session)  # staged audit events reach the chain at commit
    before_count = (
        await db_session.execute(
            m.AuditEvent.__table__.select().where(m.AuditEvent.tenant_id == acme_tenant)
        )
    ).all()
    assert len(before_count) == 1

    _, backup = await export_archive(db_session, tenant_id=acme_tenant, passphrase=None)
    await restore_from_archive(
        db_session,
        tenant_id=acme_tenant,
        archive_bytes=backup,
        passphrase=None,
        actor_type="human",
        actor_id=actor,
    )
    await flush_audit(db_session)
    after = (
        await db_session.execute(
            m.AuditEvent.__table__.select()
            .where(m.AuditEvent.tenant_id == acme_tenant)
            .order_by(m.AuditEvent.seq.asc())
        )
    ).all()
    assert len(after) == len(before_count) + 1
    assert after[0].action == "agent.created"
    assert after[-1].action == "restore" and after[-1].category == "backup"


async def test_preview_writes_nothing_and_reports_row_counts(
    db_session: AsyncSession, acme_tenant: uuid.UUID
) -> None:
    _, backup = await export_archive(db_session, tenant_id=acme_tenant, passphrase=None)
    preview = await preview_restore(db_session, tenant_id=acme_tenant, archive_bytes=backup)
    assert preview.problems == []
    assert preview.has_secrets is False
    assert "agent" in preview.table_counts
    assert preview.table_counts["agent"]["archive"] == preview.table_counts["agent"]["current"]

    still_there = (
        await db_session.execute(m.Agent.__table__.select().where(m.Agent.tenant_id == acme_tenant))
    ).all()
    assert len(still_there) == 1


async def test_archive_with_no_secrets_leaves_targets_existing_secrets_untouched(
    db_session: AsyncSession, acme_tenant: uuid.UUID, actor: uuid.UUID
) -> None:
    await store_secret(db_session, tenant_id=acme_tenant, name="odoo/password", value="hunter2")
    _, backup = await export_archive(db_session, tenant_id=acme_tenant, passphrase=None)

    result = await restore_from_archive(
        db_session,
        tenant_id=acme_tenant,
        archive_bytes=backup,
        passphrase=None,
        actor_type="human",
        actor_id=actor,
    )
    assert result.secrets_restored == 0
    assert await resolve_secret(db_session, tenant_id=acme_tenant, ref="odoo/password") == (
        "hunter2"
    )


async def test_secrets_are_restored_under_the_target_tenants_own_dek(
    app_session: AppSessionFactory,
    other_instance: AppSessionFactory,
    acme_tenant: uuid.UUID,
    other_tenant: uuid.UUID,
    actor: uuid.UUID,
) -> None:
    async with app_session(acme_tenant) as source_db:
        await store_secret(source_db, tenant_id=acme_tenant, name="odoo/password", value="hunter2")
        _, backup = await export_archive(source_db, tenant_id=acme_tenant, passphrase="op-pass")

    async with other_instance(other_tenant) as target_db:
        result = await restore_from_archive(
            target_db,
            tenant_id=other_tenant,
            archive_bytes=backup,
            passphrase="op-pass",
            actor_type="human",
            actor_id=actor,
        )
        assert result.secrets_restored == 1
        assert await resolve_secret(target_db, tenant_id=other_tenant, ref="odoo/password") == (
            "hunter2"
        )


async def test_organization_name_and_region_update_but_id_slug_tier_do_not(
    app_session: AppSessionFactory,
    other_instance: AppSessionFactory,
    acme_tenant: uuid.UUID,
    other_tenant: uuid.UUID,
    actor: uuid.UUID,
) -> None:
    async with other_instance(other_tenant) as target_db:
        before_org = await target_db.get(m.Organization, other_tenant)
        assert before_org is not None
        before_slug, before_tier, before_id = before_org.slug, before_org.tier, before_org.id

    async with app_session(acme_tenant) as source_db:
        _, backup = await export_archive(source_db, tenant_id=acme_tenant, passphrase=None)
        source_org = await source_db.get(m.Organization, acme_tenant)
        assert source_org is not None
        source_name, source_region = source_org.name, source_org.region

    async with other_instance(other_tenant) as target_db2:
        await restore_from_archive(
            target_db2,
            tenant_id=other_tenant,
            archive_bytes=backup,
            passphrase=None,
            actor_type="human",
            actor_id=actor,
        )
        after_org = await target_db2.get(m.Organization, other_tenant)
        assert after_org is not None
        assert after_org.name == source_name
        assert after_org.region == source_region
        assert after_org.slug == before_slug
        assert after_org.tier == before_tier
        assert after_org.id == before_id


async def test_restore_result_excludes_secret_tables_only_when_the_archive_carried_none(
    db_session: AsyncSession, acme_tenant: uuid.UUID, actor: uuid.UUID
) -> None:
    """Final review finding: `RestoreResult.excluded` must not claim `secret`/
    `tenant_dek` are "not restored" beside a `secrets_restored` count above
    zero -- that directly contradicts what the restore just did. An archive
    with no `secrets.json` at all still excludes them (there is nothing to
    restore through that channel), but one that carried and decrypted
    credentials must not."""
    _, without_secrets = await export_archive(db_session, tenant_id=acme_tenant, passphrase=None)
    result = await restore_from_archive(
        db_session,
        tenant_id=acme_tenant,
        archive_bytes=without_secrets,
        passphrase=None,
        actor_type="human",
        actor_id=actor,
    )
    assert result.secrets_restored == 0
    assert "secret" in result.excluded and "tenant_dek" in result.excluded
    # What genuinely never travels must still be named.
    assert "audit_event" in result.excluded and "blobs" in result.excluded

    await store_secret(db_session, tenant_id=acme_tenant, name="odoo/password", value="hunter2")
    _, with_secrets = await export_archive(
        db_session, tenant_id=acme_tenant, passphrase="op-passphrase"
    )
    result = await restore_from_archive(
        db_session,
        tenant_id=acme_tenant,
        archive_bytes=with_secrets,
        passphrase="op-passphrase",
        actor_type="human",
        actor_id=actor,
    )
    assert result.secrets_restored == 1
    assert "secret" not in result.excluded and "tenant_dek" not in result.excluded
    assert "audit_event" in result.excluded and "blobs" in result.excluded


async def test_restore_is_all_or_nothing_on_a_failure_partway_through_inserts(
    app_session: AppSessionFactory,
    acme_tenant: uuid.UUID,
    actor: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forced failure after the first table's insert has already gone out,
    but before the rest, must leave the tenant byte-identical to before the
    restore was attempted -- no agent from the archive, no half-applied
    department, nothing. One rollback undoes everything because the service
    never commits."""
    async with app_session(acme_tenant) as seed_db:
        _, backup = await export_archive(seed_db, tenant_id=acme_tenant, passphrase=None)

    async with app_session(acme_tenant) as before_db:
        before_depts = (
            await before_db.execute(
                m.Department.__table__.select().where(m.Department.tenant_id == acme_tenant)
            )
        ).all()
        before_agents = (
            await before_db.execute(
                m.Agent.__table__.select().where(m.Agent.tenant_id == acme_tenant)
            )
        ).all()
        before_runs = (
            await before_db.execute(
                m.AgentRun.__table__.select().where(m.AgentRun.tenant_id == acme_tenant)
            )
        ).all()

    import oc8.backup.service as svc

    real_insert: Any = svc.insert  # type: ignore[attr-defined]
    calls = {"n": 0}

    def _flaky_insert(table: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("synthetic failure partway through the restore's inserts")
        return real_insert(table)

    monkeypatch.setattr(svc, "insert", _flaky_insert)

    with pytest.raises(RuntimeError, match="synthetic failure"):
        async with app_session(acme_tenant) as db:
            await restore_from_archive(
                db,
                tenant_id=acme_tenant,
                archive_bytes=backup,
                passphrase=None,
                actor_type="human",
                actor_id=actor,
            )
    assert calls["n"] >= 2  # the second table's insert really was attempted

    async with app_session(acme_tenant) as after_db:
        after_depts = (
            await after_db.execute(
                m.Department.__table__.select().where(m.Department.tenant_id == acme_tenant)
            )
        ).all()
        after_agents = (
            await after_db.execute(
                m.Agent.__table__.select().where(m.Agent.tenant_id == acme_tenant)
            )
        ).all()
        after_runs = (
            await after_db.execute(
                m.AgentRun.__table__.select().where(m.AgentRun.tenant_id == acme_tenant)
            )
        ).all()

    assert after_depts == before_depts
    assert after_agents == before_agents
    assert after_runs == before_runs
