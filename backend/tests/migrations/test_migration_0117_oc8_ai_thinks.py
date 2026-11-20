"""0117: every existing oc8 AI model config can think, so the chat's effort
control shows for it (0116's backfill ran before the provider declared it)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


_ROWS: list[tuple[str, str, bool]] = [
    ("opaas_ai", "opaas-echo", True),
    ("opaas", "opaas-echo", True),
    ("odoo-gpt", "opaas-echo", True),
    ("openai", "gpt-4o", False),
    ("auto", "router", False),
]


def test_oc8_ai_model_configs_are_switched_to_can_think(pg_url: str) -> None:
    tenant = uuid.uuid4()
    ids = [uuid.uuid4() for _ in _ROWS]
    dsn = pg_url.replace("+psycopg", "")
    command.downgrade(_cfg(), "0116")
    try:
        with psycopg.connect(dsn) as conn:
            for row_id, (provider, model, _) in zip(ids, _ROWS, strict=True):
                conn.execute(
                    "INSERT INTO model_config (id, tenant_id, provider, model, locality, params, "
                    "fallbacks, cost_meta, health, used_by_copilot, supports_reasoning) "
                    "VALUES (%s, %s, %s, %s, 'cloud', %s::jsonb, '[]', '{}', '{}', false, false)",
                    (row_id, tenant, provider, model, json.dumps({})),
                )
    finally:
        command.upgrade(_cfg(), "head")
    with psycopg.connect(dsn) as conn:
        after = dict(
            conn.execute(
                "SELECT id, supports_reasoning FROM model_config WHERE tenant_id = %s", (tenant,)
            ).fetchall()
        )
    assert after == {row_id: row[2] for row_id, row in zip(ids, _ROWS, strict=True)}


def test_downgrade_leaves_the_rows_as_they_are(pg_url: str) -> None:
    command.downgrade(_cfg(), "0116")
    command.upgrade(_cfg(), "head")
