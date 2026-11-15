"""0121 halves the grid row height, so every stored y and h doubles."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import psycopg
from alembic import command
from alembic.config import Config


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def _insert(
    dsn: str, tenant: uuid.UUID, layout: list[dict[str, Any]], preset: list[dict[str, Any]]
) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO member_dashboard_layout (id, tenant_id, member_id, widgets) "
            "VALUES (%s, %s, %s, %s::jsonb)",
            (uuid.uuid4(), tenant, uuid.uuid4(), json.dumps(layout)),
        )
        conn.execute(
            "INSERT INTO dashboard_preset (id, tenant_id, member_id, name, widgets) "
            "VALUES (%s, %s, %s, 'p', %s::jsonb)",
            (uuid.uuid4(), tenant, uuid.uuid4(), json.dumps(preset)),
        )


def _read(dsn: str, tenant: uuid.UUID) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT widgets FROM member_dashboard_layout WHERE tenant_id = %s", (tenant,)
        ).fetchone()
        prow = conn.execute(
            "SELECT widgets FROM dashboard_preset WHERE tenant_id = %s", (tenant,)
        ).fetchone()
    assert row is not None and prow is not None
    return row[0], prow[0]


def test_layouts_and_presets_double_y_and_h(pg_url: str) -> None:
    tenant = uuid.uuid4()
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    command.downgrade(cfg, "0120")
    try:
        _insert(
            dsn,
            tenant,
            [
                {"id": "a", "type": "chat", "x": 0, "y": 0, "w": 6, "h": 6, "config": {}},
                {"id": "b", "type": "budget", "x": 6, "y": 3, "w": 3, "h": 3, "config": {"k": 1}},
            ],
            [{"id": "c", "type": "tasks", "x": 0, "y": 5, "w": 6, "h": 5, "config": {}}],
        )
    finally:
        command.upgrade(cfg, "head")
    layout, preset = _read(dsn, tenant)
    assert layout == [
        {"id": "a", "type": "chat", "x": 0, "y": 0, "w": 6, "h": 12, "config": {}},
        {"id": "b", "type": "budget", "x": 6, "y": 6, "w": 3, "h": 6, "config": {"k": 1}},
    ]
    assert preset == [{"id": "c", "type": "tasks", "x": 0, "y": 10, "w": 6, "h": 10, "config": {}}]


def test_downgrade_halves_again(pg_url: str) -> None:
    tenant = uuid.uuid4()
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    _insert(
        dsn,
        tenant,
        [{"id": "a", "type": "chat", "x": 0, "y": 6, "w": 6, "h": 12, "config": {}}],
        [{"id": "c", "type": "tasks", "x": 0, "y": 10, "w": 6, "h": 1, "config": {}}],
    )
    command.downgrade(cfg, "0120")
    try:
        layout, preset = _read(dsn, tenant)
    finally:
        command.upgrade(cfg, "head")
    assert layout == [{"id": "a", "type": "chat", "x": 0, "y": 3, "w": 6, "h": 6, "config": {}}]
    assert preset == [{"id": "c", "type": "tasks", "x": 0, "y": 5, "w": 6, "h": 1, "config": {}}]


def test_odd_stored_values_are_left_unchanged_and_never_abort(pg_url: str) -> None:
    tenant = uuid.uuid4()
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    odd: list[dict[str, Any]] = [
        {"id": "n", "type": "chat", "x": 0, "y": None, "w": 6, "h": 6, "config": {}},
        {"id": "big", "type": "chat", "x": 0, "y": 2147483647, "w": 6, "h": 6, "config": {}},
        {"id": "frac", "type": "chat", "x": 0, "y": 1.5, "w": 6, "h": 2, "config": {}},
        {"id": "sy", "type": "chat", "x": 0, "y": "abc", "w": 6, "h": 2, "config": {}},
        {"id": "sh", "type": "chat", "x": 0, "y": 2, "w": 6, "h": "x", "config": {}},
        {"id": "ok", "type": "chat", "x": 0, "y": 1, "w": 6, "h": 2, "config": {}},
    ]
    command.downgrade(cfg, "0120")
    try:
        _insert(dsn, tenant, odd, [])
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO dashboard_preset (id, tenant_id, member_id, name, widgets) "
                "VALUES (%s, %s, %s, 'o', '{}'::jsonb)",
                (uuid.uuid4(), tenant, uuid.uuid4()),
            )
    finally:
        command.upgrade(cfg, "head")
    layout, _ = _read(dsn, tenant)
    assert layout[:5] == odd[:5]
    assert (layout[5]["y"], layout[5]["h"]) == (2, 4)
    with psycopg.connect(dsn) as conn:
        obj = conn.execute(
            "SELECT widgets FROM dashboard_preset WHERE tenant_id = %s AND name = 'o'", (tenant,)
        ).fetchone()
    assert obj is not None and obj[0] == {}
