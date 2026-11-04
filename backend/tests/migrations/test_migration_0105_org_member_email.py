"""Migration 0105 adds `org_member.email` and backfills it for local sign-in,
where the subject IS the address. A subject that is not address-shaped (an
OIDC `sub` uuid, a dev-login name) stays NULL: showing it as an email is the
bug this column exists to fix. Runs `backfill_email` directly against a raw
connection and rolls back, so the shared test database is unchanged.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import sqlalchemy as sa

_PATH = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0105_org_member_email.py"


def _module() -> object:
    spec = importlib.util.spec_from_file_location("oc8_migration_0105", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _member(conn: sa.Connection, tenant: uuid.UUID, subject: str, email: str | None = None) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO org_member (id, tenant_id, subject, subject_uuid, email, "
            " created_at, updated_at) VALUES (:id, :t, :s, :su, :e, now(), now())"
        ),
        {"id": uuid.uuid4(), "t": tenant, "s": subject, "su": uuid.uuid4(), "e": email},
    )


def test_the_revision_follows_0104() -> None:
    module = _module()
    assert module.revision == "0105"  # type: ignore[attr-defined]
    assert module.down_revision == "0104"  # type: ignore[attr-defined]


def test_backfill_copies_only_address_shaped_subjects(pg_url: str) -> None:
    tenant = uuid.uuid4()
    engine = sa.create_engine(pg_url)
    with engine.connect() as conn:
        _member(conn, tenant, "ana@acme.test")
        _member(conn, tenant, "5f0c2a8e-3b1d-4c7e-9a64-2d8f1e0b7c35")
        _member(conn, tenant, "alice")
        _member(conn, tenant, "ben@acme.test", email="ben.new@acme.test")

        _module().backfill_email(conn)  # type: ignore[attr-defined]

        rows: dict[str, str | None] = {
            subject: email
            for subject, email in conn.execute(
                sa.text("SELECT subject, email FROM org_member WHERE tenant_id = :t"),
                {"t": tenant},
            ).all()
        }
        conn.rollback()
    engine.dispose()

    assert rows == {
        "ana@acme.test": "ana@acme.test",
        "5f0c2a8e-3b1d-4c7e-9a64-2d8f1e0b7c35": None,
        "alice": None,
        # An address the identity provider already reported is not overwritten.
        "ben@acme.test": "ben.new@acme.test",
    }
