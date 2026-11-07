"""Migration 0106 adds `org_member.onboarding_status`. Everybody already in a
workspace is 'completed' (nobody is sent through a welcome they never needed);
rows created afterwards start 'pending'. Runs `add_onboarding_status` against a
raw connection inside a transaction and rolls back, so the shared test database
is unchanged (PostgreSQL DDL is transactional).
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0106_org_member_onboarding_status.py"
)


def _module() -> object:
    spec = importlib.util.spec_from_file_location("oc8_migration_0106", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _member(conn: sa.Connection, tenant: uuid.UUID, subject: str) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO org_member (id, tenant_id, subject, subject_uuid, created_at, updated_at) "
            "VALUES (:id, :t, :s, :su, now(), now())"
        ),
        {"id": uuid.uuid4(), "t": tenant, "s": subject, "su": uuid.uuid4()},
    )


def _status(conn: sa.Connection, tenant: uuid.UUID, subject: str) -> str:
    return conn.execute(
        sa.text("SELECT onboarding_status FROM org_member WHERE tenant_id = :t AND subject = :s"),
        {"t": tenant, "s": subject},
    ).scalar_one()


def test_the_revision_follows_0105() -> None:
    module = _module()
    assert module.revision == "0106"  # type: ignore[attr-defined]
    assert module.down_revision == "0105"  # type: ignore[attr-defined]


def test_existing_members_are_completed_and_new_ones_pending(pg_url: str) -> None:
    tenant = uuid.uuid4()
    engine = sa.create_engine(pg_url)
    with engine.connect() as conn:
        # Back to the schema before 0106, inside this transaction only.
        conn.execute(
            sa.text("ALTER TABLE org_member DROP CONSTRAINT ck_org_member_onboarding_status")
        )
        conn.execute(sa.text("ALTER TABLE org_member DROP COLUMN onboarding_status"))
        _member(conn, tenant, "already-here@acme.test")

        _module().add_onboarding_status(conn)  # type: ignore[attr-defined]
        _member(conn, tenant, "invited-later@acme.test")

        before = _status(conn, tenant, "already-here@acme.test")
        after = _status(conn, tenant, "invited-later@acme.test")
        conn.rollback()
    engine.dispose()

    assert (before, after) == ("completed", "pending")


def test_an_unknown_status_is_refused(pg_url: str) -> None:
    tenant = uuid.uuid4()
    engine = sa.create_engine(pg_url)
    with engine.connect() as conn:
        _member(conn, tenant, "a@acme.test")
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text("UPDATE org_member SET onboarding_status = 'later' WHERE tenant_id = :t"),
                {"t": tenant},
            )
        conn.rollback()
    engine.dispose()
