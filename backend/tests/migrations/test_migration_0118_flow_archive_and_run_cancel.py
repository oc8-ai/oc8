"""0118: flows can be archived and flow runs cancelled; a downgrade turns
cancelled runs into failed ones so the old status check holds again."""

from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def test_a_cancelled_run_survives_a_round_trip_as_failed(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    tenant, flow_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO flow (id, tenant_id, name, archived_at) VALUES (%s, %s, %s, now())",
            (flow_id, tenant, f"flow-{flow_id.hex[:8]}"),
        )
        conn.execute(
            "INSERT INTO flow_run (id, tenant_id, flow_version_id, status, current_stages, "
            "context) VALUES (%s, %s, %s, 'cancelled', '[]', '{}')",
            (run_id, tenant, uuid.uuid4()),
        )
    command.downgrade(_cfg(), "0117")
    try:
        with psycopg.connect(dsn) as conn:
            status = conn.execute("SELECT status FROM flow_run WHERE id = %s", (run_id,)).fetchone()
            assert status == ("failed",)
    finally:
        command.upgrade(_cfg(), "head")
    with psycopg.connect(dsn) as conn:
        archived = conn.execute("SELECT archived_at FROM flow WHERE id = %s", (flow_id,)).fetchone()
    assert archived == (None,)
