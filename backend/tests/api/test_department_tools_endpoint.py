"""PUT /departments/{id}/tools -- validates the tool-policy payload against
the shape `authz/pdp.py`'s `ToolPolicy.from_json` actually expects, instead of
writing arbitrary JSON straight into `department.frame["tools"]`. Same
tenant-wide-only `department:manage` gate as `PATCH /departments/{id}`
(test_department_patch.py) -- no seat can ever hold it, so `org_admin` is used
for the allowed cases and `member` for the 403 case, matching that file's
convention exactly."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.config import get_settings
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

# tests/api/<this file> -> tests -> backend -> repo root, where capas/ lives.
_PLUGINS_DIR = Path(__file__).resolve().parents[3] / "capas"


@pytest.fixture(autouse=True)
def _plugins_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OC8_CAPAS_PATH", str(_PLUGINS_DIR))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _headers(tenant: uuid.UUID, role: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="u", role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _seed_department(app_session: AppSessionFactory) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(
            tenant_id=tenant,
            name="Sales",
            goal="",
            frame={},
            presentation={"icon": "building", "slug": "sales"},
        )
        db.add(dept)
        # Every existing test in this file writes `approvalEur`/`only` for the
        # "odoo" key -- since Task 5, that now needs to resolve back to a real
        # manifest connection with a `value_spec` to be accepted (odoo_mcp's
        # `primary` connection has one; see `capas/odoo_mcp/tool_pack.toml`).
        conn = m.McpConnection(
            tenant_id=tenant,
            name="odoo",
            server_url="",
            transport="stdio",
            config={"_plugin_name": "odoo_mcp", "_connection_key": "primary"},
        )
        db.add(conn)
        await db.flush()
        return tenant, dept.id


async def test_well_formed_payload_is_accepted_and_echoed_back(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _seed_department(app_session)
    payload = {
        "tools": {
            "odoo": {
                "enabled": True,
                "read": True,
                "modify": False,
                "approvalEur": 3000,
                "approvalActions": ["send"],
                "only": ["search_records", "post_message"],
            }
        }
    }
    # The frame -- and therefore the read path -- stores/echoes snake_case
    # keys, matching what `ToolPolicy.from_json` expects (see
    # `DepartmentToolsDTO.tools: dict[str, dict[str, Any]]`: the inner values
    # are raw dicts, not nested CamelModels, so no camelCase aliasing applies
    # to them -- only the request body goes through camelCase translation).
    expected = {
        "tools": {
            "odoo": {
                "enabled": True,
                "read": True,
                "modify": False,
                "approval_eur": 3000,
                "approval_actions": ["send"],
                "only": ["search_records", "post_message"],
                "conditions": [],
                "limits": [],
                "default_connection_id": None,
            }
        },
        "deviationCounts": {"odoo": 0},
        "agentCount": 0,
    }
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 200, resp.text
        assert resp.json() == expected

        get_resp = await http.get(f"/api/v1/departments/{dept_id}/tools", headers=h)
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json() == expected


async def test_unknown_key_is_rejected(app_session: AppSessionFactory) -> None:
    tenant, dept_id = await _seed_department(app_session)
    payload = {"tools": {"odoo": {"enabled": True, "foo": True}}}
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 422, resp.text


async def test_wrong_type_for_bool_field_is_rejected(app_session: AppSessionFactory) -> None:
    # Not a string like "yes"/"true" -- Pydantic v2's lenient bool coercion
    # accepts those. A list is not bool-coercible under any mode.
    tenant, dept_id = await _seed_department(app_session)
    payload = {"tools": {"odoo": {"enabled": ["not", "a", "bool"]}}}
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 422, resp.text


async def test_negative_approval_eur_is_rejected(app_session: AppSessionFactory) -> None:
    tenant, dept_id = await _seed_department(app_session)
    payload = {"tools": {"odoo": {"approvalEur": -100}}}
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 422, resp.text


async def test_zero_approval_eur_is_preserved_not_dropped(
    app_session: AppSessionFactory,
) -> None:
    """`0` means "a human decides every send" (ToolPolicy's docstring in
    pdp.py, and the identical rule GuardrailPreset enforces in manifest.py) --
    the single most important assertion in this file. A normalisation bug
    here would silently delete the strictest possible setting."""
    tenant, dept_id = await _seed_department(app_session)
    payload = {"tools": {"odoo": {"approvalEur": 0}}}
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 200, resp.text
        assert resp.json()["tools"]["odoo"]["approval_eur"] == 0

        get_resp = await http.get(f"/api/v1/departments/{dept_id}/tools", headers=h)
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json()["tools"]["odoo"]["approval_eur"] == 0


async def test_empty_string_approval_action_is_rejected(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _seed_department(app_session)
    payload = {"tools": {"odoo": {"approvalActions": [""]}}}
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 422, resp.text


async def test_non_string_approval_action_is_rejected(
    app_session: AppSessionFactory,
) -> None:
    """Raw JSON permits a non-string entry even though `approval_actions` is
    typed `list[str]`; verify Pydantic's coercion does NOT silently accept
    it."""
    tenant, dept_id = await _seed_department(app_session)
    payload = {"tools": {"odoo": {"approvalActions": [123]}}}
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 422, resp.text


async def test_omitting_only_is_accepted_and_reads_back_as_null(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _seed_department(app_session)
    payload = {"tools": {"odoo": {"enabled": True}}}
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 200, resp.text
        assert resp.json()["tools"]["odoo"]["only"] is None

        get_resp = await http.get(f"/api/v1/departments/{dept_id}/tools", headers=h)
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json()["tools"]["odoo"]["only"] is None


async def test_applying_a_guardrail_with_nonempty_only_persists_the_exact_list(
    app_session: AppSessionFactory,
) -> None:
    """There is no dedicated "apply a guardrail" endpoint (task-6 investigation
    confirmed: `guardrail-preset-picker.tsx` is entirely client-side and every
    save path -- preset, library guardrail, or hand-configured -- converges on
    this one `PUT`). This proves that ONE write path preserves `only` for a
    real Task-4 library entry with a non-empty `only`: `sales_autonomous_with_
    limit` from `plugins/odoo_mcp/guardrails/`, which deliberately excludes
    `delete_record` from its 9-tool alphabet (that file's own summary: "a euro
    threshold can never catch a deletion, which carries no amount")."""
    tenant, dept_id = await _seed_department(app_session)
    payload = {
        "tools": {
            "odoo": {
                "enabled": True,
                "read": True,
                "modify": False,
                "approvalEur": 1000,
                "approvalActions": [],
                "only": [
                    "search_records",
                    "get_record",
                    "list_models",
                    "list_resource_templates",
                    "aggregate_records",
                    "create_record",
                    "update_record",
                    "post_message",
                ],
            }
        }
    }
    expected_only = payload["tools"]["odoo"]["only"]
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 200, resp.text
        assert resp.json()["tools"]["odoo"]["only"] == expected_only

        get_resp = await http.get(f"/api/v1/departments/{dept_id}/tools", headers=h)
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json()["tools"]["odoo"]["only"] == expected_only
        assert "delete_record" not in get_resp.json()["tools"]["odoo"]["only"]


async def test_applying_a_guardrail_with_empty_only_persists_explicit_empty_list(
    app_session: AppSessionFactory,
) -> None:
    """Same write path, the other real Task-4 case: `quote_approval_threshold`
    from `plugins/odoo_mcp/guardrails/` deliberately ships `only = []` --
    that file's own summary calls this out as a documented gap ("`only` is
    deliberately left empty here ... delete_record and post_message stay
    reachable ungated"). This is the "dropped at four boundaries" failure mode
    named in the guardrail-library spec: an explicit `only: []` must read back
    as `[]`, not `null`/missing -- distinct from `test_omitting_only_is_
    accepted_and_reads_back_as_null` above, which never sends the key at all."""
    tenant, dept_id = await _seed_department(app_session)
    payload = {
        "tools": {
            "odoo": {
                "enabled": True,
                "read": True,
                "modify": False,
                "approvalEur": 3000,
                "approvalActions": [],
                "only": [],
            }
        }
    }
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 200, resp.text
        assert resp.json()["tools"]["odoo"]["only"] == []
        assert resp.json()["tools"]["odoo"]["only"] is not None

        get_resp = await http.get(f"/api/v1/departments/{dept_id}/tools", headers=h)
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json()["tools"]["odoo"]["only"] == []
        assert get_resp.json()["tools"]["odoo"]["only"] is not None


async def test_member_role_is_refused(app_session: AppSessionFactory) -> None:
    tenant, dept_id = await _seed_department(app_session)
    payload = {"tools": {"odoo": {"enabled": True}}}
    async with _http() as http:
        h = _headers(tenant, "member")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 403, resp.text


async def test_bogus_department_id_404s(app_session: AppSessionFactory) -> None:
    tenant, _ = await _seed_department(app_session)
    payload = {"tools": {"odoo": {"enabled": True}}}
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{uuid.uuid4()}/tools", json=payload, headers=h)
        assert resp.status_code == 404, resp.text


# ---------------------------- default_connection_id (department default login)


async def test_default_connection_id_round_trips(app_session: AppSessionFactory) -> None:
    tenant, dept_id = await _seed_department(app_session)
    conn_id = str(uuid.uuid4())
    payload = {"tools": {"odoo": {"enabled": True, "defaultConnectionId": conn_id}}}
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 200, resp.text
        assert resp.json()["tools"]["odoo"]["default_connection_id"] == conn_id

        get_resp = await http.get(f"/api/v1/departments/{dept_id}/tools", headers=h)
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json()["tools"]["odoo"]["default_connection_id"] == conn_id


async def test_omitting_default_connection_id_is_accepted_and_reads_back_as_null(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _seed_department(app_session)
    payload = {"tools": {"odoo": {"enabled": True}}}
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 200, resp.text
        assert resp.json()["tools"]["odoo"]["default_connection_id"] is None


async def test_non_uuid_default_connection_id_is_rejected(app_session: AppSessionFactory) -> None:
    tenant, dept_id = await _seed_department(app_session)
    payload = {"tools": {"odoo": {"enabled": True, "defaultConnectionId": "not-a-uuid"}}}
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 422, resp.text


async def test_empty_string_default_connection_id_is_normalised_to_null(
    app_session: AppSessionFactory,
) -> None:
    """The frontend clears a picker by sending "" -- treated the same as
    omitting the field entirely, not rejected."""
    tenant, dept_id = await _seed_department(app_session)
    payload = {"tools": {"odoo": {"enabled": True, "defaultConnectionId": ""}}}
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 200, resp.text
        assert resp.json()["tools"]["odoo"]["default_connection_id"] is None


# ---------------------------- only/approval_eur value-spec gate (Task 5)


async def test_setting_approval_eur_for_a_connection_with_no_value_spec_is_rejected(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _seed_department(app_session)
    async with app_session(tenant) as db:
        conn = m.McpConnection(
            tenant_id=tenant,
            name="github",
            server_url="",
            transport="stdio",
            config={"_plugin_name": "github_mcp", "_connection_key": "primary"},
        )
        db.add(conn)
        await db.flush()

    payload = {"tools": {"github": {"enabled": True, "read": True, "approvalEur": 3000}}}
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 422, resp.text
        body = resp.json()["detail"]
        assert body["error"] == "value_spec_not_supported"
        assert body["violations"] == [{"connection": "github", "field": "approval_eur"}]


async def test_an_empty_only_list_for_a_connection_with_no_value_spec_is_accepted(
    app_session: AppSessionFactory,
) -> None:
    """The frontend's GuardrailValue always sends `only: []` for a tool that
    never had an allowlist -- never `null` -- so this must NOT trip the same
    gate a genuinely non-empty `only` does. Regression for a live-verification
    finding: every real save from the new ToolGuardrailTable UI was 422ing on
    any connection with no value_spec, because `only` was checked with
    `is not None` instead of a real non-empty check."""
    tenant, dept_id = await _seed_department(app_session)
    async with app_session(tenant) as db:
        conn = m.McpConnection(
            tenant_id=tenant,
            name="github",
            server_url="",
            transport="stdio",
            config={"_plugin_name": "github_mcp", "_connection_key": "primary"},
        )
        db.add(conn)
        await db.flush()

    payload = {"tools": {"github": {"enabled": True, "read": True, "only": []}}}
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 200, resp.text


async def test_setting_a_nonempty_only_for_a_connection_with_no_value_spec_is_accepted(
    app_session: AppSessionFactory,
) -> None:
    """Regression: `only` is a plain tool-name allowlist, not a value_spec
    feature -- github_mcp/jira_mcp/microsoft365/google_workspace all ship real
    `only`-based guardrail presets (e.g. github's "Support: Issue triage, no
    code access") and none of them declare a value_spec. Gating `only` behind
    `connection_supports_value_spec` broke every one of those presets; only
    `approval_eur` genuinely needs a value_spec, since that's where the
    monetary threshold gets compared against. Found live: applying github's
    own "Support" preset from the department Guardrails tab 422'd."""
    tenant, dept_id = await _seed_department(app_session)
    async with app_session(tenant) as db:
        conn = m.McpConnection(
            tenant_id=tenant,
            name="github",
            server_url="",
            transport="stdio",
            config={"_plugin_name": "github_mcp", "_connection_key": "primary"},
        )
        db.add(conn)
        await db.flush()

    payload = {
        "tools": {
            "github": {"enabled": True, "read": True, "only": ["add_issue_comment", "issue_write"]}
        }
    }
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 200, resp.text
        assert resp.json()["tools"]["github"]["only"] == ["add_issue_comment", "issue_write"]


async def test_setting_only_for_a_connection_with_a_value_spec_is_accepted(
    app_session: AppSessionFactory,
) -> None:
    # `_seed_department` already seeds an "odoo" McpConnection stamped to
    # `odoo_mcp`'s `primary` connection, which HAS a `value_spec` block.
    tenant, dept_id = await _seed_department(app_session)
    payload = {"tools": {"odoo": {"enabled": True, "read": True, "only": ["search_records"]}}}
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 200, resp.text
        assert resp.json()["tools"]["odoo"]["only"] == ["search_records"]


# ---------------------------- deviation_counts and agent_count (Task 6)


async def test_get_department_tools_reports_deviation_counts(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _seed_department(app_session)
    async with app_session(tenant) as db:
        dept = await db.get(m.Department, dept_id)
        dept.frame = {"tools": {"github": {"enabled": True, "read": True, "modify": True}}}
        agent_a = m.Agent(
            tenant_id=tenant,
            department_id=dept_id,
            name="A",
            narrowing_overridden_keys=["github"],
        )
        agent_b = m.Agent(
            tenant_id=tenant,
            department_id=dept_id,
            name="B",
            narrowing_overridden_keys=[],
        )
        agent_c = m.Agent(
            tenant_id=tenant,
            department_id=dept_id,
            name="C",
            narrowing_overridden_keys=["github"],
        )
        db.add_all([agent_a, agent_b, agent_c])
        await db.flush()

    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.get(f"/api/v1/departments/{dept_id}/tools", headers=h)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["deviationCounts"]["github"] == 2
        assert body["agentCount"] == 3


async def test_put_department_tools_also_reports_deviation_counts(
    app_session: AppSessionFactory,
) -> None:
    """The write endpoint returns the same DTO shape as the read endpoint --
    a caller acting on the PUT response directly must not see stale zeros
    for a department that already has agents with real overrides."""
    tenant, dept_id = await _seed_department(app_session)
    async with app_session(tenant) as db:
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept_id,
            name="A",
            narrowing_overridden_keys=["odoo"],
        )
        db.add(agent)
        await db.flush()

    payload = {"tools": {"odoo": {"enabled": True, "read": True, "modify": True}}}
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["deviationCounts"]["odoo"] == 1
        assert body["agentCount"] == 1


async def test_a_generic_condition_round_trips_through_the_write_and_read_paths(
    app_session: AppSessionFactory,
) -> None:
    """The generic Condition model (authz/pdp.py) is writable through this
    endpoint, not just readable off a hand-built frame -- proves
    `ToolPolicyWriteDTO.conditions`/`ConditionWriteDTO` actually reach
    `department.frame["tools"][key]["conditions"]` in the exact shape
    `Condition.from_json` expects."""
    tenant, dept_id = await _seed_department(app_session)
    payload = {
        "tools": {
            "odoo": {
                "enabled": True,
                "modify": True,
                "conditions": [
                    {"attribute": "order_value", "operator": ">", "value": 5000},
                ],
            }
        }
    }
    expected_condition = {
        "attribute": "order_value",
        "datatype": "number",
        "operator": ">",
        "value": 5000,
        "then": "require_approval",
    }
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 200, resp.text
        assert resp.json()["tools"]["odoo"]["conditions"] == [expected_condition]

        get_resp = await http.get(f"/api/v1/departments/{dept_id}/tools", headers=h)
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json()["tools"]["odoo"]["conditions"] == [expected_condition]


async def test_an_unknown_condition_operator_is_rejected(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _seed_department(app_session)
    payload = {
        "tools": {
            "odoo": {
                "conditions": [{"attribute": "order_value", "operator": "~=", "value": 1}],
            }
        }
    }
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 422, resp.text


async def test_a_blank_condition_attribute_is_rejected(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _seed_department(app_session)
    payload = {"tools": {"odoo": {"conditions": [{"attribute": "", "value": 1}]}}}
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.put(f"/api/v1/departments/{dept_id}/tools", json=payload, headers=h)
        assert resp.status_code == 422, resp.text


async def test_resetting_a_key_with_a_capa_default_restores_it_verbatim(
    app_session: AppSessionFactory,
) -> None:
    """A key that WAS the CAPA template's own default, then hand-edited by an
    operator, goes back to exactly that default -- not to some other
    invented value -- on reset."""
    tenant, dept_id = await _seed_department(app_session)
    capa_default = {"enabled": True, "read": True, "modify": False}
    async with app_session(tenant) as db:
        dept = await db.get(m.Department, dept_id)
        assert dept is not None
        dept.frame = {"tools": {"odoo": {"enabled": True, "read": True, "modify": True}}}
        dept.frame_capa_defaults = {"tools": {"odoo": capa_default}}
        await db.flush()

    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.post(f"/api/v1/departments/{dept_id}/tools/odoo/reset", headers=h)
        assert resp.status_code == 200, resp.text
        assert resp.json()["tools"]["odoo"]["modify"] is False

        get_resp = await http.get(f"/api/v1/departments/{dept_id}/tools", headers=h)
        assert get_resp.json()["tools"]["odoo"]["modify"] is False


async def test_resetting_a_key_with_no_capa_default_removes_it_entirely(
    app_session: AppSessionFactory,
) -> None:
    """A hand-created department (or one that predates `frame_capa_defaults`)
    has nothing to restore a key TO, so reset removes the key rather than
    inventing or keeping a value."""
    tenant, dept_id = await _seed_department(app_session)
    async with app_session(tenant) as db:
        dept = await db.get(m.Department, dept_id)
        assert dept is not None
        dept.frame = {"tools": {"odoo": {"enabled": True, "read": True}}}
        # frame_capa_defaults left at its column default (None).
        await db.flush()

    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.post(f"/api/v1/departments/{dept_id}/tools/odoo/reset", headers=h)
        assert resp.status_code == 200, resp.text
        assert "odoo" not in resp.json()["tools"]

        get_resp = await http.get(f"/api/v1/departments/{dept_id}/tools", headers=h)
        assert "odoo" not in get_resp.json()["tools"]


async def test_resetting_an_untracked_key_404s(app_session: AppSessionFactory) -> None:
    tenant, dept_id = await _seed_department(app_session)
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.post(f"/api/v1/departments/{dept_id}/tools/odoo/reset", headers=h)
        assert resp.status_code == 404, resp.text


async def test_resetting_a_key_cascades_to_agents_that_never_overrode_it(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _seed_department(app_session)
    capa_default = {"enabled": False, "read": True}
    async with app_session(tenant) as db:
        dept = await db.get(m.Department, dept_id)
        assert dept is not None
        dept.frame = {"tools": {"odoo": {"enabled": True, "read": True}}}
        dept.frame_capa_defaults = {"tools": {"odoo": capa_default}}
        agent = m.Agent(tenant_id=tenant, department_id=dept_id, name="Nora", narrowing={})
        db.add(agent)
        await db.flush()
        agent_id = agent.id

    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.post(f"/api/v1/departments/{dept_id}/tools/odoo/reset", headers=h)
        assert resp.status_code == 200, resp.text
        assert resp.json()["deviationCounts"] == {"odoo": 0}

    async with app_session(tenant) as db:
        reloaded = await db.get(m.Agent, agent_id)
        assert reloaded is not None
        assert reloaded.narrowing["tools"]["odoo"]["enabled"] is False
