"""A personal department, or an agent of one, is not exportable as a capa.

`POST /capas/export` renders the mission, prompt starters, trigger task text
and persona into a manifest -- with `dryRun` straight into the response. A
personal agent's content belongs to its owner alone, so the export treats a
personal department or agent id as not found, dry run or not, for any caller.
A standard department still exports.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from tests.api.test_personal_agent_content import _MISSION, _TASK, _headers, _http, _world
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _item(kind: str, item_id: uuid.UUID) -> dict[str, Any]:
    return {"kind": kind, "id": str(item_id), "name": "pia", "version": "1.0.0", "summary": ""}


async def _department_of(
    app_session: AppSessionFactory, tenant: uuid.UUID, agent_id: uuid.UUID
) -> uuid.UUID:
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None and agent.department_id is not None
        return agent.department_id


def _assert_no_content(text: str) -> None:
    assert _MISSION not in text
    assert _TASK not in text
    assert "Summarise my inbox" not in text


@pytest.mark.parametrize("kind", ["department", "agent"])
async def test_an_admin_dry_run_of_a_personal_id_is_refused_as_not_found(
    app_session: AppSessionFactory, kind: str
) -> None:
    w = await _world(app_session)
    target = (
        await _department_of(app_session, w.tenant, w.personal_agent)
        if kind == "department"
        else w.personal_agent
    )
    async with _http() as http:
        r = await http.post(
            "/api/v1/capas/export",
            json={"items": [_item(kind, target)], "dry_run": True},
            headers=_headers(w.tenant, "admin", "org_admin"),
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == []
    assert body["errors"] == [f"{kind} {target} not found"]
    _assert_no_content(r.text)


@pytest.mark.parametrize("kind", ["department", "agent"])
async def test_a_real_export_of_a_personal_id_is_refused_as_not_found(
    app_session: AppSessionFactory, kind: str
) -> None:
    w = await _world(app_session)
    target = (
        await _department_of(app_session, w.tenant, w.personal_agent)
        if kind == "department"
        else w.personal_agent
    )
    async with _http() as http:
        r = await http.post(
            "/api/v1/capas/export",
            json={"items": [_item(kind, target)], "dry_run": False},
            headers=_headers(w.tenant, "admin", "org_admin"),
        )
    assert r.status_code == 422, r.text
    assert f"{kind} {target} not found" in r.text
    _assert_no_content(r.text)


async def test_a_standard_agent_still_exports(app_session: AppSessionFactory) -> None:
    w = await _world(app_session)
    async with _http() as http:
        r = await http.post(
            "/api/v1/capas/export",
            json={"items": [_item("agent", w.standard_agent)], "dry_run": True},
            headers=_headers(w.tenant, "admin", "org_admin"),
        )
    assert r.status_code == 200, r.text
    assert r.json()["errors"] == []
    assert _MISSION in r.json()["items"][0]["manifest_toml"]
