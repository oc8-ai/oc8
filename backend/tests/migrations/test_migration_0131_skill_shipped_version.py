"""0131: a skill records its shipped version and whether it was customised; an
existing store skill's current version becomes its shipped one."""

from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config

_COLUMNS = {
    "shipped_version_id",
    "shipped_description",
    "shipped_category",
    "customized_at",
    "shipped_update_at",
}


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
                    "INSERT INTO skill (id, tenant_id, name, description, category, author, "
                    "origin, trust_level, current_version_id) "
                    "VALUES (%s, %s, %s, 'Shipped text.', 'finance', 'x', %s, 'first_party', %s)",
                    (skill_id, tenant, f"s-{skill_id.hex[:6]}", origin, version_id),
                )
            conn.commit()
    finally:
        command.upgrade(cfg, "head")
    assert _COLUMNS <= _columns(dsn)
    with psycopg.connect(dsn) as conn:
        rows = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT id, shipped_version_id, shipped_description, shipped_category "
                "FROM skill WHERE id = ANY(%s)",
                ([store, local],),
            ).fetchall()
        }
        conn.execute("DELETE FROM skill WHERE id = ANY(%s)", ([store, local],))
        conn.commit()
    assert rows[store] == (store_version, "Shipped text.", "finance")
    assert rows[local] == (None, None, None)


def test_a_capa_skill_gets_no_shipped_description_so_an_earlier_edit_is_not_taken_as_shipped(
    pg_url: str,
) -> None:
    """Before 0131 an admin could already change a store skill's description and
    category. For a capa's skill the live values may be such an edit, so they are
    left unknown (NULL): the next sync compares them with the capa's own. A
    seeded or imported store skill has no capa to ask, so its live values are
    the best record of what shipped."""
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    tenant = uuid.uuid4()
    capa_skill, seeded = uuid.uuid4(), uuid.uuid4()
    command.downgrade(cfg, "0129")
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO capa (id, tenant_id, name, type, author, origin, trust_level, "
                "core_compat) VALUES (%s, %s, 'acme.skills', 'skill', 'acme', 'store', "
                "'first_party', '')",
                (uuid.uuid4(), tenant),
            )
            for skill_id, author in ((capa_skill, "acme.skills"), (seeded, "oc8 store")):
                conn.execute(
                    "INSERT INTO skill (id, tenant_id, name, description, category, author, "
                    "origin, trust_level, current_version_id) "
                    "VALUES (%s, %s, %s, 'Edited by an admin.', 'ops', %s, 'store', "
                    "'first_party', %s)",
                    (skill_id, tenant, f"s-{skill_id.hex[:6]}", author, uuid.uuid4()),
                )
            conn.commit()
    finally:
        command.upgrade(cfg, "head")
    with psycopg.connect(dsn) as conn:
        rows = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT id, shipped_version_id IS NOT NULL, shipped_description, "
                "shipped_category FROM skill WHERE id = ANY(%s)",
                ([capa_skill, seeded],),
            ).fetchall()
        }
        conn.execute("DELETE FROM skill WHERE id = ANY(%s)", ([capa_skill, seeded],))
        conn.execute("DELETE FROM capa WHERE tenant_id = %s", (tenant,))
        conn.commit()
    assert rows[capa_skill] == (True, None, None)
    assert rows[seeded] == (True, "Edited by an admin.", "ops")
