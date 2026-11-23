"""0133: a department has a kind, an owner and override stamps."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

import psycopg
import pytest
from alembic import command
from alembic.config import Config

_ROOT = Path(__file__).resolve().parents[2]
_NEW = {"kind", "owner_member_id", "frame_overridden_at", "budget_overridden_at"}


def _cfg() -> Config:
    return Config(str(_ROOT / "alembic.ini"))


def _module() -> ModuleType:
    (path,) = (_ROOT / "migrations" / "versions").glob("*_personal_departments.py")
    spec = importlib.util.spec_from_file_location("oc8_migration_personal_departments", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'department'"
        ).fetchall()
    return {row[0] for row in rows}


def _insert(
    conn: psycopg.Connection, tenant: uuid.UUID, sql_extra: str, *values: object
) -> uuid.UUID:
    dept_id = uuid.uuid4()
    cols, marks = ("", "")
    if sql_extra:
        cols = ", " + sql_extra
        marks = ", " + ", ".join(["%s"] * len(values))
    conn.execute(
        "INSERT INTO department (id, tenant_id, name, frame, presentation, "
        f"prompt_caching_enabled, created_at, updated_at{cols}) "
        f"VALUES (%s, %s, 'D', '{{\"closed\": true}}', '{{}}', true, now(), now(){marks})",
        (dept_id, tenant, *values),
    )
    return dept_id


def test_upgrade_folds_the_flag_and_downgrade_restores_it(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    module = _module()
    down_revision = module.down_revision
    tenant = uuid.uuid4()
    owner = uuid.uuid4()
    command.downgrade(cfg, down_revision)
    try:
        assert "is_assistant_department" in _columns(dsn)
        assert not _NEW & _columns(dsn)
        with psycopg.connect(dsn, autocommit=True) as conn:
            assistant = _insert(conn, tenant, "is_assistant_department", True)
            ordinary = _insert(conn, tenant, "")
    finally:
        command.upgrade(cfg, "head")

    columns = _columns(dsn)
    assert _NEW <= columns
    assert "is_assistant_department" not in columns
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute("SELECT id, kind FROM department").fetchall()
        kinds: dict[object, str] = dict(rows)
        assert kinds[assistant] == "assistant"
        assert kinds[ordinary] == "standard"

        with pytest.raises(psycopg.errors.CheckViolation):
            _insert(conn, tenant, "kind", "personal")
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert(conn, tenant, "kind, owner_member_id", "standard", owner)
        personal = _insert(conn, tenant, "kind, owner_member_id", "personal", owner)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert(conn, tenant, "kind, owner_member_id", "personal", owner)

    command.downgrade(cfg, down_revision)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            flag = conn.execute(
                "SELECT is_assistant_department FROM department WHERE id = %s", (assistant,)
            ).fetchone()
            ordinary_flag = conn.execute(
                "SELECT is_assistant_department FROM department WHERE id = %s", (ordinary,)
            ).fetchone()
            deleted = conn.execute(
                "SELECT deleted_at FROM department WHERE id = %s", (personal,)
            ).fetchone()
        assert flag is not None and flag[0] is True
        assert ordinary_flag is not None and ordinary_flag[0] is False
        assert deleted is not None and deleted[0] is not None
        assert not _NEW & _columns(dsn)
    finally:
        command.upgrade(cfg, "head")


def test_the_owner_index_ignores_soft_deleted_rows_and_other_tenants(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    tenant, other_tenant, owner = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        first = _insert(conn, tenant, "kind, owner_member_id", "personal", owner)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert(conn, tenant, "kind, owner_member_id", "personal", owner)
        # The same owner id in another tenant is a different person's row.
        _insert(conn, other_tenant, "kind, owner_member_id", "personal", owner)
        # A soft-deleted row frees the slot.
        conn.execute("UPDATE department SET deleted_at = now() WHERE id = %s", (first,))
        _insert(conn, tenant, "kind, owner_member_id", "personal", owner)
