"""0134: columns for split confirmations and a held department frame."""

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


def _cfg() -> Config:
    return Config(str(_ROOT / "alembic.ini"))


def _module() -> ModuleType:
    (path,) = (_ROOT / "migrations" / "versions").glob("*_personal_login_binding.py")
    spec = importlib.util.spec_from_file_location("oc8_migration_personal_login_binding", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(dsn: str, table: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (table,),
        ).fetchall()
    return {row[0] for row in rows}


def _insert_department(
    conn: psycopg.Connection,
    *,
    kind: str,
    owner: uuid.UUID | None,
    pending_frame_tools: str | None,
) -> None:
    frame = '{"closed": true}' if kind == "personal" else "{}"
    conn.execute(
        "INSERT INTO department (id, tenant_id, name, frame, presentation, "
        "prompt_caching_enabled, created_at, updated_at, kind, owner_member_id, "
        "pending_frame_tools) "
        "VALUES (%s, %s, 'D', %s, '{}', true, now(), now(), %s, %s, %s)",
        (uuid.uuid4(), uuid.uuid4(), frame, kind, owner, pending_frame_tools),
    )


def test_upgrade_adds_the_columns_and_downgrade_drops_them(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    down_revision = _module().down_revision
    command.downgrade(cfg, down_revision)
    try:
        assert "pending_frame_tools" not in _columns(dsn, "department")
    finally:
        command.upgrade(cfg, "head")

    assert "binding_split" in _columns(dsn, "agent_tool_access")
    assert "access_split" in _columns(dsn, "trigger")
    assert "pending_frame_tools" in _columns(dsn, "department")
    with psycopg.connect(dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_department(
                conn, kind="standard", owner=None, pending_frame_tools='{"tools": {}}'
            )
        _insert_department(
            conn, kind="personal", owner=uuid.uuid4(), pending_frame_tools='{"tools": {}}'
        )
        _insert_department(conn, kind="standard", owner=None, pending_frame_tools=None)

    command.downgrade(cfg, down_revision)
    try:
        assert "pending_frame_tools" not in _columns(dsn, "department")
        assert "binding_split" not in _columns(dsn, "agent_tool_access")
        assert "access_split" not in _columns(dsn, "trigger")
    finally:
        command.upgrade(cfg, "head")
