"""Migration 0110: triggers record what their creator saw. Existing triggers
get no fingerprint (their creator saves them once again); the revision
downgrades and upgrades cleanly."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

_BACKEND = Path(__file__).resolve().parents[2]
_PATH = _BACKEND / "migrations" / "versions" / "0110_trigger_access_fingerprint.py"


def _module() -> object:
    spec = importlib.util.spec_from_file_location("oc8_migration_0110", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(pg_url: str) -> set[str]:
    engine = sa.create_engine(pg_url)
    try:
        return {c["name"] for c in sa.inspect(engine).get_columns("trigger")}
    finally:
        engine.dispose()


def test_the_revision_follows_0109() -> None:
    module = _module()
    assert module.revision == "0110"  # type: ignore[attr-defined]
    assert module.down_revision == "0109"  # type: ignore[attr-defined]


def test_existing_triggers_have_no_fingerprint(pg_url: str) -> None:
    engine = sa.create_engine(pg_url)
    with engine.connect() as conn:
        module = _module()
        module.remove_access_fingerprint(conn)  # type: ignore[attr-defined]
        conn.execute(
            sa.text(
                "INSERT INTO trigger (id, tenant_id, agent_id, kind, task_text, enabled, "
                "cron_expression, created_at, updated_at) VALUES (:id, :t, :a, 'cron', 'x', "
                "true, '0 8 * * *', now(), now())"
            ),
            {"id": uuid.uuid4(), "t": uuid.uuid4(), "a": uuid.uuid4()},
        )
        module.add_access_fingerprint(conn)  # type: ignore[attr-defined]
        module.add_access_fingerprint(conn)  # type: ignore[attr-defined]  # idempotent
        values = conn.execute(sa.text("SELECT access_fingerprint FROM trigger")).scalars().all()
        conn.rollback()
    engine.dispose()
    assert values and all(v is None for v in values)


def test_downgrade_and_upgrade(pg_url: str) -> None:
    cfg = Config(str(_BACKEND / "alembic.ini"))
    command.downgrade(cfg, "0109")
    try:
        assert "access_fingerprint" not in _columns(pg_url)
    finally:
        command.upgrade(cfg, "head")
    assert "access_fingerprint" in _columns(pg_url)
