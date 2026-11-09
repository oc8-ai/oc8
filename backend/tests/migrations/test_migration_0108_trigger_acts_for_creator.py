"""Migration 0108: triggers do not act for their creator unless told to. Runs the
helpers against a raw connection inside a transaction and rolls back."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import sqlalchemy as sa

_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0108_trigger_acts_for_creator.py"
)


def _module() -> object:
    spec = importlib.util.spec_from_file_location("oc8_migration_0108", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_revision_follows_0107() -> None:
    module = _module()
    assert module.revision == "0108"  # type: ignore[attr-defined]
    assert module.down_revision == "0107"  # type: ignore[attr-defined]


def test_existing_triggers_do_not_act_for_their_creator(pg_url: str) -> None:
    engine = sa.create_engine(pg_url)
    with engine.connect() as conn:
        module = _module()
        module.remove_acts_for_creator(conn)  # type: ignore[attr-defined]
        conn.execute(
            sa.text(
                "INSERT INTO trigger (id, tenant_id, agent_id, kind, task_text, enabled, "
                "webhook_token, created_at, updated_at) VALUES (:id, :t, :a, 'webhook', 'x', "
                "true, :w, now(), now())"
            ),
            {"id": uuid.uuid4(), "t": uuid.uuid4(), "a": uuid.uuid4(), "w": uuid.uuid4().hex},
        )
        module.add_acts_for_creator(conn)  # type: ignore[attr-defined]
        flags = conn.execute(sa.text("SELECT acts_for_creator FROM trigger")).scalars().all()
        conn.rollback()
    engine.dispose()
    assert flags and not any(flags)
