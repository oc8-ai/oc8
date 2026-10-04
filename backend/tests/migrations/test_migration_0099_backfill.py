"""Migration 0099's v1 backfill computes a REAL payload for each pre-existing
agent -- the same one `oc8.agents.versioning.snapshot_agent` would compute
for it right now -- not a placeholder. `backfill_v1`/`_agent_payload`/
`_payload_hash` are exercised directly (loaded via `importlib`, since Alembic
revision modules have a leading digit in their filename and are not meant to
be imported by dotted path), against a raw sync connection that bypasses RLS
the way the migration itself runs -- and rolled back at the end, so the
shared test database is unchanged.

Why this matters (see the migration's own docstring): a placeholder hash
makes every migrated agent look permanently "dirty" to a later draft-status
diff, and a rollback to a fake v1 would silently disable real skill
assignments and delete real knowledge grants.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import uuid
from pathlib import Path

import sqlalchemy as sa

from oc8.agents.versioning import payload_hash

_PATH = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0099_agent_version.py"


def _module() -> object:
    spec = importlib.util.spec_from_file_location("oc8_migration_0099", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _department(conn: sa.Connection, tenant: uuid.UUID) -> uuid.UUID:
    dept_id = uuid.uuid4()
    conn.execute(
        sa.text(
            "INSERT INTO department (id, tenant_id, name, frame, presentation, "
            " prompt_caching_enabled, created_at, updated_at) "
            "VALUES (:id, :t, 'D', '{}', '{}', true, now(), now())"
        ),
        {"id": dept_id, "t": str(tenant)},
    )
    return dept_id


def _agent(
    conn: sa.Connection,
    tenant: uuid.UUID,
    department: uuid.UUID,
    *,
    mission: str = "answer tickets",
) -> uuid.UUID:
    agent_id = uuid.uuid4()
    conn.execute(
        sa.text(
            "INSERT INTO agent (id, tenant_id, department_id, name, role_title, mission, "
            " definition, narrowing, narrowing_overridden_keys, is_team_lead, "
            " is_tenant_assistant, status, trust_level, presentation, created_at, updated_at) "
            "VALUES (:id, :t, :d, 'A', 'Support', :mission, '{}', '{}', '[]', false, false, "
            " 'idle', 'first_party', '{}', now(), now())"
        ),
        {"id": agent_id, "t": str(tenant), "d": str(department), "mission": mission},
    )
    return agent_id


def _skill_assignment(
    conn: sa.Connection,
    tenant: uuid.UUID,
    *,
    agent_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    enabled: bool = True,
    deleted: bool = False,
) -> uuid.UUID:
    skill_version_id = uuid.uuid4()
    conn.execute(
        sa.text(
            "INSERT INTO skill_assignment (id, tenant_id, agent_id, department_id, "
            " skill_version_id, enabled, overrides, deleted_at, created_at, updated_at) "
            "VALUES (:id, :t, :a, :d, :sv, :en, '{}', :del, now(), now())"
        ),
        {
            "id": uuid.uuid4(),
            "t": str(tenant),
            "a": str(agent_id) if agent_id else None,
            "d": str(department_id) if department_id else None,
            "sv": str(skill_version_id),
            "en": enabled,
            "del": dt.datetime.now(dt.UTC) if deleted else None,
        },
    )
    return skill_version_id


def _knowledge_grant(
    conn: sa.Connection, tenant: uuid.UUID, *, grantee_type: str, grantee_id: uuid.UUID
) -> uuid.UUID:
    kb_id = uuid.uuid4()
    conn.execute(
        sa.text(
            "INSERT INTO knowledge_grant (id, tenant_id, kb_id, grantee_type, grantee_id, "
            " scope, created_at, updated_at) "
            "VALUES (:id, :t, :kb, :gt, :gi, '{}', now(), now())"
        ),
        {
            "id": uuid.uuid4(),
            "t": str(tenant),
            "kb": str(kb_id),
            "gt": grantee_type,
            "gi": str(grantee_id),
        },
    )
    return kb_id


def test_backfill_computes_the_real_payload_not_a_placeholder(pg_url: str) -> None:
    tenant = uuid.uuid4()

    engine = sa.create_engine(pg_url)
    with engine.connect() as conn:
        dept = _department(conn, tenant)
        mine = _agent(conn, tenant, dept, mission="answer support tickets")
        other = _agent(conn, tenant, dept)

        # Counts toward the payload: enabled, not soft-deleted, agent-scoped.
        mine_skill = _skill_assignment(conn, tenant, agent_id=mine)
        # Must NOT count: disabled, soft-deleted, department-wide, another agent's.
        _skill_assignment(conn, tenant, agent_id=mine, enabled=False)
        _skill_assignment(conn, tenant, agent_id=mine, deleted=True)
        _skill_assignment(conn, tenant, department_id=dept)
        _skill_assignment(conn, tenant, agent_id=other)

        mine_kb = _knowledge_grant(conn, tenant, grantee_type="agent", grantee_id=mine)
        # Must NOT count: department-wide, another agent's own grant.
        _knowledge_grant(conn, tenant, grantee_type="department", grantee_id=dept)
        _knowledge_grant(conn, tenant, grantee_type="agent", grantee_id=other)

        _module().backfill_v1(conn)  # type: ignore[attr-defined]

        version = conn.execute(
            sa.text(
                "SELECT payload, payload_hash, version_no FROM agent_version WHERE agent_id = :a"
            ),
            {"a": mine},
        ).one()
        current_version_id = conn.execute(
            sa.text("SELECT current_version_id FROM agent WHERE id = :a"), {"a": mine}
        ).scalar_one()
        conn.rollback()
    engine.dispose()

    payload = version.payload
    assert payload["mission"] == "answer support tickets"
    assert payload["skill_assignments"] == [{"skill_version_id": str(mine_skill)}]
    assert payload["knowledge_grants"] == [str(mine_kb)]
    assert version.version_no == 1
    assert current_version_id is not None

    # The hash is a REAL digest of the payload actually stored, not a
    # placeholder -- recomputing it independently with the same canonical
    # form `oc8.agents.versioning.payload_hash` uses must reproduce it byte
    # for byte.
    assert bytes(version.payload_hash) == payload_hash(payload)


def test_backfill_is_idempotent_and_skips_already_versioned_agents(pg_url: str) -> None:
    tenant = uuid.uuid4()

    engine = sa.create_engine(pg_url)
    with engine.connect() as conn:
        dept = _department(conn, tenant)
        agent_id = _agent(conn, tenant, dept)

        module = _module()
        module.backfill_v1(conn)  # type: ignore[attr-defined]
        module.backfill_v1(conn)  # type: ignore[attr-defined]

        count = conn.execute(
            sa.text("SELECT count(*) FROM agent_version WHERE agent_id = :a"), {"a": agent_id}
        ).scalar_one()
        conn.rollback()
    engine.dispose()

    assert count == 1
