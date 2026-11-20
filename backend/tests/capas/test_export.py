from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.capas.export import (
    ExportValidationError,
    build_agent_export,
    build_department_export,
    build_skill_export,
)
from oc8.capas.manifest import SkillTemplateSpec, parse_manifest
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _make_skill(
    s: AsyncSession, tenant_id: uuid.UUID, name: str, origin: str = "local"
) -> m.Skill:
    skill = m.Skill(tenant_id=tenant_id, name=name, origin=origin, description=f"{name} skill")
    s.add(skill)
    await s.flush()
    version = m.SkillVersion(
        tenant_id=tenant_id,
        skill_id=skill.id,
        semver="1.0.0",
        definition={
            "schema_version": 1,
            "instruction": f"do {name}",
            "requires": {"tools": [], "kbs": []},
            "guardrails": [],
        },
        artifact_hash=b"x",
    )
    s.add(version)
    await s.flush()
    skill.current_version_id = version.id
    await s.flush()
    return skill


async def test_build_skill_export_round_trips_through_parse_manifest(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        skill = await _make_skill(s, tenant, "crm-follow-up")
        exported = await build_skill_export(
            s,
            tenant_id=tenant,
            skill_id=skill.id,
            capa_name="crm_follow_up",
            version="1.0.0",
            summary="Follows up on stale CRM leads",
        )
        assert exported.folder_name == "crm_follow_up"
        assert exported.warnings == []
        import tomllib

        parsed = parse_manifest(tomllib.loads(exported.manifest_toml)["plugin"])
        assert parsed.name == "crm_follow_up"
        assert parsed.type == "skill"
        # The skill body lives in a sibling skills/<name>.toml, not inline in
        # plugin.toml -- discovery.py hard-rejects an inline skill_template.
        assert parsed.skill_template is None
        assert set(exported.extra_files) == {"skills/crm_follow_up.toml"}
        skill_raw = tomllib.loads(exported.extra_files["skills/crm_follow_up.toml"])
        skill_spec = SkillTemplateSpec.model_validate(skill_raw)
        assert skill_spec.name == "crm-follow-up"
        assert skill_spec.instruction == "do crm-follow-up"


async def test_build_skill_export_excludes_store_origin_skill(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        skill = await _make_skill(s, tenant, "store-skill", origin="store")
        with pytest.raises(ExportValidationError, match="local"):
            await build_skill_export(
                s, tenant_id=tenant, skill_id=skill.id, capa_name="x", version="1.0.0", summary=""
            )


async def test_build_department_export_maps_agents_and_skills(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        skill = await _make_skill(s, tenant, "crm-follow-up")
        dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={"tools": {}, "memory": {}})
        s.add(dept)
        await s.flush()
        lead = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Nora",
            mission="Lead the team",
            is_team_lead=True,
            status="stopped",
        )
        s.add(lead)
        await s.flush()
        dept.team_lead_agent_id = lead.id
        rep = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Rep A",
            mission="Follow up",
            status="stopped",
        )
        s.add(rep)
        await s.flush()
        s.add(
            m.SkillAssignment(
                tenant_id=tenant, agent_id=rep.id, skill_version_id=skill.current_version_id
            )
        )
        await s.flush()

        exported = await build_department_export(
            s,
            tenant_id=tenant,
            department_id=dept.id,
            capa_name="vertrieb",
            version="1.0.0",
            summary="Vertriebsteam",
        )
        import tomllib

        parsed = parse_manifest(tomllib.loads(exported.manifest_toml)["plugin"])
        assert parsed.type == "department_template"
        assert parsed.department_template is not None
        names = {a.name for a in parsed.department_template.agents}
        assert names == {"Nora", "Rep A"}
        nora = next(a for a in parsed.department_template.agents if a.name == "Nora")
        rep_a = next(a for a in parsed.department_template.agents if a.name == "Rep A")
        assert nora.is_team_lead is True
        assert nora.reports_to is None
        assert rep_a.reports_to == "Nora"
        assert rep_a.skills == ["crm-follow-up"]


async def test_build_department_export_drops_unresolvable_tool_grant_with_warning(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        dept = m.Department(
            tenant_id=tenant,
            name="Vertrieb",
            frame={"tools": {"ghost_connection": {"read": True}}, "memory": {}},
        )
        s.add(dept)
        await s.flush()
        lead = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Nora",
            is_team_lead=True,
            status="stopped",
        )
        s.add(lead)
        await s.flush()
        dept.team_lead_agent_id = lead.id
        await s.flush()

        exported = await build_department_export(
            s, tenant_id=tenant, department_id=dept.id, capa_name="vertrieb",
            version="1.0.0", summary="",
        )
        assert any("ghost_connection" in w for w in exported.warnings)
        parsed = parse_manifest(__import__("tomllib").loads(exported.manifest_toml)["plugin"])
        assert parsed.department_template is not None
        assert "ghost_connection" not in (parsed.department_template.frame.get("tools") or {})


async def test_build_department_export_resolves_connection_to_capability_dependency(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        capa = m.Capa(tenant_id=tenant, name="odoo_mcp", type="tool_pack")
        s.add(capa)
        await s.flush()
        capa_version = m.CapaVersion(
            tenant_id=tenant,
            capa_id=capa.id,
            semver="1.0.0",
            manifest={"name": "odoo_mcp", "version": "1.0.0"},
            artifact_hash=b"x",
            capabilities=["integration:odoo"],
        )
        s.add(capa_version)
        await s.flush()
        capa.current_version_id = capa_version.id
        s.add(
            m.McpConnection(
                tenant_id=tenant,
                name="odoo",
                server_url="stdio://odoo",
                config={"_plugin_name": "odoo_mcp"},
            )
        )
        dept = m.Department(
            tenant_id=tenant,
            name="Vertrieb",
            frame={"tools": {"odoo": {"read": True}}, "memory": {}},
        )
        s.add(dept)
        await s.flush()
        lead = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Nora",
            is_team_lead=True,
            status="stopped",
        )
        s.add(lead)
        await s.flush()
        dept.team_lead_agent_id = lead.id
        await s.flush()

        exported = await build_department_export(
            s, tenant_id=tenant, department_id=dept.id, capa_name="vertrieb",
            version="1.0.0", summary="",
        )
        parsed = parse_manifest(__import__("tomllib").loads(exported.manifest_toml)["plugin"])
        assert "integration:odoo" in parsed.depends
        assert parsed.department_template is not None
        assert "odoo" in (parsed.department_template.frame.get("tools") or {})
        assert exported.warnings == []


async def test_build_department_export_drops_null_policy_fields(
    app_session: AppSessionFactory,
) -> None:
    """`ToolPolicy.to_json()` (authz/pdp.py:107-109) always writes `only` and
    `approval_eur`, `None` when unset -- the real shape stored on a
    department's `frame.tools` (confirmed live against a real Helpdesk
    department, 2026-09-02). `frame`/`narrowing` are untyped `dict[str, Any]`
    in manifest.py, so `Manifest.model_dump(exclude_none=True)` never reaches
    into them -- a raw `None` used to reach `tomli_w.dumps` and crash with
    `TypeError: Object of type 'NoneType' is not TOML serializable` for any
    real department, not just a crafted fixture."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        capa = m.Capa(tenant_id=tenant, name="odoo_mcp", type="tool_pack")
        s.add(capa)
        await s.flush()
        capa_version = m.CapaVersion(
            tenant_id=tenant,
            capa_id=capa.id,
            semver="1.0.0",
            manifest={"name": "odoo_mcp", "version": "1.0.0"},
            artifact_hash=b"x",
            capabilities=["integration:odoo"],
        )
        s.add(capa_version)
        await s.flush()
        capa.current_version_id = capa_version.id
        s.add(
            m.McpConnection(
                tenant_id=tenant,
                name="odoo",
                server_url="stdio://odoo",
                config={"_plugin_name": "odoo_mcp"},
            )
        )
        dept = m.Department(
            tenant_id=tenant,
            name="Helpdesk",
            frame={
                "tools": {
                    "odoo": {
                        "only": None,
                        "read": True,
                        "modify": True,
                        "enabled": True,
                        "approval_eur": None,
                        "approval_actions": [],
                        "default_connection_id": str(uuid.uuid4()),
                    }
                },
                "memory": {},
            },
        )
        s.add(dept)
        await s.flush()
        lead = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Nora",
            is_team_lead=True,
            status="stopped",
        )
        s.add(lead)
        await s.flush()
        dept.team_lead_agent_id = lead.id
        await s.flush()

        exported = await build_department_export(
            s, tenant_id=tenant, department_id=dept.id, capa_name="helpdesk",
            version="1.0.0", summary="",
        )
        parsed = parse_manifest(__import__("tomllib").loads(exported.manifest_toml)["plugin"])
        assert parsed.department_template is not None
        odoo_policy = (parsed.department_template.frame.get("tools") or {})["odoo"]
        assert "only" not in odoo_policy
        assert "approval_eur" not in odoo_policy
        assert odoo_policy["read"] is True


_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


async def test_build_department_export_never_leaks_any_uuid_into_toml(
    app_session: AppSessionFactory,
) -> None:
    """A recursive check for the design's hard global constraint: no secret
    value, credential id, connection id, model_config_id, or KB id may ever
    appear in generated TOML. Scanning the RENDERED TEXT for anything shaped
    like a UUID is a stronger check than asserting individual fields are
    absent -- it catches a leak through any field, present or future, not
    just the ones this test happens to name.

    Crucially, this plants a real connection id INSIDE a tool-policy dict at
    both levels the runtime actually writes one: `default_connection_id` on
    `Department.frame["tools"][key]` (`ToolPolicyWriteDTO`,
    api/v1/departments.py) and `connection_id` on
    `Agent.narrowing["tools"][key]` (`ToolPolicy.to_json()`, authz/pdp.py) --
    the exact leak path a generic UUID scan over untouched fixtures would
    never exercise."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        skill = await _make_skill(s, tenant, "crm-follow-up")
        capa = m.Capa(tenant_id=tenant, name="odoo_mcp", type="tool_pack")
        s.add(capa)
        await s.flush()
        capa_version = m.CapaVersion(
            tenant_id=tenant,
            capa_id=capa.id,
            semver="1.0.0",
            manifest={"name": "odoo_mcp", "version": "1.0.0"},
            artifact_hash=b"x",
            capabilities=["integration:odoo"],
        )
        s.add(capa_version)
        await s.flush()
        capa.current_version_id = capa_version.id
        s.add(
            m.McpConnection(
                tenant_id=tenant,
                name="odoo",
                server_url="stdio://odoo",
                config={"_plugin_name": "odoo_mcp"},
                credential_id=uuid.uuid4(),
            )
        )
        dept_connection_id = str(uuid.uuid4())
        dept = m.Department(
            tenant_id=tenant,
            name="Vertrieb",
            frame={
                "tools": {
                    "odoo": {"read": True, "default_connection_id": dept_connection_id}
                },
                "memory": {"department": ["read"]},
            },
        )
        s.add(dept)
        await s.flush()
        lead = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Nora",
            is_team_lead=True,
            status="stopped",
            model_config_id=uuid.uuid4(),
        )
        s.add(lead)
        await s.flush()
        dept.team_lead_agent_id = lead.id
        agent_connection_id = str(uuid.uuid4())
        rep = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Rep A",
            status="stopped",
            model_config_id=uuid.uuid4(),
            narrowing={
                "tools": {"odoo": {"read": True, "connection_id": agent_connection_id}}
            },
            narrowing_overridden_keys=["odoo"],
        )
        s.add(rep)
        await s.flush()
        s.add(
            m.SkillAssignment(
                tenant_id=tenant, agent_id=rep.id, skill_version_id=skill.current_version_id
            )
        )
        await s.flush()

        exported = await build_department_export(
            s, tenant_id=tenant, department_id=dept.id, capa_name="vertrieb",
            version="1.0.0", summary="",
        )
        assert dept_connection_id not in exported.manifest_toml
        assert agent_connection_id not in exported.manifest_toml
        assert _UUID_RE.search(exported.manifest_toml) is None
        for warning in exported.warnings:
            assert _UUID_RE.search(warning) is None
        assert any("odoo" in w and "connection" in w for w in exported.warnings)

        parsed = parse_manifest(__import__("tomllib").loads(exported.manifest_toml)["plugin"])
        assert parsed.department_template is not None
        frame_tools = parsed.department_template.frame.get("tools") or {}
        assert "default_connection_id" not in frame_tools.get("odoo", {})
        assert frame_tools["odoo"]["read"] is True
        rep_a = next(a for a in parsed.department_template.agents if a.name == "Rep A")
        assert "connection_id" not in rep_a.narrowing.get("tools", {}).get("odoo", {})
        assert rep_a.narrowing["tools"]["odoo"]["read"] is True


async def test_build_department_export_rejects_duplicate_agent_names(
    app_session: AppSessionFactory,
) -> None:
    """Two agents named the same within one department is already impossible
    in this codebase (agent names aren't unique-constrained, but this proves
    the export-time guard fires if it ever happens) -- mirrors
    `instantiate_department`'s own `PluginError("template agent names must be
    unique")` (service.py:181-182)."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        s.add(dept)
        await s.flush()
        for _ in range(2):
            s.add(
                m.Agent(
                    tenant_id=tenant, department_id=dept.id, name="Nora", status="stopped"
                )
            )
        await s.flush()
        with pytest.raises(ExportValidationError, match="unique"):
            await build_department_export(
                s, tenant_id=tenant, department_id=dept.id, capa_name="vertrieb",
                version="1.0.0", summary="",
            )


async def test_build_agent_export_standalone_drops_reports_to(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        s.add(dept)
        await s.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Rep A",
            status="stopped",
            definition={"reports_to": "Nora"},
        )
        s.add(agent)
        await s.flush()

        exported = await build_agent_export(
            s, tenant_id=tenant, agent_id=agent.id, capa_name="rep_a", version="1.0.0", summary=""
        )
        assert any("reports_to" in w for w in exported.warnings)
        parsed = parse_manifest(__import__("tomllib").loads(exported.manifest_toml)["plugin"])
        assert parsed.agent_template is not None
        assert parsed.agent_template.reports_to is None


async def test_build_agent_export_skips_copilot_followup_triggers(
    app_session: AppSessionFactory,
) -> None:
    import datetime as dt

    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        s.add(dept)
        await s.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Rep A", status="stopped")
        s.add(agent)
        await s.flush()
        s.add(
            m.Trigger(
                tenant_id=tenant, agent_id=agent.id, kind="cron", cron_expression="0 9 * * 1",
                task_text="a member's private follow-up", enabled=True,
                chat_session_id=uuid.uuid4(), responsibility_id=uuid.uuid4(), timezone="UTC",
                ends_at=dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=30),
            )
        )  # fmt: skip
        await s.flush()

        exported = await build_agent_export(
            s, tenant_id=tenant, agent_id=agent.id, capa_name="rep_a", version="1.0.0", summary=""
        )
        assert "private follow-up" not in exported.manifest_toml
        parsed = parse_manifest(__import__("tomllib").loads(exported.manifest_toml)["plugin"])
        assert parsed.agent_template is not None
        assert parsed.agent_template.trigger is None
