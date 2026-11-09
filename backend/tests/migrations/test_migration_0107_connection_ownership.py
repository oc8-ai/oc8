"""Migration 0107: owners for OAuth connections and credentials, the creator of
a trigger, and the agent_tool_access table. Runs the helpers against a raw
connection inside a transaction and rolls back (PostgreSQL DDL is
transactional), so the shared test database is unchanged."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

_PATH = (
    Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0107_connection_ownership.py"
)


def _module() -> object:
    spec = importlib.util.spec_from_file_location("oc8_migration_0107", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _member(conn: sa.Connection, tenant: uuid.UUID) -> uuid.UUID:
    member_id = uuid.uuid4()
    conn.execute(
        sa.text(
            "INSERT INTO org_member (id, tenant_id, subject, subject_uuid, created_at, updated_at) "
            "VALUES (:id, :t, :s, :su, now(), now())"
        ),
        {"id": member_id, "t": tenant, "s": f"{member_id}@acme.test", "su": uuid.uuid4()},
    )
    return member_id


def _oauth(
    conn: sa.Connection, tenant: uuid.UUID, label: str, owner: uuid.UUID | None = None
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO oauth_connection (id, tenant_id, provider, account_label, scopes, "
            "access_secret_ref, status, client_source, grant_type, provider_metadata, "
            "owner_member_id, created_at, updated_at) VALUES (:id, :t, 'microsoft', :l, '{}', '', "
            "'active', 'platform', 'authorization_code', '{}', :o, now(), now())"
        ),
        {"id": uuid.uuid4(), "t": tenant, "l": label, "o": owner},
    )


def test_the_revision_follows_0106() -> None:
    module = _module()
    assert module.revision == "0107"  # type: ignore[attr-defined]
    assert module.down_revision == "0106"  # type: ignore[attr-defined]


def test_existing_rows_stay_organisation_owned(pg_url: str) -> None:
    tenant = uuid.uuid4()
    engine = sa.create_engine(pg_url)
    with engine.connect() as conn:
        _module().remove_connection_ownership(conn)  # type: ignore[attr-defined]
        conn.execute(
            sa.text(
                "INSERT INTO oauth_connection (id, tenant_id, provider, account_label, scopes, "
                "access_secret_ref, status, client_source, grant_type, provider_metadata, "
                "created_at, updated_at) VALUES (:id, :t, 'google', 'old@acme.test', '{}', '', "
                "'active', 'platform', 'authorization_code', '{}', now(), now())"
            ),
            {"id": uuid.uuid4(), "t": tenant},
        )
        conn.execute(
            sa.text(
                "INSERT INTO credential (id, tenant_id, name, credential_type, field_values, "
                "secret_refs, created_at, updated_at) VALUES (:id, :t, 'Odoo', 'odoo_login', "
                "'{}', '{}', now(), now())"
            ),
            {"id": uuid.uuid4(), "t": tenant},
        )
        _module().add_connection_ownership(conn)  # type: ignore[attr-defined]
        owners = (
            conn.execute(
                sa.text(
                    "SELECT owner_member_id FROM oauth_connection WHERE tenant_id = :t "
                    "UNION ALL SELECT owner_member_id FROM credential WHERE tenant_id = :t"
                ),
                {"t": tenant},
            )
            .scalars()
            .all()
        )
        conn.rollback()
    engine.dispose()
    assert owners == [None, None]


def test_organisation_labels_stay_unique_and_each_person_gets_their_own(pg_url: str) -> None:
    tenant = uuid.uuid4()
    engine = sa.create_engine(pg_url)
    with engine.connect() as conn:
        anna, ben = _member(conn, tenant), _member(conn, tenant)
        _oauth(conn, tenant, "shared@acme.test")
        _oauth(conn, tenant, "shared@acme.test", anna)  # a person may connect the same account
        _oauth(conn, tenant, "shared@acme.test", ben)
        nested = conn.begin_nested()
        with pytest.raises(sa.exc.IntegrityError):
            _oauth(conn, tenant, "shared@acme.test")  # a second organisation row: refused as before
        nested.rollback()
        nested = conn.begin_nested()
        with pytest.raises(sa.exc.IntegrityError):
            _oauth(conn, tenant, "shared@acme.test", anna)  # the same person twice: refused
        nested.rollback()
        conn.rollback()
    engine.dispose()


def test_an_owner_from_another_tenant_is_refused(pg_url: str) -> None:
    tenant, other = uuid.uuid4(), uuid.uuid4()
    engine = sa.create_engine(pg_url)
    with engine.connect() as conn:
        stranger = _member(conn, other)
        with pytest.raises(sa.exc.IntegrityError, match="same tenant"):
            _oauth(conn, tenant, "x@acme.test", stranger)
        conn.rollback()
    engine.dispose()


def _credential(
    conn: sa.Connection, tenant: uuid.UUID, name: str, owner: uuid.UUID | None = None
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO credential (id, tenant_id, name, credential_type, field_values, "
            "secret_refs, owner_member_id, created_at, updated_at) VALUES (:id, :t, :n, "
            "'odoo_login', '{}', '{}', :o, now(), now())"
        ),
        {"id": uuid.uuid4(), "t": tenant, "n": name, "o": owner},
    )


def test_downgrade_drops_personal_rows_and_the_round_trip_works(pg_url: str) -> None:
    tenant = uuid.uuid4()
    engine = sa.create_engine(pg_url)
    with engine.connect() as conn:
        anna = _member(conn, tenant)
        _oauth(conn, tenant, "shared@acme.test")
        _oauth(conn, tenant, "shared@acme.test", anna)
        _credential(conn, tenant, "Odoo")
        _credential(conn, tenant, "Odoo", anna)
        _module().remove_connection_ownership(conn)  # type: ignore[attr-defined]
        left = conn.execute(
            sa.text(
                "SELECT count(*) FROM oauth_connection WHERE tenant_id = :t AND account_label = "
                "'shared@acme.test'"
            ),
            {"t": tenant},
        ).scalar_one()
        creds = conn.execute(
            sa.text("SELECT count(*) FROM credential WHERE tenant_id = :t"), {"t": tenant}
        ).scalar_one()
        _module().add_connection_ownership(conn)  # type: ignore[attr-defined]
        owners = (
            conn.execute(
                sa.text("SELECT owner_member_id FROM oauth_connection WHERE tenant_id = :t"),
                {"t": tenant},
            )
            .scalars()
            .all()
        )
        conn.rollback()
    engine.dispose()
    # only the organisation rows survive; none became organisation-owned by accident
    assert (left, creds, owners) == (1, 1, [None])


def test_new_reference_columns_are_indexed(pg_url: str) -> None:
    engine = sa.create_engine(pg_url)
    with engine.connect() as conn:
        indexed = set(
            conn.execute(
                sa.text(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                    "AND a.attnum = ANY(i.indkey) WHERE i.indrelid IN "
                    "('agent_tool_access'::regclass, 'trigger'::regclass)"
                )
            )
            .scalars()
            .all()
        )
    engine.dispose()
    assert {
        "oauth_connection_id",
        "credential_id",
        "set_by_member_id",
        "created_by_member_id",
    } <= indexed
