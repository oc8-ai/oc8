"""0131: a skill records its shipped version and whether it was customised; an
existing store skill's current version becomes its shipped one."""

from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config

_COLUMNS = {"shipped_version_id", "customized_at", "shipped_update_at"}


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def _columns(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'skill'"
        ).fetchall()
    return {row[0] for row in rows}


def test_upgrade_adds_the_columns_backfills_store_skills_and_downgrade_removes_them(
    pg_url: str,
) -> None:
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    tenant = uuid.uuid4()
    store, local = uuid.uuid4(), uuid.uuid4()
    store_version, local_version = uuid.uuid4(), uuid.uuid4()
    command.downgrade(cfg, "0129")
    try:
        assert not _COLUMNS & _columns(dsn)
        with psycopg.connect(dsn) as conn:
            for skill_id, origin, version_id in (
                (store, "store", store_version),
                (local, "local", local_version),
            ):
                conn.execute(
                    "INSERT INTO skill (id, tenant_id, name, description, author, origin, "
                    "trust_level, current_version_id) "
                    "VALUES (%s, %s, %s, '', 'x', %s, 'first_party', %s)",
                    (skill_id, tenant, f"s-{skill_id.hex[:6]}", origin, version_id),
                )
            conn.commit()
    finally:
        command.upgrade(cfg, "head")
    assert _COLUMNS <= _columns(dsn)
    with psycopg.connect(dsn) as conn:
        rows: dict[uuid.UUID, uuid.UUID | None] = dict(
            conn.execute(
                "SELECT id, shipped_version_id FROM skill WHERE id = ANY(%s)", ([store, local],)
            ).fetchall()
        )
        conn.execute("DELETE FROM skill WHERE id = ANY(%s)", ([store, local],))
        conn.commit()
    assert rows[store] == store_version
    assert rows[local] is None
