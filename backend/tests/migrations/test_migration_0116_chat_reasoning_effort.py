"""0116: a chat keeps a reasoning level, and a model config says whether it
can think (spec 2026-11-11 §4)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from oc8 import models as m
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


async def test_a_session_keeps_a_level_or_none(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        s = m.ChatSession(tenant_id=tenant, agent_id=uuid.uuid4(), member_id=uuid.uuid4())
        db.add(s)
        await db.flush()
        await db.refresh(s)
        assert s.effort is None
        s.effort = "thorough"
        await db.flush()
        await db.refresh(s)
        assert s.effort == "thorough"


async def test_only_the_three_levels(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    with pytest.raises(IntegrityError):
        async with app_session(tenant) as db:
            db.add(
                m.ChatSession(
                    tenant_id=tenant, agent_id=uuid.uuid4(), member_id=uuid.uuid4(), effort="max"
                )
            )
            await db.flush()


async def test_a_new_model_config_cannot_think_until_told(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cfg = m.ModelConfig(tenant_id=tenant, provider="anthropic", model="claude-sonnet-5")
        db.add(cfg)
        await db.flush()
        await db.refresh(cfg)
        assert cfg.supports_reasoning is False


_ROWS: list[tuple[str, str, dict[str, Any], bool]] = [
    ("anthropic", "claude-sonnet-5", {}, True),
    ("anthropic", "claude-3-5-sonnet-20241022", {}, False),
    ("ollama", "llama3.1:8b", {}, False),
    ("Ollama", "qwen3:8b", {}, True),
    ("openai", "o3-mini", {}, True),
    # An operator already set an effort: that row worked with a level before
    # 0116, and the gate (spec §5) must not silently drop it.
    ("openai", "gpt-4o", {"effort": "high"}, True),
    ("auto", "router", {"tiers": {}}, False),
]


def test_the_backfill_marks_models_that_can_think(pg_url: str) -> None:
    tenant = uuid.uuid4()
    ids = [uuid.uuid4() for _ in _ROWS]
    dsn = pg_url.replace("+psycopg", "")
    command.downgrade(_cfg(), "0115")
    try:
        with psycopg.connect(dsn) as conn:
            for row_id, (provider, model, params, _) in zip(ids, _ROWS, strict=True):
                conn.execute(
                    "INSERT INTO model_config (id, tenant_id, provider, model, locality, params, "
                    "fallbacks, cost_meta, health, used_by_copilot) "
                    "VALUES (%s, %s, %s, %s, 'cloud', %s::jsonb, '[]', '{}', '{}', false)",
                    (row_id, tenant, provider, model, json.dumps(params)),
                )
    finally:
        command.upgrade(_cfg(), "head")
    with psycopg.connect(dsn) as conn:
        after: dict[uuid.UUID, bool] = dict(
            conn.execute(
                "SELECT id, supports_reasoning FROM model_config WHERE tenant_id = %s", (tenant,)
            ).fetchall()
        )
    assert after == {row_id: row[3] for row_id, row in zip(ids, _ROWS, strict=True)}


def _insert_agent(
    conn: psycopg.Connection[Any],
    tenant: uuid.UUID,
    *,
    model_config_id: uuid.UUID | None,
    definition: dict[str, Any],
) -> uuid.UUID:
    agent_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO agent (id, tenant_id, department_id, name, role_title, mission, "
        "config_revision, definition, model_config_id, narrowing, narrowing_overridden_keys, "
        "is_team_lead, is_tenant_assistant, status, trust_level, presentation) "
        "VALUES (%s, %s, %s, 'A', '', '', 1, %s::jsonb, %s, '{}', '[]', false, false, "
        "'stopped', 'first_party', '{}')",
        (agent_id, tenant, uuid.uuid4(), json.dumps(definition), model_config_id),
    )
    return agent_id


def _insert_model(conn: psycopg.Connection[Any], tenant: uuid.UUID, model: str) -> uuid.UUID:
    row_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO model_config (id, tenant_id, provider, model, locality, params, "
        "fallbacks, cost_meta, health, used_by_copilot) "
        "VALUES (%s, %s, 'openai', %s, 'cloud', '{}', '[]', '{}', '{}', false)",
        (row_id, tenant, model),
    )
    return row_id


def test_the_backfill_marks_models_an_agent_sets_an_effort_for(pg_url: str) -> None:
    """An agent's own `model_params.effort` was sent to its model before 0116,
    on the live row or on a published version: that model must keep it."""
    tenant = uuid.uuid4()
    dsn = pg_url.replace("+psycopg", "")
    command.downgrade(_cfg(), "0115")
    try:
        with psycopg.connect(dsn) as conn:
            live = _insert_model(conn, tenant, "gpt-4o")
            versioned = _insert_model(conn, tenant, "gpt-4.1")
            blank = _insert_model(conn, tenant, "gpt-4o-mini")
            unused = _insert_model(conn, tenant, "gpt-4-turbo")
            _insert_agent(
                conn, tenant, model_config_id=live, definition={"model_params": {"effort": "high"}}
            )
            _insert_agent(
                conn, tenant, model_config_id=blank, definition={"model_params": {"effort": "  "}}
            )
            _insert_agent(
                conn, tenant, model_config_id=unused, definition={"model_params": {"effort": 3}}
            )
            agent_id = _insert_agent(conn, tenant, model_config_id=None, definition={})
            conn.execute(
                "INSERT INTO agent_version (id, tenant_id, agent_id, version_no, payload, "
                "payload_hash) VALUES (%s, %s, %s, 1, %s::jsonb, %s)",
                (
                    uuid.uuid4(),
                    tenant,
                    agent_id,
                    json.dumps(
                        {
                            "model_config_id": str(versioned),
                            "definition": {"model_params": {"effort": "max"}},
                        }
                    ),
                    b"h",
                ),
            )
    finally:
        command.upgrade(_cfg(), "head")
    with psycopg.connect(dsn) as conn:
        after: dict[uuid.UUID, bool] = dict(
            conn.execute(
                "SELECT id, supports_reasoning FROM model_config WHERE tenant_id = %s", (tenant,)
            ).fetchall()
        )
    assert after == {live: True, versioned: True, blank: False, unused: False}


def _columns_and_check(dsn: str) -> tuple[set[tuple[str, str]], bool]:
    with psycopg.connect(dsn) as conn:
        cols = {
            (r[0], r[1])
            for r in conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE (table_name, column_name) IN "
                "(('chat_session','effort'), ('model_config','supports_reasoning'))"
            ).fetchall()
        }
        check = conn.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = 'ck_chat_session_effort'"
        ).fetchone()
    return cols, check is not None


def test_the_downgrade_drops_both_columns(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    assert _columns_and_check(dsn) == (
        {("chat_session", "effort"), ("model_config", "supports_reasoning")},
        True,
    )
    command.downgrade(_cfg(), "0115")
    try:
        assert _columns_and_check(dsn) == (set(), False)
    finally:
        command.upgrade(_cfg(), "head")
